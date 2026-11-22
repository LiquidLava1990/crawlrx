"""expedia.py -- Expedia flights and hotels scraper.

Endpoints:
- POST /scrape/expedia/flights -- Search Expedia flights
- POST /scrape/expedia/hotels  -- Search Expedia hotels

Uses Playwright CDP. Helpers, dataclasses + the shared search runner live
in :mod:`._expedia_helpers` so this module stays focused on the FastAPI
surface plus per-product result parsers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter
from playwright.async_api import Page
from pydantic import BaseModel, Field

from ._expedia_helpers import (
    FlightQuery,
    HotelQuery,
    build_flight_search_url,
    build_hotel_search_url,
    coerce_flight_dict,
    coerce_hotel_dict,
    random_delay,
    run_expedia_search,
    scroll_and_lazy_load,
    wait_for_cards,
)
from ._scraper_helpers import PARSE_EXCEPTIONS

logger = logging.getLogger("crawlrx.expedia")
router = APIRouter(prefix="/scrape/expedia", tags=["expedia"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class LayoverInfo(BaseModel):
    """Layover information."""
    airport: str
    duration: str
    change_planes: bool = True


class FlightResult(BaseModel):
    """Single flight result."""
    airline: str
    flight_number: Optional[str] = None
    departure_time: str
    arrival_time: str
    duration: str
    stops: int
    price: str
    cabin_class: str
    booking_url: str
    layover_info: List[LayoverInfo] = Field(default_factory=list)


class HotelResult(BaseModel):
    """Single hotel result."""
    name: str
    price_per_night: Optional[str] = None
    total_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    location: Optional[str] = None
    hotel_url: str
    image_url: Optional[str] = None
    free_cancellation: bool = False
    amenities_summary: List[str] = Field(default_factory=list)


class ExpediaFlightsRequest(BaseModel):
    """Request model for POST /scrape/expedia/flights."""
    origin: str = Field(..., description="Origin airport code (e.g., JFK)")
    destination: str = Field(..., description="Destination airport code (e.g., LAX)")
    depart_date: str = Field(..., description="Departure date (YYYY-MM-DD)")
    return_date: Optional[str] = Field(default=None, description="Return date for round-trip (YYYY-MM-DD). Omit for one-way.")
    passengers: int = Field(default=1, ge=1, le=9, description="Number of passengers")
    cabin: str = Field(default="economy", description="Cabin class: economy, premium_economy, business, or first")
    limit: int = Field(default=10, ge=1, le=30, description="Maximum flights to return")


class ExpediaFlightsResponse(BaseModel):
    """Response model for POST /scrape/expedia/flights."""
    success: bool
    flights: List[FlightResult] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class ExpediaHotelsRequest(BaseModel):
    """Request model for POST /scrape/expedia/hotels."""
    location: str = Field(..., description="Location to search (city, address, or landmark)")
    checkin: str = Field(..., description="Check-in date (YYYY-MM-DD)")
    checkout: str = Field(..., description="Check-out date (YYYY-MM-DD)")
    guests: int = Field(default=1, ge=1, le=14, description="Number of guests")
    min_price: Optional[int] = Field(default=None, ge=1, description="Minimum price per night")
    max_price: Optional[int] = Field(default=None, ge=1, description="Maximum price per night")
    limit: int = Field(default=10, ge=1, le=30, description="Maximum hotels to return")


class ExpediaHotelsResponse(BaseModel):
    """Response model for POST /scrape/expedia/hotels."""
    success: bool
    hotels: List[HotelResult] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


def _flight_query(request: ExpediaFlightsRequest) -> FlightQuery:
    """Translate FastAPI request to a :class:`FlightQuery`."""
    return FlightQuery(
        origin=request.origin,
        destination=request.destination,
        depart_date=request.depart_date,
        return_date=request.return_date,
        passengers=request.passengers,
        cabin=request.cabin,
        limit=request.limit,
    )


def _hotel_query(request: ExpediaHotelsRequest) -> HotelQuery:
    """Translate FastAPI request to a :class:`HotelQuery`."""
    return HotelQuery(
        location=request.location,
        checkin=request.checkin,
        checkout=request.checkout,
        guests=request.guests,
        min_price=request.min_price,
        max_price=request.max_price,
        limit=request.limit,
    )


# ─────────────────────────────────────────────
# Per-product parsers (still live here -- they bind to FlightResult/HotelResult)
# ─────────────────────────────────────────────

_FLIGHT_CARD_SELECTOR = (
    "[data-testid='flight-search-result'], "
    ".flight-search-result, .uitk-card"
)

_HOTEL_CARD_SELECTOR = (
    "[data-testid='property-card'], .property-card, "
    ".uitk-card[data-testid='lodging-card']"
)


async def _parse_flights(page: Page, limit: int) -> List[FlightResult]:
    """Parse flight results from an Expedia search page."""
    if not await wait_for_cards(page, _FLIGHT_CARD_SELECTOR, "flight"):
        return []
    await random_delay(1000, 2000)
    raw = await page.evaluate(_FLIGHTS_JS)
    return [FlightResult(layover_info=[], **coerce_flight_dict(d)) for d in raw[:limit]]


async def _parse_hotels(page: Page, limit: int) -> List[HotelResult]:
    """Parse hotel results from an Expedia search page."""
    if not await wait_for_cards(page, _HOTEL_CARD_SELECTOR, "hotel"):
        return []
    await random_delay(1000, 2000)
    await scroll_and_lazy_load(page, steps=3)
    raw = await page.evaluate(_HOTELS_JS)
    return [HotelResult(**coerce_hotel_dict(d)) for d in raw[:limit]]


# ─────────────────────────────────────────────
# Search runners (thin wrappers over run_expedia_search)
# ─────────────────────────────────────────────

async def _do_search_flights(query: FlightQuery) -> ExpediaFlightsResponse:
    """Run an Expedia flights search via the shared driver."""
    logger.info("Searching Expedia flights: %s to %s", query.origin, query.destination)
    outcome = await run_expedia_search(
        url=build_flight_search_url(query),
        parser=_parse_flights,
        limit=query.limit,
        label="flights",
    )
    return ExpediaFlightsResponse(
        success=outcome.success,
        flights=list(outcome.items),
        count=len(outcome.items),
        error=outcome.error,
        elapsed_ms=outcome.elapsed_ms,
    )


async def _do_search_hotels(query: HotelQuery) -> ExpediaHotelsResponse:
    """Run an Expedia hotels search via the shared driver."""
    logger.info("Searching Expedia hotels: %s", query.location)
    outcome = await run_expedia_search(
        url=build_hotel_search_url(query),
        parser=_parse_hotels,
        limit=query.limit,
        label="hotels",
    )
    return ExpediaHotelsResponse(
        success=outcome.success,
        hotels=list(outcome.items),
        count=len(outcome.items),
        error=outcome.error,
        elapsed_ms=outcome.elapsed_ms,
    )


# ─────────────────────────────────────────────
# JS extraction snippets
# ─────────────────────────────────────────────

_FLIGHTS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-testid="flight-search-result"], .flight-search-result, .uitk-card[data-testid="offer-card"]');

    cards.forEach(card => {
        try {
            const airlineEl = card.querySelector('[data-testid="flight-operator"], .airline-name, .uitk-text:contains("Airlines")');
            const airline = airlineEl ? airlineEl.textContent.trim() : '';

            const flightNumEl = card.querySelector('[data-testid="flight-number"], .flight-number');
            const flight_number = flightNumEl ? flightNumEl.textContent.trim() : '';

            const depEl = card.querySelector('[data-testid="departure-time"], .departure-time, .uitk-text:has([data-testid="departure"])');
            const departure_time = depEl ? depEl.textContent.trim() : '';

            const arrEl = card.querySelector('[data-testid="arrival-time"], .arrival-time');
            const arrival_time = arrEl ? arrEl.textContent.trim() : '';

            const durEl = card.querySelector('[data-testid="duration"], .duration, .uitk-text:contains("h"):contains("m")');
            const duration = durEl ? durEl.textContent.trim() : '';

            const stopsEl = card.querySelector('[data-testid="stops"], .stops');
            let stops = 0;
            if (stopsEl) {
                const text = stopsEl.textContent.toLowerCase();
                if (text.includes('nonstop') || text.includes('direct')) {
                    stops = 0;
                } else {
                    const match = text.match(/(\\d+)\\s*stop/);
                    if (match) stops = parseInt(match[1]);
                }
            }

            const priceEl = card.querySelector('[data-testid="price"], .price, .uitk-text:contains("$")');
            const price = priceEl ? priceEl.textContent.trim() : '';

            const cabinEl = card.querySelector('[data-testid="cabin-class"], .cabin-class');
            const cabin_class = cabinEl ? cabinEl.textContent.trim() : 'Economy';

            const linkEl = card.querySelector('a[href*="/Flight-Information"], a.uitk-card-link');
            let booking_url = '';
            if (linkEl) {
                const href = linkEl.getAttribute('href');
                booking_url = href.startsWith('http') ? href : 'https://www.expedia.com' + href;
            }

            if (airline && departure_time) {
                results.push({
                    airline, flight_number, departure_time, arrival_time,
                    duration, stops, price, cabin_class, booking_url
                });
            }
        } catch (e) { /* skip card */ }
    });
    return results;
}
"""


