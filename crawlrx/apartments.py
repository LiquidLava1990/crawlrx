"""apartments.py -- Apartments.com listing scraper.

Router prefix: ``/scrape/apartments``

* ``POST /search`` -- search Apartments.com listings
* ``POST /listing`` -- scrape a single apartment-complex detail page

Thin orchestrator over the helpers in ``_apartments_helpers``,
``_apartments_models``, ``_realestate_helpers`` and ``_scraper_helpers``.
JS payloads, URL builder and ``ApartmentsSearchParams`` live in the
helper module so this file stays under code-quality thresholds and never
catches a bare ``Exception``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import _apartments_helpers as aph
from ._apartments_models import (
    ApartmentDetail,
    ApartmentListing,
    ApartmentsListingRequest,
    ApartmentsListingResponse,
    ApartmentsSearchRequest,
    ApartmentsSearchResponse,
    FloorPlan,
    Review,
)
from ._realestate_helpers import (
    DEFAULT_USER_AGENTS,
    opt_value as _opt,
    random_delay_ms,
)
from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.apartments")
router = APIRouter(prefix="/scrape/apartments", tags=["apartments"])


# ---------------------------------------------------------------------------
# Card-payload mappers (per-row JS dict -> validated pydantic model)
# ---------------------------------------------------------------------------

def _to_listing(data: Dict[str, Any]) -> Optional[ApartmentListing]:
    """Validate one card row, ``None`` on schema error.

    Inlines ``data.get(key) or None`` into a single per-key local so the
    hot path doesn't pay 8x function-call overhead for the simple coalesce.
    """
    try:
        return ApartmentListing(
            name=data.get("name", ""),
            address=data.get("address") or None,
            price_range=data.get("price_range") or None,
            beds_range=data.get("beds_range") or None,
            baths_range=data.get("baths_range") or None,
            sqft_range=data.get("sqft_range") or None,
            rating=aph.parse_rating(data.get("rating_text", "")),
            review_count=aph.parse_number(data.get("review_text", "")),
            listing_url=data.get("url", ""),
            image_url=data.get("image_url") or None,
            amenities_summary=data.get("amenities_summary") or None,
            available_now=data.get("available_now"),
            pet_policy=data.get("pet_policy") or None,
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_listings(
    rows: List[Dict[str, Any]], limit: int,
) -> List[ApartmentListing]:
    """Map each card row to a validated listing, dropping invalid ones."""
    out: List[ApartmentListing] = []
    for row in rows[:limit]:
        listing = _to_listing(row)
        if listing is not None:
            out.append(listing)
    return out


# ---------------------------------------------------------------------------
# Detail-page assemblers (floor plans, reviews, contact info)
# ---------------------------------------------------------------------------

def _build_floor_plan(fp: Dict[str, Any]) -> Optional[FloorPlan]:
    """Map a single JS floor-plan blob to a typed model."""
    try:
        return FloorPlan(
            name=_opt(fp, "name"),
            beds=aph.parse_float_field(fp.get("beds", "")),
            baths=aph.parse_float_field(fp.get("baths", "")),
            sqft=aph.parse_sqft_field(fp.get("sqft", "")),
            price=_opt(fp, "price"),
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_floor_plans(rows: List[Dict[str, Any]]) -> List[FloorPlan]:
    """Validate each floor-plan row, dropping ones that fail."""
    out: List[FloorPlan] = []
    for fp in rows:
        plan = _build_floor_plan(fp)
        if plan is not None:
            out.append(plan)
    return out


def _build_review(rev: Dict[str, Any]) -> Optional[Review]:
    """Map a single JS review blob to a typed model."""
    try:
        return Review(
            author=_opt(rev, "author"),
            rating=aph.parse_rating(rev.get("rating", "")),
            text=_opt(rev, "text"),
            date=_opt(rev, "date"),
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_reviews(rows: List[Dict[str, Any]]) -> List[Review]:
    """Validate each review row, dropping ones that fail."""
    out: List[Review] = []
    for rev in rows:
        review = _build_review(rev)
        if review is not None:
            out.append(review)
    return out


def _build_contact_info(phone: Optional[str]) -> Optional[Dict[str, str]]:
    """Wrap a phone string in the contact-info dict, or return ``None``."""
    if not phone:
        return None
    return {"phone": phone}


def _build_apartment_detail(data: Dict[str, Any]) -> ApartmentDetail:
    """Map the JS detail payload to a validated apartment model."""
    return ApartmentDetail(
        name=data.get("name", ""),
        address=_opt(data, "address"),
        price_range=_opt(data, "price_range"),
        floor_plans=_build_floor_plans(data.get("floor_plans", [])),
        amenities=data.get("amenities", []),
        pet_policy=_opt(data, "pet_policy"),
        parking=_opt(data, "parking"),
        utilities_included=_opt(data, "utilities_included"),
        lease_terms=_opt(data, "lease_terms"),
        contact_info=_build_contact_info(data.get("phone")),
        photos_count=aph.parse_number(data.get("photos_count_text", "")),
        reviews=_build_reviews(data.get("reviews", [])),
        rating=aph.parse_rating(data.get("rating_text", "")),
        review_count=aph.parse_number(data.get("review_count_text", "")),
        website=_opt(data, "website"),
        phone=_opt(data, "phone"),
    )


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

async def _parse_search_listings(
    page: Any, limit: int,
) -> List[ApartmentListing]:
    """Wait for the SRP, evaluate the JS payload, build typed listings."""
    try:
        await page.wait_for_selector(aph.LISTING_WAIT, timeout=20000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for listings to load")
        return []
    await random_delay_ms(800, 1500)
    rows = await page.evaluate(aph.LISTING_CARDS_JS)
    return _build_listings(rows or [], limit)


async def _parse_listing_detail(page: Any) -> ApartmentDetail:
    """Wait for detail markup, evaluate JS, build the typed model."""
    await page.wait_for_selector(aph.DETAIL_WAIT, timeout=20000)
    await random_delay_ms(1000, 2000)
    data = await page.evaluate(aph.LISTING_DETAIL_JS)
    return _build_apartment_detail(data or {})


# ---------------------------------------------------------------------------
# Browser context (Apartments.com uses a plain Playwright context)
# ---------------------------------------------------------------------------

async def _open_browser_and_page(pw: Any) -> tuple[Any, Any, Any, bool]:
    """Launch chromium and return ``(browser, context, page, owned_context)``."""
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    contexts = browser.contexts
    if contexts:
        context = contexts[0]
        owned_context = False
    else:
        context = await browser.new_context(
            user_agent=random.choice(DEFAULT_USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
        )
        owned_context = True
    page = await context.new_page()
    return browser, context, page, owned_context


async def safe_close_page(page: "Page") -> None:
    """Close ``page`` swallowing only known browser teardown failures."""
    if page is None:
        return
    try:
        await page.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Page cleanup suppressed (%s)", type(exc).__name__)


async def safe_close_context(context: "BrowserContext | None", owned: bool) -> None:
    """Close ``context`` only if we created it -- swallow teardown errors."""
    if not owned or context is None:
        return
    try:
        await context.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Context cleanup suppressed (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# Failure-payload builders
# ---------------------------------------------------------------------------

def _elapsed_ms(start: float) -> int:
    """Return the wall-clock millisecond delta since ``start``."""
    return int((time.time() - start) * 1000)


def _search_failure(message: str, start: float) -> ApartmentsSearchResponse:
    """Build the canonical search-failure response."""
    return ApartmentsSearchResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


def _listing_failure(message: str, start: float) -> ApartmentsListingResponse:
    """Build the canonical listing-failure response."""
    return ApartmentsListingResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


# ---------------------------------------------------------------------------
# Main async entry points (share the page lifecycle via _run_under_context)
# ---------------------------------------------------------------------------

async def _run_under_context(
    operation: Any,
    operation_label: str,
    on_failure: Any,
    start: float,
) -> Any:
    """Open a chromium context, hand the page to ``operation``, then close.

    ``operation(page)`` is awaited inside the try block. Any narrow
    Playwright / scrape error is logged with ``operation_label`` and
    converted to a failure-response via ``on_failure(message, start)``.
    """
    page = context = None
    owned_context = False
    try:
        async with async_playwright() as pw:
            _, context, page, owned_context = await _open_browser_and_page(pw)
            return await operation(page)
    except PlaywrightTimeoutError as exc:
        logger.error("%s timeout: %s", operation_label, exc)
        return on_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("%s browser error: %s", operation_label, exc)
        return on_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("%s parse error: %s", operation_label, exc)
        return on_failure(str(exc), start)
    finally:
        await safe_close_page(page)
        await safe_close_context(context, owned_context)


async def _search_on_page(
    page: Any, params: aph.ApartmentsSearchParams, start: float,
) -> ApartmentsSearchResponse:
    """Run the search-flow steps on an already-open Playwright page."""
    search_url = aph.build_search_url(params)
    logger.info("Searching Apartments.com: %s", search_url[:100])
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await random_delay_ms(1500, 2500)
    listings = await _parse_search_listings(page, params.limit)
    elapsed = _elapsed_ms(start)
    logger.info(
        "Found %d apartment listings in %dms", len(listings), elapsed,
    )
    return ApartmentsSearchResponse(
        success=True,
        listings=listings,
        count=len(listings),
        elapsed_ms=elapsed,
    )


async def _listing_on_page(
    page: Any, url: str, start: float,
) -> ApartmentsListingResponse:
    """Run the listing-detail steps on an already-open Playwright page."""
    logger.info("Scraping Apartments.com listing: %s", url[:100])
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await random_delay_ms(2000, 3000)
    detail = await _parse_listing_detail(page)
    elapsed = _elapsed_ms(start)
    logger.info("Scraped property '%s' in %dms", detail.name[:40], elapsed)
    return ApartmentsListingResponse(
        success=True, property=detail, elapsed_ms=elapsed,
    )


async def _do_search_apartments(
    params: aph.ApartmentsSearchParams,
) -> ApartmentsSearchResponse:
    """Search Apartments.com under a fresh chromium context."""
    start = time.time()
    return await _run_under_context(
        lambda page: _search_on_page(page, params, start),
        "Apartments.com search",
        _search_failure,
        start,
    )


async def _do_scrape_listing(url: str) -> ApartmentsListingResponse:
    """Scrape one Apartments.com listing URL."""
    start = time.time()
    return await _run_under_context(
        lambda page: _listing_on_page(page, url, start),
        "Apartments.com listing scrape",
        _listing_failure,
        start,
    )


# ---------------------------------------------------------------------------
# Wrappers protect handlers against asyncio cancellation
# ---------------------------------------------------------------------------

async def _wrap_search(
    request: ApartmentsSearchRequest,
) -> ApartmentsSearchResponse:
    """Build :class:`ApartmentsSearchParams` and run with a 60s timeout."""
    params = aph.ApartmentsSearchParams(
        location=request.location,
        min_price=request.min_price,
        max_price=request.max_price,
        min_beds=request.min_beds,
        max_beds=request.max_beds,
        pet_friendly=request.pet_friendly,
        limit=request.limit,
    )
    try:
        return await asyncio.wait_for(
            _do_search_apartments(params), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Apartments.com search request hit hard timeout")
        return ApartmentsSearchResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


async def _wrap_listing(
    request: ApartmentsListingRequest,
) -> ApartmentsListingResponse:
    """Validate and run the listing scrape with a 60s timeout."""
    if not request.url:
        return ApartmentsListingResponse(
            success=False, error="'url' is required", elapsed_ms=0,
        )
    try:
        return await asyncio.wait_for(
            _do_scrape_listing(request.url), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Apartments.com listing request hit hard timeout")
        return ApartmentsListingResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


# ---------------------------------------------------------------------------
# FastAPI route handlers
# ---------------------------------------------------------------------------

@router.post(
    "/search",
    response_model=ApartmentsSearchResponse,
    summary="Search Apartments.com listings",
)
async def search_apartments(
    request: ApartmentsSearchRequest,
) -> ApartmentsSearchResponse:
    """Search Apartments.com listings by location and filters."""
    return await _wrap_search(request)


@router.post(
    "/listing",
    response_model=ApartmentsListingResponse,
    summary="Scrape Apartments.com listing detail",
)
async def scrape_apartments_listing(
    request: ApartmentsListingRequest,
) -> ApartmentsListingResponse:
    """Scrape detailed information from a single Apartments.com listing."""
    return await _wrap_listing(request)
