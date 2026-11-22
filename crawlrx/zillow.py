"""zillow.py -- Zillow real-estate scraper (search / property / comps).

Router prefix: ``/scrape/zillow``

* ``POST /search`` -- search Zillow listings
* ``POST /property`` -- scrape one Zillow property page
* ``POST /comps`` -- recently-sold comparable sales for an address

The whole module is a thin orchestrator over the helpers in
``_zillow_helpers``, ``_zillow_comps``, ``_zillow_models``, and
``_realestate_helpers``. Per-card / per-fragment branches live in
those sibling modules so this file stays under code-quality thresholds
and never catches a bare ``Exception``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional
from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import _zillow_comps as zcomps
from . import _zillow_helpers as zh
from ._realestate_helpers import (
    is_captcha_blocked,
    random_delay_ms,
)
from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)
from ._zillow_models import (
    PriceHistoryItem,
    ZillowCompsRequest,
    ZillowCompsResponse,
    ZillowListing,
    ZillowPropertyDetail,
    ZillowPropertyRequest,
    ZillowPropertyResponse,
    ZillowSearchRequest,
    ZillowSearchResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.zillow")
router = APIRouter(prefix="/scrape/zillow", tags=["zillow"])


# ---------------------------------------------------------------------------
# Card-payload mappers (per-row JS dict -> validated pydantic model)
# ---------------------------------------------------------------------------

def _to_zillow_listing(data: dict[str, Any]) -> Optional[ZillowListing]:
    """Validate one JS card row to a :class:`ZillowListing`, ``None`` on error."""
    try:
        return ZillowListing(
            address=data.get("address", ""),
            price=data.get("price"),
            beds=data.get("beds"),
            baths=data.get("baths"),
            sqft=data.get("sqft"),
            listing_url=data.get("url", ""),
            image_url=data.get("image_url"),
            status=None,
            days_on_market=None,
            zestimate=None,
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_listings(rows: List[dict[str, Any]], limit: int) -> List[ZillowListing]:
    """Map each JS card row to a model, skipping invalid ones."""
    out: List[ZillowListing] = []
    for row in rows[:limit]:
        listing = _to_zillow_listing(row)
        if listing is not None:
            out.append(listing)
    return out


# ---------------------------------------------------------------------------
# Search-page parser
# ---------------------------------------------------------------------------

_LISTING_WAIT_SELECTOR = (
    "article[data-test='property-card'], .list-card, "
    "[data-testid='property-card']"
)


async def _parse_search_listings(page: Any, limit: int) -> List[ZillowListing]:
    """Wait for cards, evaluate the JS payload, build typed listings."""
    try:
        await page.wait_for_selector(_LISTING_WAIT_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for listings to load")
        return []
    await random_delay_ms(500, 1000)
    rows = await page.evaluate(zh.LISTING_CARDS_JS)
    return _build_listings(rows or [], limit)


# ---------------------------------------------------------------------------
# Property-detail parsing
# ---------------------------------------------------------------------------

_DETAIL_WAIT_SELECTOR = (
    "h1, [data-testid='home-details'], .ds-address-container"
)


def _first_match_int(value: Any) -> Optional[int]:
    """Coerce a JS regex-match payload to an int (commas tolerated)."""
    if value is None:
        return None
    try:
        return int(value[0]) if value else None
    except (ValueError, TypeError, IndexError):
        return None


def _first_match_float(value: Any) -> Optional[float]:
    """Coerce a JS regex-match payload to a float."""
    if value is None:
        return None
    try:
        return float(value[0]) if value else None
    except (ValueError, TypeError, IndexError):
        return None


def _first_match_int_with_commas(value: Any) -> Optional[int]:
    """Coerce a JS regex-match payload (``["1,234"]``) to an int."""
    if value is None:
        return None
    try:
        return int(value[0].replace(",", "")) if value else None
    except (ValueError, TypeError, AttributeError, IndexError):
        return None


def _build_price_history(rows: List[dict[str, Any]]) -> List[PriceHistoryItem]:
    """Validate each price-history row, dropping ones that fail."""
    out: List[PriceHistoryItem] = []
    for row in rows:
        try:
            out.append(PriceHistoryItem(
                date=row.get("date", ""),
                event=row.get("event", ""),
                price=row.get("price") or None,
            ))
        except PARSE_EXCEPTIONS:
            continue
    return out


def _build_property_detail(data: dict[str, Any]) -> ZillowPropertyDetail:
    """Map the JS detail payload to a validated property model."""
    return ZillowPropertyDetail(
        address=data.get("address", ""),
        price=data.get("price") or None,
        beds=_first_match_int(data.get("beds")),
        baths=_first_match_float(data.get("baths")),
        sqft=_first_match_int_with_commas(data.get("sqft")),
        lot_size=None,
        year_built=data.get("year_built"),
        zestimate=data.get("zestimate") or None,
        price_history=_build_price_history(data.get("price_history", [])),
        tax_history=[],
        nearby_schools=[],
        listing_agent=None,
    )


async def _parse_property_detail(page: Any) -> ZillowPropertyDetail:
    """Wait for detail markup, evaluate JS, build the typed model."""
    await page.wait_for_selector(_DETAIL_WAIT_SELECTOR, timeout=20000)
    await random_delay_ms(800, 1500)
    data = await page.evaluate(zh.PROPERTY_DETAIL_JS)
    return _build_property_detail(data or {})


# ---------------------------------------------------------------------------
# Search entry point (param-bundle keeps the parameter-count limit)
# ---------------------------------------------------------------------------

def _search_failure(message: str, start: float) -> ZillowSearchResponse:
    """Build the canonical failure search response."""
    return ZillowSearchResponse(
        success=False,
        error=message,
        elapsed_ms=int((time.time() - start) * 1000),
    )


async def _do_search_zillow(
    params: zh.ZillowSearchParams,
) -> ZillowSearchResponse:
    """Run the search flow under the stealth context and return a response."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            search_url = zh.build_search_url(
                params.location,
                params.listing_type,
                params.min_price,
                params.max_price,
                params.min_beds,
            )
            logger.info("Searching Zillow: %s", search_url[:100])
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30000,
            )
            await random_delay_ms(1500, 2500)
            if await is_captcha_blocked(page):
                return _search_failure(
                    "blocked: challenge or environment-check", start,
                )
            listings = await _parse_search_listings(page, params.limit)
            elapsed = int((time.time() - start) * 1000)
            logger.info(
                "Found %d Zillow listings in %dms", len(listings), elapsed,
            )
            return ZillowSearchResponse(
                success=True,
                listings=listings,
                count=len(listings),
                elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Zillow search timeout: %s", exc)
        return _search_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Zillow browser error: %s", exc)
        return _search_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Zillow parse error: %s", exc)
        return _search_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Property entry point
# ---------------------------------------------------------------------------

def _resolve_property_url(url: Optional[str], address: Optional[str]) -> Optional[str]:
    """Return ``url`` if given, else build one from ``address``."""
    if url:
        return url
    if address:
        encoded = quote(address)
        return f"{zh.ZILLOW_BASE}/homes/{encoded}_rb/"
    return None


def _property_failure(message: str, start: float) -> ZillowPropertyResponse:
    """Build the canonical failure property response."""
    return ZillowPropertyResponse(
        success=False,
        error=message,
        elapsed_ms=int((time.time() - start) * 1000),
    )


async def _maybe_click_first_card(page: Any) -> None:
    """When we landed on a search results page, follow the first card."""
    try:
        await page.click(
            "article[data-test='property-card'] a, .list-card a", timeout=5000,
        )
    except (PlaywrightTimeoutError,) + PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("First-card click skipped (%s)", type(exc).__name__)
        return
    await random_delay_ms(1500, 2500)


async def _do_scrape_property(
    url: Optional[str], address: Optional[str],
) -> ZillowPropertyResponse:
    """Scrape one Zillow property page (URL- or address-driven)."""
    start = time.time()
    target_url = _resolve_property_url(url, address)
    if not target_url:
        return ZillowPropertyResponse(
            success=False, error="Either 'url' or 'address' is required",
        )
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            logger.info("Scraping Zillow property: %s", target_url[:100])
            await page.goto(
                target_url, wait_until="domcontentloaded", timeout=30000,
            )
            await random_delay_ms(2000, 3000)
            if await is_captcha_blocked(page):
                return _property_failure(
                    "blocked: challenge or environment-check", start,
                )
            if address and not url:
                await _maybe_click_first_card(page)
            detail = await _parse_property_detail(page)
            elapsed = int((time.time() - start) * 1000)
            logger.info(
                "Scraped property '%s' in %dms", detail.address[:40], elapsed,
            )
            return ZillowPropertyResponse(
                success=True, property=detail, elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Zillow property scrape timeout: %s", exc)
        return _property_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Zillow property browser error: %s", exc)
        return _property_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Zillow property parse error: %s", exc)
        return _property_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Comps entry point (delegates to ``_zillow_comps``)
# ---------------------------------------------------------------------------

def _comps_failure(message: str, start: float) -> ZillowCompsResponse:
    """Build the canonical failure comps response."""
    return ZillowCompsResponse(
        success=False,
        error=message,
        elapsed_ms=int((time.time() - start) * 1000),
    )


async def _do_get_comps(request: ZillowCompsRequest) -> ZillowCompsResponse:
    """Multi-step comps lookup -- delegates to the ``_zillow_comps`` module."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            return await zcomps.gather_comps(page, request, start)
    except PlaywrightTimeoutError as exc:
        logger.error("Zillow comps timeout: %s", exc)
        return _comps_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Zillow comps browser error: %s", exc)
        return _comps_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Zillow comps parse error: %s", exc)
        return _comps_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Wrappers that protect each handler against asyncio cancellation
# ---------------------------------------------------------------------------

async def _wrap_search(
    request: ZillowSearchRequest,
) -> ZillowSearchResponse:
    """Build :class:`ZillowSearchParams` and run with a 60s timeout."""
    params = zh.ZillowSearchParams(
        location=request.location,
        listing_type=request.listing_type,
        min_price=request.min_price,
        max_price=request.max_price,
        min_beds=request.min_beds,
        limit=request.limit,
    )
    try:
        return await asyncio.wait_for(_do_search_zillow(params), timeout=60)
    except asyncio.TimeoutError:
        logger.error("Zillow search request hit hard timeout")
        return ZillowSearchResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


async def _wrap_property(
    request: ZillowPropertyRequest,
) -> ZillowPropertyResponse:
    """Validate inputs and dispatch to ``_do_scrape_property``."""
    if not request.url and not request.address:
        return ZillowPropertyResponse(
            success=False, error="Either 'url' or 'address' is required",
        )
    try:
        return await asyncio.wait_for(
            _do_scrape_property(request.url, request.address), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Zillow property request hit hard timeout")
        return ZillowPropertyResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


async def _wrap_comps(
    request: ZillowCompsRequest,
) -> ZillowCompsResponse:
    """Run the comps flow under a 90s hard timeout."""
    try:
        return await asyncio.wait_for(_do_get_comps(request), timeout=90)
    except asyncio.TimeoutError:
        logger.error("Zillow comps request hit hard timeout")
        return ZillowCompsResponse(
            success=False, error="Request failed: timeout", elapsed_ms=90000,
        )


# ---------------------------------------------------------------------------
# FastAPI route handlers
# ---------------------------------------------------------------------------

@router.post(
    "/search",
    response_model=ZillowSearchResponse,
    summary="Search Zillow listings",
)
async def search_zillow(request: ZillowSearchRequest) -> ZillowSearchResponse:
    """Search Zillow listings by location and filters."""
    return await _wrap_search(request)


@router.post(
    "/property",
    response_model=ZillowPropertyResponse,
    summary="Scrape Zillow property detail",
)
async def scrape_zillow_property(
    request: ZillowPropertyRequest,
) -> ZillowPropertyResponse:
    """Scrape detailed information from a single Zillow property."""
    return await _wrap_property(request)


@router.post(
    "/comps",
    response_model=ZillowCompsResponse,
    summary="Get comparable sales for a property",
)
async def get_zillow_comps(
    request: ZillowCompsRequest,
) -> ZillowCompsResponse:
    """Get recently-sold comparable properties for an address."""
    return await _wrap_comps(request)