_HOTELS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-testid="property-card"], .property-card, [data-testid="lodging-card"]');

    cards.forEach(card => {
        try {
            const nameEl = card.querySelector('[data-testid="property-title"], .property-title, h3 a, [data-testid="listing-title"]');
            const name = nameEl ? nameEl.textContent.trim() : '';

            const priceEl = card.querySelector('[data-testid="price-per-night"], .price-per-night, .uitk-text:contains("per night")');
            let price_per_night = '';
            if (priceEl) {
                price_per_night = priceEl.textContent.trim();
            } else {
                const altPriceEl = card.querySelector('[data-testid="price"], .price');
                if (altPriceEl) price_per_night = altPriceEl.textContent.trim();
            }

            const totalEl = card.querySelector('[data-testid="total-price"], .total-price, .uitk-text:contains("total")');
            const total_price = totalEl ? totalEl.textContent.trim() : '';

            const ratingEl = card.querySelector('[data-testid="rating"], .rating, .uitk-badge');
            let rating = null;
            if (ratingEl) {
                const match = ratingEl.textContent.match(/([\\d.]+)/);
                if (match) rating = parseFloat(match[1]);
            }

            const reviewEl = card.querySelector('[data-testid="review-count"], .review-count, .uitk-text:contains("reviews")');
            let review_count = null;
            if (reviewEl) {
                const match = reviewEl.textContent.match(/([\\d,]+)/);
                if (match) review_count = parseInt(match[1].replace(/,/g, ''));
            }

            const locEl = card.querySelector('[data-testid="property-location"], .location, .uitk-text:contains("miles")');
            const location = locEl ? locEl.textContent.trim() : '';

            const linkEl = card.querySelector('a[href*="/hotel"], a[data-testid="property-link"]');
            let hotel_url = '';
            if (linkEl) {
                const href = linkEl.getAttribute('href');
                hotel_url = href.startsWith('http') ? href : 'https://www.expedia.com' + href;
            }

            const imgEl = card.querySelector('img[data-testid="property-image"], img');
            const image_url = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src')) : '';

            const cancelEl = card.querySelector('[data-testid="free-cancellation"], .free-cancellation');
            const free_cancellation = cancelEl ? true : false;

            const amenities = [];
            const amenityEls = card.querySelectorAll('[data-testid="amenity"], .amenity');
            amenityEls.forEach(el => {
                const text = el.textContent.trim();
                if (text) amenities.push(text);
            });

            if (name && hotel_url) {
                results.push({
                    name, price_per_night, total_price, rating, review_count,
                    location, hotel_url, image_url, free_cancellation, amenities
                });
            }
        } catch (e) { /* skip card */ }
    });
    return results;
}
"""


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post(
    "/flights",
    response_model=ExpediaFlightsResponse,
    summary="Search Expedia flights",
)
async def search_expedia_flights(request: ExpediaFlightsRequest) -> ExpediaFlightsResponse:
    """Search Expedia flights."""
    start = time.time()
    try:
        return await asyncio.wait_for(
            _do_search_flights(_flight_query(request)),
            timeout=90,
        )
    except (asyncio.TimeoutError, *PARSE_EXCEPTIONS) as exc:
        logger.error("Expedia flights search request failed (%s): %s", type(exc).__name__, exc)
        return ExpediaFlightsResponse(
            success=False,
            error=f"Request failed: {type(exc).__name__}",
            elapsed_ms=int((time.time() - start) * 1000),
        )


@router.post(
    "/hotels",
    response_model=ExpediaHotelsResponse,
    summary="Search Expedia hotels",
)
async def search_expedia_hotels(request: ExpediaHotelsRequest) -> ExpediaHotelsResponse:
    """Search Expedia hotels."""
    start = time.time()
    try:
        return await asyncio.wait_for(
            _do_search_hotels(_hotel_query(request)),
            timeout=90,
        )
    except (asyncio.TimeoutError, *PARSE_EXCEPTIONS) as exc:
        logger.error("Expedia hotels search request failed (%s): %s", type(exc).__name__, exc)
        return ExpediaHotelsResponse(
            success=False,
            error=f"Request failed: {type(exc).__name__}",
            elapsed_ms=int((time.time() - start) * 1000),
        )
