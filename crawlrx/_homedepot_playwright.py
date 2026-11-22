"""HomeDepot Playwright-driven scrape helpers (split from homedepot.py).

Keeps homedepot.py under the 500-line ceiling and isolates the long chain
of Playwright DOM probes from the HTTP / API surface.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._homedepot_parsers import HD_PARSE_EXCEPTIONS, HOMEDEPOT_BASE_URL
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.homedepot.playwright")

_MODEL_RE = re.compile(r"Model[#\s]+([A-Za-z0-9-]+)")


def _build_search_url(query: str) -> str:
    """Build a Home Depot search URL for ``query``."""
    return f"{HOMEDEPOT_BASE_URL}/s/{urllib.parse.quote_plus(query)}"


async def _wait_for_search_results(page: Any) -> None:
    """Soft-wait for either a product card or the testid title to appear."""
    try:
        await page.wait_for_selector(
            "[data-testid='product-title'], .product-title, .browse-search-result",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pass


async def _scroll_search_page(page: Any) -> None:
    """Scroll to surface lazy-loaded results."""
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(0.5)


async def _text_or_none(card: Any, selector: str) -> Optional[str]:
    """Return inner text from the first matching descendant of ``card``."""
    element = await card.query_selector(selector)
    if element is None:
        return None
    text = (await element.inner_text()).strip()
    return text or None


async def _href_or_none(card: Any, selector: str) -> Optional[str]:
    """Return the ``href`` of the first matching anchor, or ``None``."""
    element = await card.query_selector(selector)
    if element is None:
        return None
    return await element.get_attribute("href")


async def _attr_or_none(card: Any, selector: str, name: str) -> Optional[str]:
    """Return ``getAttribute(name)`` from the first matching descendant."""
    element = await card.query_selector(selector)
    if element is None:
        return None
    return await element.get_attribute(name)


def _parse_price_str(price_text: Optional[str]) -> Optional[str]:
    """Pull the ``$N.NN`` substring out of ``price_text`` (or strip)."""
    if not price_text:
        return None
    match = re.search(r"\$[\d,]+\.\d{2}", price_text)
    return match.group(0) if match else price_text.strip()


async def _build_search_card_product(
    card: Any, product_factory: Any
) -> Optional[Any]:
    """Parse one Playwright search-results card."""
    title = await _text_or_none(
        card, "[data-testid='product-title'], .product-title, h3 a"
    )
    if not title:
        return None
    price = await _text_or_none(
        card, "[data-testid='product-price'], .price-current"
    )
    href = await _href_or_none(card, "a[href^='/p/']")
    image_url = await _attr_or_none(card, "img", "src")
    sku = await _text_or_none(card, "[data-testid='model-number'], .model-number")
    brand = await _text_or_none(card, "[data-testid='brand-name'], .brand-name")
    product_url = f"{HOMEDEPOT_BASE_URL}{href}" if href else ""

    return product_factory(
        title=title,
        price=_parse_price_str(price) if price else None,
        brand=brand,
        product_url=product_url,
        image_url=image_url,
        model_number=sku,
    )


async def _collect_search_products(
    page: Any, limit: int, product_factory: Any
) -> List[Any]:
    """Iterate the rendered search-result cards into product objects."""
    cards = await page.query_selector_all(
        "[data-testid='product-card'], .product-card, .browse-search-result"
    )
    products: List[Any] = []
    for card in cards[:limit]:
        product = await safe_extract_async(
            lambda card=card: _build_search_card_product(card, product_factory),
            field="hd_pw_card",
            log=logger,
        )
        if product:
            products.append(product)
    return products


async def search_via_playwright(
    request: object,
    response_factory: "Callable[..., Any]",
    product_factory: "Callable[..., Any]",
) -> object:
    """Search Home Depot using Playwright CDP."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = _build_search_url(request.query)
            logger.info("[Home Depot] Navigating to search: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_search_results(page)
            await _scroll_search_page(page)
            products = await _collect_search_products(
                page, request.limit, product_factory
            )
            elapsed = int(timer_ms() - start)
            logger.info(
                "Home Depot Playwright search returned %d products in %dms",
                len(products),
                elapsed,
            )
            return response_factory(
                success=True,
                products=products,
                count=len(products),
                elapsed_ms=elapsed,
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Home Depot Playwright search error: %s", exc)
        return response_factory(
            success=False,
            products=[],
            count=0,
            error=str(exc),
            elapsed_ms=elapsed,
        )
    finally:
        await close_stealth_context(browser, ctx, page)


async def _pw_text(page: Any, selector: str) -> Optional[str]:
    """Return ``inner_text`` of the first matching element on ``page``."""
    elem = await page.query_selector(selector)
    if elem is None:
        return None
    text = (await elem.inner_text()).strip()
    return text or None


async def _populate_pw_title(page: Any, product: Any) -> None:
    """Populate ``product.title`` from the page H1."""
    title = await _pw_text(page, "h1[data-testid='product-title'], h1")
    if title:
        product.title = title


async def _populate_pw_price(page: Any, product: Any) -> None:
    """Populate ``product.price`` from the rendered price node."""
    text = await _pw_text(page, "[data-testid='product-price'], .price-current")
    if text:
        product.price = _parse_price_str(text)


async def _populate_pw_description(page: Any, product: Any) -> None:
    """Populate ``product.description`` from the rendered description node."""
    text = await _pw_text(
        page, "[data-testid='product-description'], .product-description"
    )
    if text:
        product.description = text


async def _populate_pw_specs(page: Any, product: Any) -> None:
    """Populate ``product.specs`` from rendered spec rows."""
    rows = await page.query_selector_all(
        "[data-testid='spec-row'], .specification-row"
    )
    specs: Dict[str, str] = {}
    for row in rows:
        label_el = await row.query_selector(
            "[data-testid='spec-label'], .spec-label"
        )
        value_el = await row.query_selector(
            "[data-testid='spec-value'], .spec-value"
        )
        if not (label_el and value_el):
            continue
        label = (await label_el.inner_text()).strip()
        value = (await value_el.inner_text()).strip()
        if label:
            specs[label] = value
    product.specs = specs


async def _populate_pw_images(page: Any, product: Any) -> None:
    """Populate ``product.images`` from the rendered gallery."""
    img_els = await page.query_selector_all(
        "[data-testid='product-image'] img, .product-image img"
    )
    images: List[str] = []
    for img in img_els:
        src = await img.get_attribute("src")
        if src:
            images.append(src)
    product.images = images


async def _populate_pw_model(page: Any, product: Any) -> None:
    """Populate ``product.model_number`` from the rendered model node."""
    text = await _pw_text(page, "[data-testid='model-number'], .model-number")
    if not text:
        return
    match = _MODEL_RE.search(text)
    if match:
        product.model_number = match.group(1)


async def _populate_pw_brand(page: Any, product: Any) -> None:
    """Populate ``product.brand`` from the rendered brand node."""
    text = await _pw_text(page, "[data-testid='brand-name'], .brand-name")
    if text:
        product.brand = text


_PRODUCT_POPULATORS: tuple = (
    ("title", _populate_pw_title),
    ("price", _populate_pw_price),
    ("description", _populate_pw_description),
    ("specs", _populate_pw_specs),
    ("images", _populate_pw_images),
    ("model", _populate_pw_model),
    ("brand", _populate_pw_brand),
)


async def _populate_product_detail(page: Any, product: Any) -> None:
    """Run each Playwright-driven field probe under narrow error handling."""
    for field, populator in _PRODUCT_POPULATORS:
        try:
            await populator(page, product)
        except (*BROWSER_SCRAPE_EXCEPTIONS, *HD_PARSE_EXCEPTIONS) as exc:
            logger.debug("HD pw %s probe error: %s", field, exc)


async def get_product_via_playwright(
    url: str, detail_factory: "Callable[..., Any]"
) -> Optional[Any]:
    """Get product details using Playwright CDP."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Home Depot] Navigating to product: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)  # Wait for dynamic content
            product = detail_factory(title="")
            await _populate_product_detail(page, product)
            return product if product.title else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Home Depot Playwright product fetch error: %s", exc)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)
