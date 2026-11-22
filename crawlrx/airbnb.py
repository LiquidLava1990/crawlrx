"""airbnb.py -- Airbnb listing scraper.

Endpoints:
  POST /scrape/airbnb/search  -- search Airbnb listings
  POST /scrape/airbnb/listing -- scrape single listing details

Uses Playwright CDP. Stealth: 1000-2500ms delays + scroll. Field-level
parsers live in :mod:`._airbnb_helpers` (search) and
:mod:`._airbnb_detail` (single listing).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from playwright.async_api import (
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel, Field

from ._airbnb_detail import (
    HostFields,
    collect_amenities,
    collect_host_fields,
    collect_house_rules,
    collect_reviews,
    count_photos,
    parse_cancellation_policy,
    parse_detail_price,
    parse_room_details,
    safe_first_text,
)
from ._airbnb_helpers import (
    AIRBNB_BASE_URL,
    BROWSER_EXC,
    CARD_SELECTORS,
    CardFields,
    PROBE_EXC,
    SearchUrlSpec,
    absolute_listing_url,
    build_search_url,
    card_to_fields,
    close_browser_chain,
    extract_listing_id,
    launch_stealth_browser,
    parse_rating,
    parse_review_count,
    random_delay,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.airbnb")
router = APIRouter(prefix="/scrape/airbnb", tags=["airbnb"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class AirbnbListing(BaseModel):
    """Single Airbnb search result listing."""
    title: str
    price_per_night: Optional[str] = None
    total_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    location: Optional[str] = None
    listing_url: str
    image_url: Optional[str] = None
    property_type: Optional[str] = None
    amenities_summary: List[str] = Field(default_factory=list)
    superhost: bool = False
    listing_id: Optional[str] = None


class AirbnbHost(BaseModel):
    """Airbnb host information."""
    name: Optional[str] = None
    is_superhost: bool = False
    joined_date: Optional[str] = None
    response_rate: Optional[str] = None
    response_time: Optional[str] = None
    profile_url: Optional[str] = None


class AirbnbReview(BaseModel):
    """Single Airbnb review."""
    author: Optional[str] = None
    date: Optional[str] = None
    rating: Optional[int] = None
    text: Optional[str] = None


class AirbnbListingDetail(BaseModel):
    """Detailed Airbnb listing information."""
    title: str
    description: Optional[str] = None
    host: AirbnbHost = Field(default_factory=AirbnbHost)
    price_per_night: Optional[str] = None
    total_price: Optional[str] = None
    price_breakdown: Dict[str, str] = Field(default_factory=dict)
    amenities: List[str] = Field(default_factory=list)
    house_rules: List[str] = Field(default_factory=list)
    reviews: List[AirbnbReview] = Field(default_factory=list)
    location: Optional[str] = None
    cancellation_policy: Optional[str] = None
    photos_count: int = 0
    rating: Optional[float] = None
    review_count: Optional[int] = None
    property_type: Optional[str] = None
    guests: Optional[int] = None
    bedrooms: Optional[int] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    listing_url: str
    listing_id: Optional[str] = None


class AirbnbSearchRequest(BaseModel):
    """Request model for POST /scrape/airbnb/search."""
    location: str = Field(..., description="Location to search (city, address, etc.)")
    checkin: Optional[str] = Field(default=None, description="Check-in date (YYYY-MM-DD)")
    checkout: Optional[str] = Field(default=None, description="Check-out date (YYYY-MM-DD)")
    guests: int = Field(default=1, ge=1, le=16, description="Number of guests")
    min_price: Optional[int] = Field(default=None, ge=1, description="Minimum price per night")
    max_price: Optional[int] = Field(default=None, ge=1, description="Maximum price per night")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum listings to return")


class AirbnbListingRequest(BaseModel):
    """Request model for POST /scrape/airbnb/listing."""
    url: str = Field(..., description="Airbnb listing URL")


class AirbnbSearchResponse(BaseModel):
    """Response model for POST /scrape/airbnb/search."""
    success: bool
    listings: List[AirbnbListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class AirbnbListingResponse(BaseModel):
    """Response model for POST /scrape/airbnb/listing."""
    success: bool
    listing: Optional[AirbnbListingDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Search orchestration
# ─────────────────────────────────────────────

def _to_listing(fields: CardFields) -> AirbnbListing:
    """Convert :class:`CardFields` into the public ``AirbnbListing`` row."""
    return AirbnbListing(
        title=fields.title,
        price_per_night=fields.price_per_night,
        total_price=None,
        rating=fields.rating,
        review_count=fields.review_count,
        location=fields.location,
        listing_url=absolute_listing_url(fields.href),
        image_url=fields.image_url,
        property_type=fields.property_type,
        superhost=fields.superhost,
        listing_id=fields.listing_id,
    )


async def _wait_for_search_cards(page: Page) -> None:
    """Wait briefly for either of the two search-card markers to appear."""
    try:
        await page.wait_for_selector(
            "[data-testid='card-container'], [itemprop='itemListElement']",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Airbnb listings to load")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal


async def _scroll_for_more(page: Page) -> None:
    """Trigger lazy load by scrolling the search page a few times."""
    for _ in range(4):
        await page.evaluate("window.scrollBy(0, 800)")
        await random_delay(800, 1500)


async def _collect_search_cards(page: Page) -> List[Any]:
    """First non-empty card list across the selector ladder."""
    cards: List[Any] = []
    for selector in CARD_SELECTORS:
        cards = await page.query_selector_all(selector)
        if cards:
            break
    return cards


async def _build_search_listings(
    page: Page, limit: int,
) -> List[AirbnbListing]:
    """Walk search cards and build :class:`AirbnbListing` rows."""
    cards = await _collect_search_cards(page)
    logger.info("Found %d Airbnb listing cards", len(cards))

    seen: set[str] = set()
    out: List[AirbnbListing] = []
    for card in cards[: limit * 2]:
        try:
            fields = await card_to_fields(card)
        except PROBE_EXC as exc:
            logger.debug("Error parsing Airbnb listing card (%s): %s", type(exc).__name__, exc)
            continue
        if fields is None or fields.listing_id in seen:
            continue
        seen.add(fields.listing_id)
        out.append(_to_listing(fields))
        if len(out) >= limit:
            break
    return out


async def _search_airbnb_with_page(
    page: Page, request: AirbnbSearchRequest,
) -> List[AirbnbListing]:
    """Drive the Airbnb search page end-to-end."""
    spec = SearchUrlSpec(
        location=request.location,
        checkin=request.checkin,
        checkout=request.checkout,
        guests=request.guests,
        min_price=request.min_price,
        max_price=request.max_price,
    )
    search_url = build_search_url(spec)
    logger.info("[Airbnb] Navigating to search: %s", search_url)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await random_delay(1500, 3000)
    await _wait_for_search_cards(page)
    await _scroll_for_more(page)
    return await _build_search_listings(page, request.limit)


async def _search_airbnb(request: AirbnbSearchRequest) -> AirbnbSearchResponse:
    """Search Airbnb listings using Playwright CDP."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await launch_stealth_browser(playwright_obj)
            listings = await _search_airbnb_with_page(page, request)
            elapsed = int(timer_ms() - start)
            logger.info("Airbnb search returned %d listings in %dms", len(listings), elapsed)
            return AirbnbSearchResponse(
                success=True,
                listings=listings,
                count=len(listings),
                elapsed_ms=elapsed,
            )
    except (PlaywrightTimeoutError, *BROWSER_EXC) as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Airbnb search error (%s): %s", type(exc).__name__, exc)
        return AirbnbSearchResponse(
            success=False,
            error=str(exc),
            elapsed_ms=elapsed,
        )
    finally:
        await close_browser_chain(page, ctx)


