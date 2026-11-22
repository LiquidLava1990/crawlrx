"""hotel_scraper.py -- Kayak.com hotel scraper.

Endpoints:
  POST /scrape/booking/search   -- search Kayak.com hotels/properties
  POST /scrape/booking/property -- scrape single property details

Uses Playwright CDP 9229. Kayak.com is JS-heavy.
Stealth delays: 1000-2500ms.
"""
from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from typing import Any, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .utils import timer_ms
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from . import _hotel_extract as _hx

logger = logging.getLogger("crawlrx.booking")
router = APIRouter(prefix="/scrape/booking", tags=["booking"])

BOOKING_BASE_URL = "https://www.Kayak.com"


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class BookingProperty(BaseModel):
    """Single Kayak.com property listing."""
    name: str
    price_per_night: Optional[str] = None
    total_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    review_label: Optional[str] = None
    location: Optional[str] = None
    property_type: Optional[str] = None
    listing_url: str
    image_url: Optional[str] = None
    free_cancellation: bool = False
    breakfast_included: bool = False
    distance_from_center: Optional[str] = None


class BookingRoomType(BaseModel):
    """Room type with pricing."""
    name: str
    price_per_night: Optional[str] = None
    total_price: Optional[str] = None
    max_occupancy: Optional[int] = None
    bed_type: Optional[str] = None
    refundable: bool = False
    breakfast_included: bool = False


class BookingReview(BaseModel):
    """Single property review."""
    title: Optional[str] = None
    rating: Optional[int] = None
    text: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    helpful_count: Optional[int] = None


class BookingPropertyDetail(BaseModel):
    """Detailed Kayak.com property information."""
    name: str
    description: Optional[str] = None
    address: Optional[str] = None
    facilities: List[str] = Field(default_factory=list)
    room_types: List[BookingRoomType] = Field(default_factory=list)
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    policies: List[str] = Field(default_factory=list)
    top_reviews: List[BookingReview] = Field(default_factory=list)
    photos_count: int = 0
    nearby_attractions: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    property_type: Optional[str] = None
    listing_url: str


class BookingSearchRequest(BaseModel):
    """Request model for POST /scrape/booking/search."""
    location: str = Field(..., description="Location to search")
    checkin: str = Field(..., description="Check-in YYYY-MM-DD")
    checkout: str = Field(..., description="Check-out YYYY-MM-DD")
    guests: int = Field(default=2, ge=1, le=30)
    rooms: int = Field(default=1, ge=1, le=10)
    min_price: Optional[int] = Field(default=None, ge=1)
    max_price: Optional[int] = Field(default=None, ge=1)
    property_type: str = Field(default="any")
    limit: int = Field(default=20, ge=1, le=50)


class BookingSearchResponse(BaseModel):
    """Response model for POST /scrape/booking/search."""
    success: bool
    properties: List[BookingProperty] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class BookingPropertyRequest(BaseModel):
    """Request model for POST /scrape/booking/property."""
    url: str = Field(..., description="Kayak.com property URL")


