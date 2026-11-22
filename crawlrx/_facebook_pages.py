"""Page orchestrators for Facebook Marketplace scraping.

Each ``scrape_*`` here owns one Playwright session, builds the response
model, and closes the session in ``finally``. Per-field DOM probes live
in :mod:`._facebook_extractors`.

Re-imported by :mod:`crawlrx.facebook`. The pydantic model classes are
imported lazily to avoid a circular import.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import _facebook_extractors as _ex
from ._facebook_models import MarketplaceQuery
from ._scraper_helpers import safe_extract_async
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.facebook.pages")

LISTING_LINK_SELECTOR = 'a[href*="/marketplace/item/"]'


# ---------------------------------------------------------------------------
# Shared utilities.
# ---------------------------------------------------------------------------

async def random_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def now_ms_since(start: float) -> int:
    return int((time.time() - start) * 1000)


async def _wait_networkidle(page: Any, timeout_ms: int) -> None:
    """``page.wait_for_load_state('networkidle')``; timeouts are expected."""
    bounded = min(timeout_ms, 20000)
    try:
        await page.wait_for_load_state("networkidle", timeout=bounded)
    except PlaywrightTimeoutError as exc:
        logger.debug("networkidle timeout (expected): %s", exc)


async def _run_in_stealth_session(
    body: Callable[[Any], Awaitable[Any]],
    on_timeout: Callable[[PlaywrightTimeoutError], Any],
) -> Any:
    """Open a stealth browser session, invoke ``body(page)``, close on exit.

    Centralises the shared ``async with async_playwright() / try / finally``
    skeleton across :func:`scrape_marketplace` and :func:`scrape_listing`.
    On Playwright timeout we hand control to ``on_timeout``; everything else
    propagates so the outer ``asyncio.wait_for`` stays in control.
    """
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as runtime:
            browser, ctx, page = await make_stealth_context(runtime)
            return await body(page)
    except PlaywrightTimeoutError as exc:
        return on_timeout(exc)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Marketplace search.
# ---------------------------------------------------------------------------

def _is_duplicate_listing(listing_url: str, listings: list[Any]) -> bool:
    return any(item.listing_url == listing_url for item in listings)


async def _build_marketplace_listing(
    elem: Any, href: str,
) -> Optional[Any]:
    """Build one ``FacebookListingItem`` from a search-result link."""
    from .facebook import FacebookListingItem  # local import: avoid cycle

    listing_url = _ex.normalise_listing_url(href)
    if listing_url is None:
        return None
    listing = FacebookListingItem(listing_url=listing_url)
    card_text = await _ex.extract_card_text(elem)
    parsed = _ex.parse_card_lines(card_text)
    listing.title = parsed["title"]
    listing.price = parsed["price"]
    listing.location = parsed["location"]
    listing.posted_date = parsed["posted_date"]
    listing.image_url = await _ex.extract_card_image(elem)
    return listing


async def _collect_listing_elements(page: Any) -> list[Any]:
    elements = await safe_extract_async(
        lambda: page.query_selector_all(LISTING_LINK_SELECTOR),
        field="listing_links",
        default=[],
    )
    return list(elements or [])


async def _process_listing_element(
    elem: Any, listings: list[Any], remaining: int,
) -> bool:
    """Append a built listing to ``listings`` if eligible.

    Returns True when ``listings`` is now full (caller stops the inner
    loop).
    """
    if remaining <= 0:
        return True
    href = await _ex._attr(elem, "href")
    if not href:
        return False
    listing_url = _ex.normalise_listing_url(href)
    if listing_url is None:
        return False
    if _is_duplicate_listing(listing_url, listings):
        return False
    listing = await safe_extract_async(
        lambda: _build_marketplace_listing(elem, href),
        field="marketplace_listing",
    )
    if listing is None:
        return False
    listings.append(listing)
    return False


async def _scroll_page(page: Any) -> bool:
    """Scroll down to load more listings; returns True on success."""
    try:
        await page.evaluate("() => window.scrollBy(0, 800)")
        await random_delay(800, 1500)
        return True
    except PlaywrightTimeoutError as exc:
        logger.debug("[Facebook] Scroll timeout: %s", exc)
        return False


def _max_scrolls(limit: int) -> int:
    return min((limit // 5) + 3, 15)


async def _collect_marketplace_results(
    page: Any, limit: int,
) -> list[Any]:
    """Loop scroll/extract until ``limit`` results or scroll budget runs out."""
    listings: list[Any] = []
    max_scrolls = _max_scrolls(limit)
    for _ in range(max_scrolls):
        if len(listings) >= limit:
            break
        elements = await _collect_listing_elements(page)
        full = await _process_elements_batch(elements, listings, limit)
        if full:
            break
        if len(listings) < limit and not await _scroll_page(page):
            break
    return listings


async def _process_elements_batch(
    elements: list[Any], listings: list[Any], limit: int,
) -> bool:
    """Walk one batch; return True when listings is full."""
    for elem in elements:
        full = await _process_listing_element(
            elem, listings, limit - len(listings),
        )
        if full:
            return True
    return False


def _marketplace_response(
    success: bool,
    listings: list[Any],
    query: MarketplaceQuery,
    elapsed_ms: int,
    error_message: Optional[str] = None,
) -> Any:
    """Construct the pydantic response model."""
    from .facebook import FacebookMarketplaceResponse  # avoid cycle

    return FacebookMarketplaceResponse(
        success=success,
        listings=listings,
        count=len(listings),
        query=query.query,
        location=query.location,
        error=error_message,
        elapsed_ms=elapsed_ms,
    )


async def _marketplace_body(query: MarketplaceQuery, start: float, page: Any) -> Any:
    """Inner body for the marketplace stealth session."""
    url = _ex.build_marketplace_search_url(
        query.query, query.location, query.min_price, query.max_price,
    )
    logger.info("[Facebook] Searching marketplace: %s", url)
    await random_delay(300, 800)
    await page.goto(url, wait_until="domcontentloaded", timeout=query.timeout_ms)
    await _wait_networkidle(page, query.timeout_ms)
    await random_delay(1000, 2000)
    listings = await _collect_marketplace_results(page, query.limit)
    elapsed = now_ms_since(start)
    logger.info(
        "[Facebook] Scraped %d marketplace listings for '%s' in %dms",
        len(listings), query.query, elapsed,
    )
    return _marketplace_response(True, listings, query, elapsed)


async def scrape_marketplace(query: MarketplaceQuery) -> object:
    """Search Facebook Marketplace and return up to ``query.limit`` listings."""
    start = time.time()

    def on_timeout(exc: PlaywrightTimeoutError) -> object:
        logger.error("[Facebook] Timeout searching marketplace: %s", exc)
        return _marketplace_response(
            False, [], query, now_ms_since(start), error_message=f"Timeout: {exc}",
        )

    return await _run_in_stealth_session(
        lambda page: _marketplace_body(query, start, page), on_timeout,
    )


# ---------------------------------------------------------------------------
# Single-listing detail.
# ---------------------------------------------------------------------------

async def _populate_listing_basic(
    page: Any, listing: Any, page_text: str,
) -> None:
    """Populate title / price / description / category."""
    listing.title = await _ex.extract_listing_title(page)
    listing.price = await _ex.extract_listing_price(page, page_text)
    listing.description = await _ex.extract_listing_description(page)
    listing.category = await _ex.extract_listing_category(page)


async def _populate_listing_seller(page: Any, listing: Any) -> None:
    seller_name, seller_url = await _ex.extract_listing_seller(page)
    listing.seller_name = seller_name
    listing.seller_url = seller_url


def _populate_listing_text_fields(listing: Any, page_text: str) -> None:
    listing.location = _ex.find_location_line(page_text)
    listing.condition = _ex.find_condition(page_text)
    listing.posted_date = _ex.find_posted_date(page_text)


async def _populate_listing_images(page: Any, listing: Any) -> None:
    listing.images = await _ex.extract_listing_images(page)


async def _build_listing(page: Any, url: str) -> Any:
    """Build a `FacebookListingDetail` from the current page."""
    from .facebook import FacebookListingDetail  # avoid cycle

    listing = FacebookListingDetail()
    listing.listing_id = _ex.extract_listing_id_from_url(url)
    page_text = await _ex.get_page_text(page)
    await _populate_listing_basic(page, listing, page_text)
    await _populate_listing_seller(page, listing)
    _populate_listing_text_fields(listing, page_text)
    await _populate_listing_images(page, listing)
    return listing


def _listing_response(
    success: bool, listing: Optional[Any], url: str, elapsed_ms: int,
    error_message: Optional[str] = None,
) -> Any:
    from .facebook import FacebookListingResponse  # avoid cycle

    return FacebookListingResponse(
        success=success,
        listing=listing,
        url=url,
        error=error_message,
        elapsed_ms=elapsed_ms,
    )


def _log_listing(listing: Any) -> None:
    title = (listing.title[:30] if listing.title else "None")
    logger.info(
        "[Facebook] Scraped listing %s: title=%s, price=%s",
        listing.listing_id or "unknown", title, listing.price or "None",
    )


async def _listing_body(url: str, timeout_ms: int, start: float, page: Any) -> Any:
    """Inner body for the single-listing stealth session."""
    logger.info("[Facebook] Navigating to listing: %s", url)
    await random_delay(300, 800)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _wait_networkidle(page, timeout_ms)
    await random_delay(1000, 2000)
    listing = await _build_listing(page, url)
    elapsed = now_ms_since(start)
    _log_listing(listing)
    return _listing_response(True, listing, url, elapsed)


async def scrape_listing(url: str, timeout_ms: int) -> object:
    """Scrape one Facebook Marketplace listing page."""
    start = time.time()

    def on_timeout(exc: PlaywrightTimeoutError) -> object:
        logger.error("[Facebook] Timeout scraping listing %s: %s", url, exc)
        return _listing_response(
            False, None, url, now_ms_since(start), error_message=f"Timeout: {exc}",
        )

    return await _run_in_stealth_session(
        lambda page: _listing_body(url, timeout_ms, start, page), on_timeout,
    )
