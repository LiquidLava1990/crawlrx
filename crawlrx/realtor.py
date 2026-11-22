"""realtor.py -- Realtor.com real-estate scraper.

Router prefix: ``/scrape/realtor``

* ``POST /search`` -- search Realtor.com listings
* ``POST /property`` -- scrape one Realtor.com property page

Thin orchestrator over the existing helpers in ``_realtor_helpers``
and ``_realestate_helpers`` / ``_scraper_helpers``. The card / detail
JS payloads, the URL builder and the ``RealtorSearchParams`` bundle
all live in ``_realtor_helpers`` so this file stays under code-quality
thresholds and never catches a bare ``Exception``.
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

from . import _realtor_helpers as rth
from ._realestate_helpers import random_delay_ms
from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.realtor")
router = APIRouter(prefix="/scrape/realtor", tags=["realtor"])


# ---------------------------------------------------------------------------
# Pydantic Models (kept here -- the public surface other modules import)
# ---------------------------------------------------------------------------

class RealtorListing(BaseModel):
    """Single Realtor.com property listing."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    listing_url: str
    image_url: Optional[str] = None
    days_on_market: Optional[int] = None
    status: Optional[str] = None
    listing_id: Optional[str] = None
    agent_name: Optional[str] = None


class PriceHistoryItem(BaseModel):
    """Single price history entry."""

    date: str
    event: str
    price: Optional[str] = None
    source: Optional[str] = None


class TaxInfo(BaseModel):
    """Tax information."""

    year: Optional[int] = None
    taxes: Optional[str] = None
    assessment: Optional[str] = None


class SchoolRating(BaseModel):
    """School rating information."""

    name: str
    rating: Optional[int] = None
    distance: Optional[str] = None
    grades: Optional[str] = None


class OpenHouseDate(BaseModel):
    """Open house date / time."""

    date: str
    time: Optional[str] = None