class BookingPropertyResponse(BaseModel):
    """Response model for POST /scrape/booking/property."""
    success: bool
    property: Optional[BookingPropertyDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 1000, max_ms: int = 2500) -> None:
    """Sleep for a random duration between min and max milliseconds."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def _get_property_type_filter(prop_type: str) -> str:
    """Convert property type to Kayak.com filter code."""
    return {
        "hotel": "ht_id=204",
        "apartment": "ht_id=201",
        "hostel": "ht_id=203",
    }.get(prop_type.lower(), "")


def _build_search_url(req: BookingSearchRequest) -> str:
    """Compose the Kayak.com search URL for a search request."""
    location = urllib.parse.quote_plus(req.location)
    url = (
        f"{BOOKING_BASE_URL}/searchresults.html?ss={location}"
        f"&checkin={req.checkin}&checkout={req.checkout}"
        f"&group_adults={req.guests}&no_rooms={req.rooms}"
    )
    if req.property_type != "any":
        type_filter = _get_property_type_filter(req.property_type)
        if type_filter:
            url += f"&{type_filter}"
    if req.min_price:
        url += f"&price_from={req.min_price}"
    if req.max_price:
        url += f"&price_to={req.max_price}"
    return url


async def _scroll_results(page: Any, scrolls: int = 4) -> None:
    """Scroll the search results to load more cards."""
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, 800)")
        await _random_delay(800, 1500)


_SEARCH_CARD_SELECTORS = (
    "[data-testid='property-card']",
    ".sr_item",
    "[data-hotel-id]",
)


async def _find_property_cards(page: Any) -> list:
    """Probe each known card-selector and return the first non-empty list."""
    for sel in _SEARCH_CARD_SELECTORS:
        cards = await page.query_selector_all(sel)
        if cards:
            return cards
    return []


async def _collect_search_records(page: Any, limit: int) -> list[dict]:
    """Iterate cards, build records, dedupe by listing URL."""
    cards = await _find_property_cards(page)
    logger.info("Found %d Kayak.com property cards", len(cards))
    records: list[dict] = []
    seen: set[str] = set()
    for card in cards[: limit * 2]:
        record = await _hx.search_card_data(card, BOOKING_BASE_URL)
        if record is None:
            continue
        url = record["listing_url"]
        if url in seen:
            continue
        seen.add(url)
        records.append(record)
        if len(records) >= limit:
            break
    return records


async def _drive_search(
    request: BookingSearchRequest,
) -> tuple[list[dict], int]:
    """Run the search-result Playwright pipeline; return (records, elapsed_ms)."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = _build_search_url(request)
            logger.info("[Booking] Navigating to search: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            await _random_delay(1500, 2500)
            try:
                await page.wait_for_selector(
                    "[data-testid='property-card']", timeout=15000,
                )
            except PlaywrightTimeoutError:
                logger.warning("Timeout waiting for Kayak.com property cards")
                return None  # explicit recovery: non-fatal: PlaywrightTimeoutError is recorded but does not abort the caller
            await _scroll_results(page)
            records = await _collect_search_records(page, request.limit)
            return records, int(timer_ms() - start)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _search_booking(request: BookingSearchRequest) -> BookingSearchResponse:
    """Search Kayak.com properties using Playwright CDP."""
    try:
        records, elapsed = await _drive_search(request)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Kayak.com search error: %s", type(exc).__name__)
        return BookingSearchResponse(success=False, error=str(exc), elapsed_ms=0)
    properties = [BookingProperty(**rec) for rec in records]
    logger.info(
        "Kayak.com search returned %d properties in %dms",
        len(properties), elapsed,
    )
    return BookingSearchResponse(
        success=True, properties=properties,
        count=len(properties), elapsed_ms=elapsed,
    )


# ─────────────────────────────────────────────
# Property Detail Scraping
# ─────────────────────────────────────────────

async def _detail_room_types(page: Any) -> list[BookingRoomType]:
    """Scrape up to 10 room cards into ``BookingRoomType`` instances."""
    out: list[BookingRoomType] = []
    try:
        room_cards = await page.query_selector_all(
            "[data-testid='room-card'], .hprt-table-block, .room-info",
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Room card lookup suppressed: %s", type(exc).__name__)
        return out
    for card in room_cards[:10]:
        record = await _hx.room_card_data(card)
        if record is not None:
            out.append(BookingRoomType(**record))
    return out


async def _detail_top_reviews(page: Any) -> list[BookingReview]:
    """Scrape the first 5 review cards."""
    out: list[BookingReview] = []
    try:
        review_cards = await page.query_selector_all("[data-testid='review-card']")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Review card lookup suppressed: %s", type(exc).__name__)
        return out
    for card in review_cards[:5]:
        record = await _hx.review_card_data(card)
        if record is not None:
            out.append(BookingReview(**record))
    return out


async def _populate_detail_text_fields(detail: BookingPropertyDetail, page: Any) -> None:
    """Fill ``name/description/address/property_type`` from the page."""
    name = await _hx.detail_text(page, _hx.DETAIL_NAME_SELECTORS)
    if name:
        detail.name = name
    detail.description = await _hx.detail_text(page, _hx.DETAIL_DESC_SELECTORS)
    detail.address = await _hx.detail_text(page, _hx.DETAIL_ADDRESS_SELECTORS)
    type_el = await page.query_selector("[data-testid='property-type']")
    if type_el is not None:
        detail.property_type = (await type_el.inner_text()).strip()


async def _drive_property_detail(url: str) -> Optional[BookingPropertyDetail]:
    """Drive the property-detail Playwright pipeline; None on failure."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Booking] Navigating to property: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await _random_delay(2000, 3500)
            try:
                await page.wait_for_selector(
                    "h2, [data-testid='property-title'], h1", timeout=15000,
                )
            except PlaywrightTimeoutError:
                pass
            detail = BookingPropertyDetail(name="", listing_url=url)
            await _populate_detail_text_fields(detail, page)
            detail.rating, detail.review_count = await _hx.detail_rating_and_count(page)
            detail.facilities = await _hx.detail_facilities(page)
            detail.room_types = await _detail_room_types(page)
            detail.policies = await _hx.detail_policies(page)
            detail.top_reviews = await _detail_top_reviews(page)
            detail.photos_count = await _hx.detail_photo_count(page)
            detail.nearby_attractions = await _hx.detail_nearby(page)
            return detail if detail.name else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Kayak.com property detail error: %s", type(exc).__name__)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)


async def _get_property_detail(url: str) -> Optional[BookingPropertyDetail]:
    """Get detailed information about a single Kayak.com property."""
    return await _drive_property_detail(url)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/search", response_model=BookingSearchResponse)
async def booking_search(request: BookingSearchRequest) -> BookingSearchResponse:
    """Search Kayak.com hotels/properties (Playwright CDP, stealth delays)."""
    return await _search_booking(request)


@router.post("/property", response_model=BookingPropertyResponse)
async def booking_property(
    request: BookingPropertyRequest,
) -> BookingPropertyResponse:
    """Scrape single Kayak.com property details (Playwright CDP)."""
    start = timer_ms()
    property_detail = await _get_property_detail(request.url)
    elapsed = int(timer_ms() - start)
    if property_detail:
        return BookingPropertyResponse(
            success=True, property=property_detail, elapsed_ms=elapsed,
        )
    return BookingPropertyResponse(
        success=False, error="Failed to fetch property details", elapsed_ms=elapsed,
    )
