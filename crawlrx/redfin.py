"""redfin.py -- Redfin real estate scraper.

Router prefix: ``/scrape/redfin``

* ``POST /search`` -- search Redfin listings
* ``POST /property`` -- scrape one Redfin property page

Implementation is a thin orchestrator over the helpers in
``_redfin_helpers`` / ``_redfin_property`` / ``_realestate_helpers`` /
``_scraper_helpers``. Every per-card / per-fragment branch lives in one
of those sibling modules so this file stays under the code-quality
thresholds (function length / nesting / cognitive / cyclomatic / param
count) and never catches a bare ``Exception``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import _redfin_helpers as rfh
from . import _redfin_property as rfp
from ._realestate_helpers import (
    RealEstateSearchParams,
    SearchUrlSpec,
    is_captcha_blocked,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)
from .models import (
    RedfinListing,
    RedfinPropertyRequest,
    RedfinPropertyResponse,
    RedfinSearchRequest,
    RedfinSearchResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.redfin")
router = APIRouter(prefix="/scrape/redfin", tags=["redfin"])


# ---------------------------------------------------------------------------
# URL builder (param-bundle keeps the parameter-count limit)
# ---------------------------------------------------------------------------

def _build_search_url(spec: SearchUrlSpec) -> str:
    """Compose a Redfin search URL from a :class:`SearchUrlSpec`."""
    slug = rfh.location_slug(spec.location)
    path = rfh.search_path(spec.listing_type, slug)
    query = rfh.search_query(
        spec.min_price, spec.max_price, spec.min_beds, spec.min_baths,
    )
    return f"{rfh.REDFIN_BASE_URL}{path}{query}"


# ---------------------------------------------------------------------------
# Page-level orchestration helpers (keep cognitive complexity bounded)
# ---------------------------------------------------------------------------

_LISTING_WAIT_SELECTOR = (
    ".HomeCardContainer, .home-card, [data-rf-test-id='homes-list']"
)


async def _wait_for_listings(page: Any) -> None:
    """Wait for the search results to render; warn but continue on timeout."""
    try:
        await page.wait_for_selector(_LISTING_WAIT_SELECTOR, timeout=20000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Redfin listings, continuing anyway")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal
    await page.wait_for_timeout(3000)


async def _scroll_for_more(page: Any, limit: int) -> None:
    """Scroll the search page enough to lazy-load up to ``limit`` cards."""
    scroll_count = min(limit // 6 + 1, 5)
    for _ in range(scroll_count):
        try:
            await page.evaluate("window.scrollBy(0, 1000)")
        except BROWSER_SCRAPE_EXCEPTIONS:
            return
        await page.wait_for_timeout(1000)


async def _extract_listings(page: Any, limit: int) -> List[RedfinListing]:
    """Walk the page's listing cards and return validated listings."""
    cards = await rfh.collect_card_elements(page)
    logger.info("Found %d potential listing cards on Redfin", len(cards))
    listings: List[RedfinListing] = []
    for card in cards[:limit]:
        try:
            listing = await rfh.extract_listing_card(card)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Skipping Redfin card (selector error): %s", exc)
            continue
        if listing is None:
            continue
        try:
            listings.append(RedfinListing(**listing))
        except SCRAPE_FAILURE_EXCEPTIONS as exc:
            logger.debug("Skipping Redfin card (model error): %s", exc)
            continue
    return listings


# ---------------------------------------------------------------------------
# Failure-payload builders (keep elapsed-ms maths in one place)
# ---------------------------------------------------------------------------

def _elapsed_ms(start: float) -> int:
    """Return the wall-clock millisecond delta since ``start``."""
    return int((time.time() - start) * 1000)


def _search_failure(start: float, error_message: str) -> Dict[str, Any]:
    """Build the canonical failed-search payload."""
    return {
        "success": False,
        "listings": [],
        "count": 0,
        "error": error_message,
        "elapsed_ms": _elapsed_ms(start),
    }


def _property_failure(start: float, url: str, error_message: str) -> Dict[str, Any]:
    """Build the canonical failed-property payload."""
    return {
        "success": False,
        "error": error_message,
        "url": url,
        "elapsed_ms": _elapsed_ms(start),
    }


# ---------------------------------------------------------------------------
# Main async entry points (each well under 80 LOC and 80 cognitive)
# ---------------------------------------------------------------------------