class RealtorPropertyDetail(BaseModel):
    """Detailed Realtor.com property information."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_size: Optional[str] = None
    year_built: Optional[int] = None
    description: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    school_ratings: List[SchoolRating] = Field(default_factory=list)
    listing_agent: Optional[Dict[str, str]] = None
    open_house_dates: List[OpenHouseDate] = Field(default_factory=list)
    price_history: List[PriceHistoryItem] = Field(default_factory=list)
    tax_info: Optional[TaxInfo] = None


class RealtorSearchRequest(BaseModel):
    """Request model for ``POST /scrape/realtor/search``."""

    location: str = Field(..., description="Location to search")
    listing_type: str = Field(default="for_sale")
    min_price: Optional[int] = Field(default=None)
    max_price: Optional[int] = Field(default=None)
    min_beds: Optional[int] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=40)


class RealtorSearchResponse(BaseModel):
    """Response model for ``POST /scrape/realtor/search``."""

    success: bool
    listings: List[RealtorListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class RealtorPropertyRequest(BaseModel):
    """Request model for ``POST /scrape/realtor/property``."""

    url: str = Field(..., description="Full Realtor.com property URL")


class RealtorPropertyResponse(BaseModel):
    """Response model for ``POST /scrape/realtor/property``."""

    success: bool
    property: Optional[RealtorPropertyDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Card-payload mappers (per-row JS dict -> validated pydantic model)
# ---------------------------------------------------------------------------

def _opt(data: Dict[str, Any], key: str) -> Optional[str]:
    """Return ``data[key]`` if truthy, ``None`` otherwise (back-compat shim)."""
    return data.get(key) or None


def _to_listing(data: Dict[str, Any]) -> Optional[RealtorListing]:
    """Validate one card row (None on schema error); inlines _opt."""
    try:
        return RealtorListing(
            address=data.get("address", ""),
            price=data.get("price") or None,
            beds=data.get("beds"),
            baths=data.get("baths"),
            sqft=data.get("sqft"),
            listing_url=data.get("url", ""),
            image_url=data.get("image_url") or None,
            days_on_market=data.get("days_on_market"),
            status=data.get("status") or None,
            listing_id=data.get("listing_id"),
            agent_name=data.get("agent_name") or None,
        )
    except PARSE_EXCEPTIONS:
        return None


def _build_listings(rows: List[Dict[str, Any]], limit: int) -> List[RealtorListing]:
    """Map each card row to a validated listing, dropping invalid ones."""
    out: List[RealtorListing] = []
    for row in rows[:limit]:
        listing = _to_listing(row)
        if listing is not None:
            out.append(listing)
    return out


# ---------------------------------------------------------------------------
# Search-page parser
# ---------------------------------------------------------------------------

async def _parse_search_listings(page: Any, limit: int) -> List[RealtorListing]:
    """Wait for the SRP, evaluate the JS payload, build typed listings."""
    try:
        await page.wait_for_selector(rth.LISTING_WAIT, timeout=15000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for listings to load")
        return []
    await random_delay_ms(500, 1000)
    rows = await page.evaluate(rth.LISTING_CARDS_JS)
    return _build_listings(rows or [], limit)


# ---------------------------------------------------------------------------
# Property-detail parser
# ---------------------------------------------------------------------------

def _build_price_history(rows: List[Dict[str, Any]]) -> List[PriceHistoryItem]:
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


def _build_property_detail(data: Dict[str, Any]) -> RealtorPropertyDetail:
    """Map the JS detail payload to a validated property model."""
    agent_name = data.get("agent_name")
    listing_agent = {"name": agent_name} if agent_name else None
    return RealtorPropertyDetail(
        address=data.get("address", ""),
        price=_opt(data, "price"),
        beds=data.get("beds"),
        baths=data.get("baths"),
        sqft=data.get("sqft"),
        lot_size=_opt(data, "lot_size"),
        year_built=data.get("year_built"),
        description=_opt(data, "description"),
        features=data.get("features", []),
        school_ratings=[],
        listing_agent=listing_agent,
        open_house_dates=[],
        price_history=_build_price_history(data.get("price_history", [])),
        tax_info=None,
    )


async def _parse_property_detail(page: Any) -> RealtorPropertyDetail:
    """Wait for detail markup, evaluate JS, build the typed model."""
    await page.wait_for_selector(rth.DETAIL_WAIT, timeout=20000)
    await random_delay_ms(800, 1500)
    data = await page.evaluate(rth.PROPERTY_DETAIL_JS)
    return _build_property_detail(data or {})


# ---------------------------------------------------------------------------
# Failure-payload builders
# ---------------------------------------------------------------------------

def _elapsed_ms(start: float) -> int:
    """Return the wall-clock millisecond delta since ``start``."""
    return int((time.time() - start) * 1000)


def _search_failure(message: str, start: float) -> RealtorSearchResponse:
    """Build the canonical search-failure response."""
    return RealtorSearchResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


def _property_failure(message: str, start: float) -> RealtorPropertyResponse:
    """Build the canonical property-failure response."""
    return RealtorPropertyResponse(
        success=False, error=message, elapsed_ms=_elapsed_ms(start),
    )


# ---------------------------------------------------------------------------
# Main async entry points
# ---------------------------------------------------------------------------

async def _do_search_realtor(
    params: rth.RealtorSearchParams,
) -> RealtorSearchResponse:
    """Search Realtor.com via Playwright + stealth and return a response."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            search_url = rth.build_search_url(params)
            logger.info("Searching Realtor.com: %s", search_url[:100])
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30000,
            )
            await random_delay_ms(1500, 2500)
            listings = await _parse_search_listings(page, params.limit)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Found %d Realtor.com listings in %dms", len(listings), elapsed,
            )
            return RealtorSearchResponse(
                success=True,
                listings=listings,
                count=len(listings),
                elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Realtor.com search timeout: %s", exc)
        return _search_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Realtor.com browser error: %s", exc)
        return _search_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Realtor.com parse error: %s", exc)
        return _search_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _do_scrape_property(url: str) -> RealtorPropertyResponse:
    """Scrape one Realtor.com property URL."""
    start = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            logger.info("Scraping Realtor.com property: %s", url[:100])
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await random_delay_ms(2000, 3000)
            detail = await _parse_property_detail(page)
            elapsed = _elapsed_ms(start)
            logger.info(
                "Scraped property '%s' in %dms", detail.address[:40], elapsed,
            )
            return RealtorPropertyResponse(
                success=True, property=detail, elapsed_ms=elapsed,
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Realtor.com property scrape timeout: %s", exc)
        return _property_failure(f"Timeout: {exc}", start)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Realtor.com property browser error: %s", exc)
        return _property_failure(str(exc), start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Realtor.com property parse error: %s", exc)
        return _property_failure(str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Wrappers protect handlers against asyncio cancellation
# ---------------------------------------------------------------------------

async def _wrap_search(
    request: RealtorSearchRequest,
) -> RealtorSearchResponse:
    """Build :class:`RealtorSearchParams` and run with a 60s timeout."""
    params = rth.RealtorSearchParams(
        location=request.location,
        listing_type=request.listing_type,
        min_price=request.min_price,
        max_price=request.max_price,
        min_beds=request.min_beds,
        limit=request.limit,
    )
    try:
        return await asyncio.wait_for(_do_search_realtor(params), timeout=60)
    except asyncio.TimeoutError:
        logger.error("Realtor.com search request hit hard timeout")
        return RealtorSearchResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


async def _wrap_property(
    request: RealtorPropertyRequest,
) -> RealtorPropertyResponse:
    """Validate and run the property scrape with a 60s timeout."""
    if not request.url:
        return RealtorPropertyResponse(
            success=False, error="'url' is required",
        )
    try:
        return await asyncio.wait_for(
            _do_scrape_property(request.url), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Realtor.com property request hit hard timeout")
        return RealtorPropertyResponse(
            success=False, error="Request failed: timeout", elapsed_ms=60000,
        )


# ---------------------------------------------------------------------------
# FastAPI route handlers
# ---------------------------------------------------------------------------

@router.post(
    "/search",
    response_model=RealtorSearchResponse,
    summary="Search Realtor.com listings",
)
async def search_realtor(
    request: RealtorSearchRequest,
) -> RealtorSearchResponse:
    """Search Realtor.com listings by location and filters."""
    return await _wrap_search(request)


@router.post(
    "/property",
    response_model=RealtorPropertyResponse,
    summary="Scrape Realtor.com property detail",
)
async def scrape_realtor_property(
    request: RealtorPropertyRequest,
) -> RealtorPropertyResponse:
    """Scrape detailed information from a single Realtor.com property."""
    return await _wrap_property(request)