# ─────────────────────────────────────────────
# Listing-detail orchestration
# ─────────────────────────────────────────────

_DESCRIPTION_SELECTORS = (
    "[data-section-id='DESCRIPTION_DEFAULT']",
    "[data-testid='listing-description']",
    "[class*='description']",
)

_TITLE_SELECTORS = (
    "h1",
    "[data-testid='listing-title']",
    "[class*='title'] h1",
)

_LOCATION_SELECTORS = (
    "[data-testid='listing-location']",
    "[class*='location']",
)

_ROOM_SELECTORS = (
    "[data-testid='listing-card-subtitle']",
    "h2[class*='subtitle']",
    "[class*='room-details']",
)

_RATING_SELECTORS = (
    "[data-testid='rating']",
    "[class*='rating']",
)


def _to_host(host: HostFields) -> AirbnbHost:
    """Convert internal :class:`HostFields` to the public ``AirbnbHost`` row."""
    return AirbnbHost(
        name=host.name,
        is_superhost=host.is_superhost,
        response_rate=host.response_rate,
        response_time=host.response_time,
    )


async def _wait_for_detail(page: Page) -> None:
    """Wait briefly for the listing-page title to render."""
    try:
        await page.wait_for_selector(
            "h1, [data-testid='listing-title']", timeout=15000,
        )
    except PlaywrightTimeoutError:
        return