async def _do_search_redfin(
    params: RealEstateSearchParams,
) -> Dict[str, Any]:
    """Search Redfin via Playwright + stealth and return a result dict."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            url_spec = SearchUrlSpec(
                location=params.location,
                listing_type=params.listing_type,
                min_price=params.min_price,
                max_price=params.max_price,
                min_beds=params.min_beds,
                min_baths=params.min_baths,
            )
            search_url = _build_search_url(url_spec)
            logger.info("Navigating to Redfin search: %s", search_url)
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=45000,
            )
            if await is_captcha_blocked(page):
                return _search_failure(start, "blocked: challenge or environment-check")
            await _wait_for_listings(page)
            await _scroll_for_more(page, params.limit)
            listings = await _extract_listings(page, params.limit)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Extracted %d Redfin listings in %dms", len(listings), elapsed,
            )
            return {
                "success": True,
                "listings": listings,
                "count": len(listings),
                "elapsed_ms": elapsed,
            }
    except PlaywrightTimeoutError as exc:
        logger.error("Timeout searching Redfin: %s", exc)
        return _search_failure(start, f"Timeout: {exc}")
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Browser error searching Redfin: %s", exc, exc_info=True)
        return _search_failure(start, str(exc))
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Parse error searching Redfin: %s", exc, exc_info=True)
        return _search_failure(start, str(exc))
    finally:
        await close_stealth_context(browser, ctx, page)


async def _wait_for_property_details(page: Any) -> None:
    """Wait for property-detail markup; warn-and-continue on timeout."""
    try:
        await page.wait_for_selector(rfh.detail_wait_selector(), timeout=20000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Redfin property details")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal
    await page.wait_for_timeout(3000)


async def _populate_property(
    page: Any, url: str,
) -> Dict[str, Any]:
    """Run every fill_* fragment against ``page`` and return the payload."""
    data = rfp.empty_property(url)
    for fragment in rfp.PROPERTY_FRAGMENTS:
        try:
            await fragment(page, data)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug(
                "Redfin fragment %s suppressed: %s",
                fragment.__name__, exc,
            )
    return data


async def _do_scrape_property(url: str) -> Dict[str, Any]:
    """Scrape one Redfin property page and return its detail payload."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            logger.info("Navigating to Redfin property: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if await is_captcha_blocked(page):
                return _property_failure(
                    start, url, "blocked: challenge or environment-check",
                )
            await _wait_for_property_details(page)
            data = await _populate_property(page, url)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Scraped Redfin property '%s' in %dms",
                data.get("address"), elapsed,
            )
            return {"success": True, **data, "elapsed_ms": elapsed}
    except PlaywrightTimeoutError as exc:
        logger.error("Timeout scraping Redfin property: %s", exc)
        return _property_failure(start, url, f"Timeout: {exc}")
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "Browser error scraping Redfin property: %s", exc, exc_info=True,
        )
        return _property_failure(start, url, str(exc))
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error(
            "Parse error scraping Redfin property: %s", exc, exc_info=True,
        )
        return _property_failure(start, url, str(exc))
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# FastAPI route handlers
# ---------------------------------------------------------------------------

@router.post("/search", response_model=RedfinSearchResponse)
async def search_redfin(request: RedfinSearchRequest) -> RedfinSearchResponse:
    """Search Redfin listings.

    - ``location``: Location to search (city, zip, address)
    - ``listing_type``: ``for_sale`` / ``sold`` / ``for_rent``
    - ``min_price`` / ``max_price``: Price filter
    - ``min_beds``: Minimum bedrooms
    - ``min_baths``: Minimum bathrooms
    - ``limit``: Max results (1-50)
    """
    params = RealEstateSearchParams(
        location=request.location,
        listing_type=request.listing_type,
        min_price=request.min_price,
        max_price=request.max_price,
        min_beds=request.min_beds,
        min_baths=request.min_baths,
        limit=request.limit,
    )
    result = await asyncio.wait_for(_do_search_redfin(params), timeout=60)
    return RedfinSearchResponse(**result)


@router.post("/property", response_model=RedfinPropertyResponse)
async def scrape_redfin_property(
    request: RedfinPropertyRequest,
) -> RedfinPropertyResponse:
    """Scrape one Redfin property URL."""
    result = await asyncio.wait_for(
        _do_scrape_property(request.url), timeout=60,
    )
    return RedfinPropertyResponse(**result)
