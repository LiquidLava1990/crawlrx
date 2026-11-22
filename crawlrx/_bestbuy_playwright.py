"""Playwright-driven Best Buy scrape paths.

Split out of ``bestbuy.py`` for readability: keeps the long
browser-driver flows out of the routing module and shrinks every
function below the Tier-0 fn-length / nesting / cognitive thresholds.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._bestbuy_parsers import BESTBUY_BASE_URL, build_product_url
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    safe_extract_async,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.bestbuy._playwright")


async def random_delay(min_ms: int = 800, max_ms: int = 1500) -> None:
    """Random stealth delay between page actions."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


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


# ---------------------------------------------------------------------------
# Browser context management
# ---------------------------------------------------------------------------

async def _acquire_browser_page(p: Any) -> tuple[Any, Any, Any]:
    """Launch Chromium, reuse first context, return (browser, ctx, page)."""
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    contexts = await browser.contexts
    context = contexts[0] if contexts else await browser.new_context()
    page = await context.new_page()
    return browser, context, page


async def _close_quietly(target: Any, label: str) -> None:
    """Close ``target`` if not ``None``; PLAYWRIGHT errors are non-fatal."""
    if target is None:
        return
    try:
        await target.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug(
            "%s cleanup failed (non-critical): %s", label, exc
        )


async def shutdown_page_context(page: "Page", context: "BrowserContext | None") -> None:
    await _close_quietly(page, "page")
    await _close_quietly(context, "context")


# ---------------------------------------------------------------------------
# Search-card extractors
# ---------------------------------------------------------------------------

async def _card_sku(card: Any) -> Optional[str]:
    sku = card.get_attribute("data-sku-id") or card.get_attribute("data-sku")
    if sku:
        return sku
    link_el = card.query_selector("a[href*='skuId=']")
    if not link_el:
        return None
    href = await link_el.get_attribute("href") or ""
    match = re.search(r"skuId=(\d+)", href)
    return match.group(1) if match else None


async def _card_title_and_url(card: Any) -> tuple[str, str]:
    title_el = card.query_selector("h4 a, .sku-title a")
    if not title_el:
        return "", ""
    title = (await title_el.inner_text()).strip()
    href = await title_el.get_attribute("href")
    if not href:
        return title, ""
    product_url = (
        href if href.startswith("http") else BESTBUY_BASE_URL + href
    )
    return title, product_url


async def _card_text(card: Any, selector: str) -> Optional[str]:
    element = card.query_selector(selector)
    if not element:
        return None
    return (await element.inner_text()).strip()


async def _card_rating(card: Any) -> Optional[float]:
    rating_el = card.query_selector("[aria-label*='stars']")
    if not rating_el:
        return None
    return _first_float_in(rating_el.get_attribute("aria-label"))


async def _card_review_count(card: Any) -> Optional[int]:
    review_el = card.query_selector(".c-reviews")
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


def _card_condition(text: str) -> tuple[str, bool]:
    lowered = text.lower()
    condition = "new"
    if "refurbished" in lowered:
        condition = "refurbished"
    elif "open-box" in lowered or "open box" in lowered:
        condition = "open-box"
    open_box_available = "open-box" in lowered or "open box" in lowered
    return condition, open_box_available


async def _build_search_card(card: Any, product_cls: Any) -> Optional[Any]:
    try:
        sku = await _card_sku(card)
        if not sku:
            return None
        title, product_url = await _card_title_and_url(card)
        price = await _card_text(card, ".pricing-price__range, .current-price")
        original = await _card_text(
            card, ".was-price, .pricing-price__range-crossed-out"
        )
        condition, open_box_available = _card_condition(card.inner_text() or "")
        return product_cls(
            title=title,
            price=price,
            original_price=original,
            rating=await _card_rating(card),
            review_count=await _card_review_count(card),
            sku=sku,
            product_url=product_url or build_product_url(sku),
            image_url=await _card_image_url(card),
            condition=condition,
            open_box_available=open_box_available,
        )
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error parsing Best Buy product card: %s", exc)
        return None


