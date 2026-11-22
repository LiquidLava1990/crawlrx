"""Playwright-driven Costco scrape paths.

Split out of ``costco.py`` for readability: keeps the long
browser-driver flows out of the routing module and shrinks every
function below the Tier-0 fn-length / nesting / cognitive thresholds.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import urllib.parse
from typing import Any, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._costco_parsers import COSTCO_BASE_URL, extract_item_number_from_url
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    safe_extract_async,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.costco._playwright")


async def random_delay(min_ms: int = 800, max_ms: int = 1500) -> None:
    """Random stealth delay between page actions."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


# ---------------------------------------------------------------------------
# Search-card extractors (one tiny extractor per attribute)
# ---------------------------------------------------------------------------

async def _card_item_number(card: Any) -> Optional[str]:
    item_number = card.get_attribute("data-item-number") or card.get_attribute(
        "data-itemid"
    )
    if item_number:
        return item_number
    link_el = card.query_selector("a[href*='.html']")
    if not link_el:
        return None
    href = await link_el.get_attribute("href") or ""
    match = re.search(r"/(\d+)\.html", href)
    return match.group(1) if match else None


async def _card_title_and_url(card: Any) -> tuple[str, str]:
    title_el = card.query_selector(".description a, .product-title, h3 a")
    if not title_el:
        return "", ""
    title = (await title_el.inner_text()).strip()
    href = await title_el.get_attribute("href")
    if not href:
        return title, ""
    product_url = (
        href if href.startswith("http") else COSTCO_BASE_URL + href
    )
    return title, product_url


async def _card_text(card: Any, selector: str) -> Optional[str]:
    element = card.query_selector(selector)
    if not element:
        return None
    return (await element.inner_text()).strip()


