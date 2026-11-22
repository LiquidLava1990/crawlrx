"""Playwright-fallback scraper for Target (split out of target.py)."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any, Callable, List, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    parse_rating_value as _hp_parse_rating,
)
from ._target_http import (
    TARGET_BASE_URL,
    build_product_url,
    extract_tcin_from_url,
)
from ._target_models import (
    TargetProduct,
    TargetProductDetail,
    TargetSearchRequest,
    TargetSearchResponse,
)
from ._target_playwright_detail import (
    populate_availability,
    populate_basics,
    populate_category,
    populate_fulfillment,
    populate_images,
    populate_metrics,
    populate_specs,
)
from ._target_playwright_reviews import populate_product_reviews
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.target.playwright")


async def _await_card_attr(card: Any, selector: str) -> Optional[str]:
    """Resolve a card sub-element's ``href``-style attribute."""
    link_el = await card.query_selector(selector)
    if link_el is None:
        return None
    href = await link_el.get_attribute("href")
    return href or None


async def _resolve_card_tcin(card: Any) -> Optional[str]:
    """Resolve TCIN for a search-result card (data-tcin or anchor href)."""
    tcin = await card.get_attribute("data-tcin")
    if tcin:
        return tcin
    href = await _await_card_attr(card, "a[href*='/p/A-']")
    return extract_tcin_from_url(href) if href else None


async def _await_inner_text(card: Any, selector: str) -> Optional[str]:
    """Return the inner text of a sub-element, or ``None``."""
    el = await card.query_selector(selector)
    if el is None:
        return None
    text = await el.inner_text()
    return text.strip() if text else None


async def _resolve_card_image(card: Any) -> Optional[str]:
    """Resolve the image src for a search-result card."""
    img_el = await card.query_selector("img")
    if img_el is None:
        return None
    return await img_el.get_attribute("src")


async def _resolve_card_url(card: Any) -> Optional[str]:
    """Resolve the absolute product URL for a search-result card."""
    href = await _await_card_attr(card, "a[href^='/p/']")
    return f"{TARGET_BASE_URL}{href}" if href else None


async def _resolve_card_availability(card: Any) -> str:
    """Probe the card for an out-of-stock indicator."""
    try:
        oos_el = await card.query_selector("text=/out of stock/i")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("availability probe suppressed: %s: %s", type(exc).__name__, exc)
        return "In stock"
    return "Out of stock" if oos_el else "In stock"


async def _resolve_card_brand(card: Any) -> Optional[str]:
    """Probe the card for the brand label (returns ``None`` on miss)."""
    try:
        return await _await_inner_text(card, "[data-test='brand-name']")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("brand probe suppressed: %s: %s", type(exc).__name__, exc)
        return None


async def _await_rating(card: Any) -> Optional[float]:
    """Resolve a numeric rating from a ``[data-test='rating']`` sub-node."""
    text = await _await_inner_text(card, "[data-test='rating']")
    return _hp_parse_rating(text)


async def _build_search_card(card: Any) -> Optional[TargetProduct]:
    """Build a :class:`TargetProduct` from a Playwright search-result card."""
    try:
        tcin = await _resolve_card_tcin(card)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("tcin resolve skipped: %s: %s", type(exc).__name__, exc)
        return None
    if not tcin:
        return None
    title = await _await_inner_text(card, "[data-test='product-title']") or ""
    price = await _await_inner_text(card, "[data-test='product-price']")
    rating = await _await_rating(card)
    image_url = await _resolve_card_image(card)
    product_url = await _resolve_card_url(card)
    availability = await _resolve_card_availability(card)
    brand = await _resolve_card_brand(card)
    return TargetProduct(
        title=title,
        price=price,
        rating=rating,
        product_url=product_url or build_product_url(tcin),
        image_url=image_url,
        tcin=tcin,
        availability=availability,
        brand=brand,
    )


async def _scroll_search_page(page: Any) -> None:
    """Trigger lazy-load scrolls on the search results page."""
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(0.5)


async def _wait_for_first_card(page: Any) -> None:
    """Wait briefly for the first product card. Timeout is non-fatal."""
    try:
        await page.wait_for_selector("[data-test='product-card']", timeout=10000)
    except PlaywrightTimeoutError:
        logger.debug("Card selector wait timed out for Target search page")


async def _safe_build_search_card(card: Any) -> Optional[TargetProduct]:
    """Wrap :func:`_build_search_card` with the selector safety-net."""
    try:
        return await _build_search_card(card)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Card build skipped: %s: %s", type(exc).__name__, exc)
        return None


async def _collect_search_cards(page: Any, limit: int) -> List[TargetProduct]:
    """Iterate the visible cards into a list of :class:`TargetProduct`."""
    out: List[TargetProduct] = []
    cards = await page.query_selector_all("[data-test='product-card']")
    for card in cards[: max(limit * 2, 1)]:
        built = await _safe_build_search_card(card)
        if built is not None:
            out.append(built)
    return out


async def _run_search_session(request: TargetSearchRequest) -> List[TargetProduct]:
    """Drive the Playwright session for a search query."""
    browser = ctx = page = None
    async with async_playwright() as p:
        try:
            browser, ctx, page = await make_stealth_context(p)
            search_url = (
                f"{TARGET_BASE_URL}/s?searchTerm={urllib.parse.quote_plus(request.query)}"
            )
            logger.info("[Target] Navigating to search: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_first_card(page)
            await _scroll_search_page(page)
            return await _collect_search_cards(page, request.limit)
        finally:
            await close_stealth_context(browser, ctx, page)


async def search_target_playwright(
    request: TargetSearchRequest,
    finalise: Callable[..., List[TargetProduct]],
) -> TargetSearchResponse:
    """Search Target using Playwright CDP."""
    start = timer_ms()
    try:
        cards = await _run_search_session(request)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Target Playwright search error: %s: %s", type(exc).__name__, exc)
        return TargetSearchResponse(success=False, error=str(exc), elapsed_ms=elapsed)
    filtered = finalise(cards, request)
    elapsed = int(timer_ms() - start)
    logger.info(
        "Target Playwright search returned %d products in %dms",
        len(filtered), elapsed,
    )
    return TargetSearchResponse(
        success=True, products=filtered, count=len(filtered), elapsed_ms=elapsed
    )


async def _run_product_session(tcin: str) -> Optional[TargetProductDetail]:
    """Drive the Playwright session that fills a product-detail page."""
    browser = ctx = page = None
    async with async_playwright() as p:
        try:
            browser, ctx, page = await make_stealth_context(p)
            url = build_product_url(tcin)
            logger.info("[Target] Navigating to product: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)
            product = TargetProductDetail(title="", tcin=tcin)
            await populate_basics(page, product)
            await populate_metrics(page, product)
            await populate_availability(page, product)
            await populate_fulfillment(page, product)
            await populate_images(page, product)
            await populate_specs(page, product)
            await populate_category(page, product)
            await populate_product_reviews(page, product)
            return product if product.title else None
        finally:
            await close_stealth_context(browser, ctx, page)


async def get_product_playwright(tcin: str) -> Optional[TargetProductDetail]:
    """Get product details using Playwright CDP."""
    try:
        return await _run_product_session(tcin)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "Target Playwright product fetch error: %s: %s",
            type(exc).__name__, exc,
        )
        return None


__all__ = (
    "get_product_playwright",
    "search_target_playwright",
)