async def _wait_for_results(page: Any) -> None:
    """Best-effort wait for product cards; selector miss is non-fatal."""
    # PlaywrightTimeoutError is the *expected* signal for "results never
    # rendered" — caller proceeds either way.
    try:
        await page.wait_for_selector(
            ".sku-item, .list-item, [data-testid='product-title']",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        return


async def _collect_search_products(
    page: Any, request: Any, product_cls: Any
) -> list[Any]:
    products: list[Any] = []
    cards = await page.query_selector_all("li.sku-item, .list-item")
    for card in cards[: request.limit]:
        built = await _build_search_card(card, product_cls)
        if built is not None:
            products.append(built)
    return products


def _build_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    return f"{BESTBUY_BASE_URL}/site/searchpage.jsp?st={encoded}"


async def search_bestbuy_playwright(
    request: object,
    product_cls: "type[Any]",
    response_cls: "type[Any]",
) -> object:
    """Search Best Buy via a real Chromium browser."""
    start = timer_ms()
    browser = context = page = None
    try:
        async with async_playwright() as p:
            browser, context, page = await _acquire_browser_page(p)
            search_url = _build_search_url(request.query)
            logger.info("[Best Buy] Navigating to search: %s", search_url)
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
            "Best Buy Playwright search returned %d products in %dms",
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
        logger.error("Best Buy Playwright search error: %s", exc)
        return response_cls(
            success=False,
            products=[],
            count=0,
            error=str(exc),
            elapsed_ms=elapsed,
        )
    finally:
        await shutdown_page_context(page, context)


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
    async def _read() -> Optional[str]:
        el = await page.query_selector(selector)
        return el.get_attribute("aria-label") if el else None

    return await safe_extract_async(_read, field=selector, log=logger)


async def _extract_rating(page: Any) -> Optional[float]:
    label = await _query_aria_label(page, "[aria-label*='stars']")
    return _first_float_in(label)


async def _extract_availability(page: Any) -> Optional[str]:
    text = await _extract_text(
        page, ".fulfillment-add-to-cart-button, .add-to-cart-button"
    )
    if not text:
        return None
    return "Available" if "add to cart" in text.lower() else text


async def _extract_images(page: Any, detail: Any) -> None:
    async def _list() -> list[Any]:
        return await page.query_selector_all(
            ".thumbnail-image img, .product-image img"
        )

    img_els = await safe_extract_async(_list, field="images", log=logger) or []
    for img in img_els:
        src = img.get_attribute("src")
        if src:
            detail.images.append(src)


async def _extract_specs(page: Any, detail: Any) -> None:
    async def _rows() -> list[Any]:
        return await page.query_selector_all(
            ".specification-row, .product-data-value"
        )

    rows = await safe_extract_async(_rows, field="specs", log=logger) or []
    for row in rows:
        cells = row.query_selector_all("th, td, dt, dd")
        if len(cells) < 2:
            continue
        key = cells[0].inner_text().strip()
        value = cells[1].inner_text().strip()
        if key and value:
            detail.specs[key] = value


async def _populate_detail(page: Any, detail: Any) -> None:
    detail.title = (
        await _extract_text(page, "h1, .product-title")
        or detail.title
    )
    detail.price = await _extract_text(
        page, ".pricing-price__range, .current-price"
    )
    detail.original_price = await _extract_text(page, ".was-price")
    detail.description = await _extract_text(
        page, ".product-description, .overview-content"
    )
    detail.rating = await _extract_rating(page)
    detail.availability = await _extract_availability(page)
    await _extract_images(page, detail)
    await _extract_specs(page, detail)


async def get_product_playwright(
    sku: str,
    detail_cls: "type[Any]",
) -> Optional[Any]:
    """Fetch a Best Buy product page in a real browser."""
    browser = context = page = None
    try:
        async with async_playwright() as p:
            browser, context, page = await _acquire_browser_page(p)
            url = build_product_url(sku)
            logger.info("[Best Buy] Navigating to product: %s", url)
            await page.goto(
                url, wait_until="domcontentloaded", timeout=30000
            )
            await random_delay()
            detail = detail_cls(title="", sku=sku)
            await _populate_detail(page, detail)
        return detail if detail.title else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Best Buy Playwright product fetch error: %s", exc)
        return None
    finally:
        await shutdown_page_context(page, context)