def _first_float_in(text: Optional[str]) -> Optional[float]:
    """Pull the first decimal number out of ``text`` (or ``None``)."""
    if not text:
        return None
    match = re.search(r"([\d.]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def _card_rating(card: Any) -> Optional[float]:
    rating_el = card.query_selector("[aria-label*='star'], .rating")
    if not rating_el:
        return None
    return _first_float_in(rating_el.get_attribute("aria-label"))


async def _card_review_count(card: Any) -> Optional[int]:
    review_el = card.query_selector(".review-count, [class*='review']")
    if not review_el:
        return None
    review_text = await review_el.inner_text()
    match = re.search(r"\((\d+)\)", review_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _card_image_url(card: Any) -> Optional[str]:
    img_el = card.query_selector("img")
    if not img_el:
        return None
    return await img_el.get_attribute("src")


async def _card_warehouse_only(card: Any) -> bool:
    text = (card.inner_text() or "").lower()
    return "warehouse only" in text


async def _build_search_card(card: Any, product_cls: Any) -> Optional[Any]:
    """Materialise one search card into a ``product_cls`` instance."""
    try:
        item_number = await _card_item_number(card)
        if not item_number:
            return None
        title, product_url = await _card_title_and_url(card)
        return product_cls(
            title=title,
            price=await _card_text(card, ".price, .value, .cost"),
            item_number=item_number,
            rating=await _card_rating(card),
            review_count=await _card_review_count(card),
            product_url=product_url,
            image_url=await _card_image_url(card),
            unit_price=await _card_text(card, ".unit-price, .per-unit"),
            warehouse_only=await _card_warehouse_only(card),
        )
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error parsing Costco product card: %s", exc)
        return None


async def _wait_for_results(page: Any) -> None:
    """Best-effort wait for product cards; selector miss is non-fatal."""
    # PlaywrightTimeoutError is the *expected* signal for "results never
    # rendered" — caller proceeds either way, so we suppress at this single
    # call site rather than wrap the function body.
    try:
        await page.wait_for_selector(
            ".product-tile, .item, [data-testid='product-tile']",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        return


async def _collect_search_products(
    page: Any,
    request: Any,
    product_cls: Any,
) -> list[Any]:
    products: list[Any] = []
    cards = await page.query_selector_all(
        ".product-tile, .item, [data-item-number]"
    )
    for card in cards[: request.limit]:
        built = await _build_search_card(card, product_cls)
        if built is not None:
            products.append(built)
    return products


def _build_search_url(query: str) -> str:
    return (
        f"{COSTCO_BASE_URL}/CatalogSearch?keyword={urllib.parse.quote_plus(query)}"
    )


async def search_costco_playwright(
    request: object,
    product_cls: "type[Any]",
    response_cls: "type[Any]",
) -> object:
    """Search Costco with a Playwright browser, returning ``response_cls``."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = _build_search_url(request.query)
            logger.info("[Costco] Navigating to search: %s", search_url)
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30000
            )
            await _wait_for_results(page)
            await random_delay()
            products = await _collect_search_products(
                page, request, product_cls
            )
        elapsed = int(timer_ms() - start)
        logger.info(
            "Costco Playwright search returned %d products in %dms",
            len(products),
            elapsed,
        )
        return response_cls(
            success=True,
            products=products,
            count=len(products),
            elapsed_ms=elapsed,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Costco Playwright search error: %s", exc)
        return response_cls(
            success=False,
            products=[],
            count=0,
            error=str(exc),
            elapsed_ms=elapsed,
        )
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Product-detail extractors
# ---------------------------------------------------------------------------

async def _extract_text(page: Any, selector: str) -> Optional[str]:
    async def _do() -> Optional[str]:
        el = await page.query_selector(selector)
        if not el:
            return None
        return (await el.inner_text()).strip()

    return await safe_extract_async(_do, field=selector, log=logger)


async def _query_aria_label(page: Any, selector: str) -> Optional[str]:
    """Return the ``aria-label`` text on the first match of ``selector``."""

    async def _read() -> Optional[str]:
        el = await page.query_selector(selector)
        return el.get_attribute("aria-label") if el else None

    return await safe_extract_async(_read, field=selector, log=logger)


async def _extract_rating(page: Any) -> Optional[float]:
    label = await _query_aria_label(page, "[aria-label*='star'], .rating")
    return _first_float_in(label)


async def _extract_images(page: Any, detail: Any) -> None:
    async def _list() -> list[Any]:
        return await page.query_selector_all(
            ".product-image img, .thumbnail img"
        )

    img_els = await safe_extract_async(_list, field="images", log=logger) or []
    for img in img_els:
        src = img.get_attribute("src") or img.get_attribute("data-zoom-image")
        if src:
            detail.images.append(src)


async def _extract_specs(page: Any, detail: Any) -> None:
    async def _rows() -> list[Any]:
        return await page.query_selector_all(
            ".specifications tr, .product-data tr"
        )

    rows = await safe_extract_async(_rows, field="specs", log=logger) or []
    for row in rows:
        cells = row.query_selector_all("th, td")
        if len(cells) < 2:
            continue
        key = cells[0].inner_text().strip()
        value = cells[1].inner_text().strip()
        if key and value:
            detail.specs[key] = value


async def _extract_features(page: Any, detail: Any) -> None:
    async def _list() -> list[Any]:
        return await page.query_selector_all(".product-features li, ul li")

    feature_els = (
        await safe_extract_async(_list, field="features", log=logger) or []
    )
    for feat in feature_els:
        text = feat.inner_text().strip()
        if text and len(text) > 10:
            detail.features.append(text)


async def _populate_detail(page: Any, detail: Any) -> None:
    detail.title = (
        await _extract_text(page, "h1, .product-title, [data-testid='product-title']")
        or detail.title
    )
    detail.price = await _extract_text(page, ".price, .value, .cost")
    detail.unit_price = await _extract_text(page, ".unit-price, .per-unit")
    detail.description = await _extract_text(
        page, ".product-description, .description"
    )
    detail.rating = await _extract_rating(page)
    detail.online_availability = await _extract_text(
        page,
        ".availability, .stock-status, [data-testid='availability']",
    )
    await _extract_images(page, detail)
    await _extract_specs(page, detail)
    await _extract_features(page, detail)


async def get_product_playwright(
    url: str,
    detail_cls: "type[Any]",
) -> Optional[Any]:
    """Fetch a product-page in a real browser, returning ``detail_cls``."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Costco] Navigating to product: %s", url)
            await page.goto(
                url, wait_until="domcontentloaded", timeout=30000
            )
            await random_delay()
            item_number = extract_item_number_from_url(url) or ""
            detail = detail_cls(title="", item_number=item_number)
            await _populate_detail(page, detail)
        return detail if detail.title else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Costco Playwright product fetch error: %s", exc)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)
