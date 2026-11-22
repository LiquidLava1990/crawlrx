"""trulia.py -- Trulia real-estate scraper.

Router prefix: ``/scrape/trulia``

* ``POST /search`` -- search Trulia listings
* ``POST /property`` -- scrape one Trulia property page

Thin orchestrator over the helpers in ``_trulia_helpers`` /
``_realestate_helpers`` / ``_scraper_helpers``. JS payloads, URL
builder and ``TruliaSearchParams`` bundle live in the helper module so
this file stays under the code-quality thresholds and never catches a
bare ``Exception``.
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
from pydantic import BaseModel, Field

from . import _trulia_helpers as trh
from ._realestate_helpers import random_delay_ms
from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.trulia")
router = APIRouter(prefix="/scrape/trulia", tags=["trulia"])


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class TruliaListing(BaseModel):
    """Single Trulia property listing."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    listing_url: str
    image_url: Optional[str] = None
    days_on_market: Optional[str] = None
    property_type: Optional[str] = None


class SchoolRating(BaseModel):
    """School rating information."""

    name: str
    rating: Optional[str] = None
    distance: Optional[str] = None
    level: Optional[str] = None


class TruliaPropertyDetail(BaseModel):
    """Detailed Trulia property information."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[str] = None
    description: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    school_ratings: List[SchoolRating] = Field(default_factory=list)
    walk_score: Optional[str] = None
    neighborhood_stats: Dict[str, Any] = Field(default_factory=dict)


class TruliaSearchRequest(BaseModel):
    """Request model for ``POST /scrape/trulia/search``."""

    location: str = Field(..., description="Location to search")
    listing_type: str = Field(default="for_sale")
    min_price: Optional[int] = Field(default=None)
    max_price: Optional[int] = Field(default=None)
    min_beds: Optional[int] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=40)


class TruliaSearchResponse(BaseModel):
    """Response model for ``POST /scrape/trulia/search``."""

    success: bool
    listings: List[TruliaListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class TruliaPropertyRequest(BaseModel):
    """Request model for ``POST /scrape/trulia/property``."""

    url: str = Field(..., description="Full Trulia property URL")


class TruliaPropertyResponse(BaseModel):
    """Response model for ``POST /scrape/trulia/property``."""

    success: bool
    property: Optional[TruliaPropertyDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Card-payload mappers
# ---------------------------------------------------------------------------

def _opt(data: Dict[str, Any], key: str) -> Optional[Any]:
    """Return ``data[key]`` if truthy, else ``None`` (back-compat shim)."""
    return data.get(key) or None


def _to_listing(data: Dict[str, Any]) -> Optional[TruliaListing]:
    """Validate one card row, ``None`` on schema error."""
    try:
        return TruliaListing(
            address=data.get("address", ""),
            price=data.get("price"),
            beds=data.get("beds"),
            baths=data.get("baths"),
            sqft=data.get("sqft"),
            listing_url=data.get("url", ""),
            image_url=data.get("image_url"),
            days_on_market=data.get("days_on_market"),
            property_type=data.get("property_type"),
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_listings(rows: List[Dict[str, Any]], limit: int) -> List[TruliaListing]:
    """Map each card row to a validated listing, dropping invalid ones."""
    out: List[TruliaListing] = []
    for row in rows[:limit]:
        listing = _to_listing(row)
        if listing is not None:
            out.append(listing)
    return out


# ---------------------------------------------------------------------------
# JS-match coercers (Trulia returns regex-match arrays for numeric fields)
# ---------------------------------------------------------------------------

def _first_int(value: Any) -> Optional[int]:
    """Coerce a JS regex-match payload to an int."""
    if not value:
        return None
    try:
        return int(value[0])
    except (ValueError, TypeError, IndexError):
        return None


def _first_float(value: Any) -> Optional[float]:
    """Coerce a JS regex-match payload to a float."""
    if not value:
        return None
    try:
        return float(value[0])
    except (ValueError, TypeError, IndexError):
        return None


def _first_int_with_commas(value: Any) -> Optional[int]:
    """Coerce a JS regex-match payload (``["1,234"]``) to an int."""
    if not value:
        return None
    try:
        return int(value[0].replace(",", ""))
    except (ValueError, TypeError, AttributeError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _build_school_ratings(rows: List[Dict[str, Any]]) -> List[SchoolRating]:
    """Validate each school-rating row, dropping ones that fail."""
    out: List[SchoolRating] = []
    for row in rows:
        try:
            out.append(SchoolRating(
                name=row.get("name", ""),
                rating=_opt(row, "rating"),
                distance=_opt(row, "distance"),
                level=_opt(row, "level"),
            ))
        except PARSE_EXCEPTIONS:
            continue
    return out


def _build_property_detail(data: Dict[str, Any]) -> TruliaPropertyDetail:
    """Map the JS detail payload to a validated property model (no _opt indirection)."""
    return TruliaPropertyDetail(
        address=data.get("address", ""),
        price=data.get("price") or None,
        beds=_first_int(data.get("beds")),
        baths=_first_float(data.get("baths")),
        sqft=_first_int_with_commas(data.get("sqft")),
        year_built=data.get("year_built") or None,
        description=data.get("description") or None,
        features=data.get("features", []),
        school_ratings=_build_school_ratings(data.get("school_ratings", [])),
        walk_score=data.get("walk_score") or None,
        neighborhood_stats=data.get("neighborhood_stats", {}),
    )


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

async def _parse_search_listings(page: Any, limit: int) -> List[TruliaListing]:
    """Wait for the SRP, evaluate the JS payload, build typed listings."""
    try:
        await page.wait_for_selector(trh.LISTING_WAIT, timeout=15000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Trulia listings to load")
        return []
    await random_delay_ms(500, 1000)
    rows = await page.evaluate(trh.LISTING_CARDS_JS)
    return _build_listings(rows or [], limit)


async def _parse_property_detail(page: Any) -> TruliaPropertyDetail:
    """Wait for detail markup, evaluate JS, build the typed model."""
    await page.wait_for_selector(trh.DETAIL_WAIT, timeout=20000)
    await random_delay_ms(800, 1500)
    data = await page.evaluate(trh.PROPERTY_DETAIL_JS)
    return _build_property_detail(data or {})


# ---------------------------------------------------------------------------
# Failure-payload builders
# ---------------------------------------------------------------------------

def _elapsed_ms(start: float) -> int:
    """Return the wall-clock millisecond delta since ``start``."""
    return int((time.time() - start) * 1000)


def _search_failure(message: str, start: float) -> TruliaSearchResponse:
    """Build the canonical search-failure response."""
    return TruliaSearchResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


def _property_failure(message: str, start: float) -> TruliaPropertyResponse:
    """Build the canonical property-failure response."""
    return TruliaPropertyResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


# ---------------------------------------------------------------------------
# Main async entry points
# ---------------------------------------------------------------------------

async def _do_search_trulia(
    params: trh.TruliaSearchParams,
) -> TruliaSearchResponse:
    """Run the Trulia search flow under stealth context."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            search_url = trh.build_search_url(params)
            logger.info("Searching Trulia: %s", search_url[:100])
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30000,
            )
            await random_delay_ms(1500, 2500)
            listings = await _parse_search_listings(page, params.limit)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Found %d Trulia listings in %dms", len(listings), elapsed,
            )
            return TruliaSearchResponse(
                success=True,
                listings=listings,
                count=len(listings),
                elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Trulia search timeout: %s", exc)
        return _search_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Trulia browser error: %s", exc)
        return _search_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Trulia parse error: %s", exc)
        return _search_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _do_scrape_property(url: str) -> TruliaPropertyResponse:
    """Scrape one Trulia property URL."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            logger.info("Scraping Trulia property: %s", url[:100])
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await random_delay_ms(2000, 3000)
            detail = await _parse_property_detail(page)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Scraped property '%s' in %dms", detail.address[:40], elapsed,
            )
            return TruliaPropertyResponse(
                success=True, property=detail, elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Trulia property scrape timeout: %s", exc)
        return _property_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Trulia property browser error: %s", exc)
        return _property_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Trulia property parse error: %s", exc)
        return _property_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Wrappers protect handlers against asyncio cancellation
# ---------------------------------------------------------------------------

async def _wrap_search(request: TruliaSearchRequest) -> TruliaSearchResponse:
    """Build :class:`TruliaSearchParams` and run with a 60s timeout."""
    params = trh.TruliaSearchParams(
        location=request.location,
        listing_type=request.listing_type,
        min_price=request.min_price,
        max_price=request.max_price,
        min_beds=request.min_beds,
        limit=request.limit,
    )
    try:
        return await asyncio.wait_for(_do_search_trulia(params), timeout=60)
    except asyncio.TimeoutError:
        logger.error("Trulia search request hit hard timeout")
        return TruliaSearchResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


async def _wrap_property(
    request: TruliaPropertyRequest,
) -> TruliaPropertyResponse:
    """Validate and run the property scrape with a 60s timeout."""
    if not request.url:
        return TruliaPropertyResponse(
            success=False, error="'url' is required",
        )
    try:
        return await asyncio.wait_for(
            _do_scrape_property(request.url), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Trulia property request hit hard timeout")
        return TruliaPropertyResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


# ---------------------------------------------------------------------------
# FastAPI route handlers
# ---------------------------------------------------------------------------

@router.post(
    "/search",
    response_model=TruliaSearchResponse,
    summary="Search Trulia listings",
)
async def search_trulia(request: TruliaSearchRequest) -> TruliaSearchResponse:
    """Search Trulia listings by location and filters."""
    return await _wrap_search(request)


@router.post(
    "/property",
    response_model=TruliaPropertyResponse,
    summary="Scrape Trulia property detail",
)
async def scrape_trulia_property(
    request: TruliaPropertyRequest,
) -> TruliaPropertyResponse:
    """Scrape detailed information from a single Trulia property."""
    return await _wrap_property(request)