async def _populate_room_details(page: Page, detail: AirbnbListingDetail) -> None:
    """Read the room-detail subtitle and copy fields onto ``detail``."""
    text = await safe_first_text(page, _ROOM_SELECTORS)
    if not text:
        return
    rooms = parse_room_details(text)
    detail.guests = rooms.guests
    detail.bedrooms = rooms.bedrooms
    detail.beds = rooms.beds
    detail.baths = rooms.baths
    if rooms.property_type:
        detail.property_type = rooms.property_type


async def _populate_rating(page: Page, detail: AirbnbListingDetail) -> None:
    """Pull rating + review_count off the detail page."""
    text = await safe_first_text(page, _RATING_SELECTORS)
    if not text:
        return
    detail.rating = parse_rating(text)
    detail.review_count = parse_review_count(text)


async def _populate_simple_text_fields(page: Page, detail: AirbnbListingDetail) -> None:
    """Pull title / description / location off the page."""
    detail.title = await safe_first_text(page, _TITLE_SELECTORS) or ""
    detail.description = await safe_first_text(page, _DESCRIPTION_SELECTORS)
    detail.location = await safe_first_text(page, _LOCATION_SELECTORS)


async def _populate_collections(page: Page, detail: AirbnbListingDetail) -> None:
    """Pull amenities, house rules, reviews, photo count off the page."""
    detail.amenities = await collect_amenities(page, limit=30)
    detail.house_rules = await collect_house_rules(page)
    review_blocks = await collect_reviews(page, limit=5)
    detail.reviews = [
        AirbnbReview(author=block.author, date=block.date, text=block.text)
        for block in review_blocks
    ]
    detail.photos_count = await count_photos(page)


async def _populate_listing_detail(
    page: Page, detail: AirbnbListingDetail,
) -> None:
    """Fill every field on the detail Pydantic model from the live page."""
    await _populate_simple_text_fields(page, detail)
    detail.host = _to_host(await collect_host_fields(page))
    detail.price_per_night = await parse_detail_price(page)
    await _populate_room_details(page, detail)
    await _populate_rating(page, detail)
    await _populate_collections(page, detail)
    detail.cancellation_policy = await parse_cancellation_policy(page)


async def _get_listing_detail(url: str) -> Optional[AirbnbListingDetail]:
    """Get detailed information about a single Airbnb listing."""
    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await launch_stealth_browser(playwright_obj)
            logger.info("[Airbnb] Navigating to listing: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await random_delay(2000, 3500)
            await _wait_for_detail(page)

            detail = AirbnbListingDetail(
                title="",
                listing_url=url,
                listing_id=extract_listing_id(url),
            )
            await _populate_listing_detail(page, detail)
            return detail if detail.title else None
    except (PlaywrightTimeoutError, *BROWSER_EXC) as exc:
        logger.error("Airbnb listing detail error (%s): %s", type(exc).__name__, exc)
        return None
    finally:
        await close_browser_chain(page, ctx)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/search", response_model=AirbnbSearchResponse)
async def airbnb_search(request: AirbnbSearchRequest) -> AirbnbSearchResponse:
    """Search Airbnb listings using Playwright CDP with stealth delays."""
    return await _search_airbnb(request)


@router.post("/listing", response_model=AirbnbListingResponse)
async def airbnb_listing(request: AirbnbListingRequest) -> AirbnbListingResponse:
    """Scrape single Airbnb listing details using Playwright CDP."""
    start = timer_ms()
    listing = await _get_listing_detail(request.url)
    elapsed = int(timer_ms() - start)
    if listing is not None:
        return AirbnbListingResponse(
            success=True,
            listing=listing,
            elapsed_ms=elapsed,
        )
    return AirbnbListingResponse(
        success=False,
        error="Failed to fetch listing details",
        elapsed_ms=elapsed,
    )
