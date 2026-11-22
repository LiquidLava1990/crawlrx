"""kayak.py -- Kayak travel scraper.

Endpoints:
  POST /scrape/kayak/flights -- search Kayak flights
  POST /scrape/kayak/hotels  -- search Kayak hotels

Uses Playwright CDP 9229. Kayak enforces strict environment checks.
Stealth delays: 1500-3000ms.
"""
from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .utils import timer_ms
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from . import _kayak_extract as _kx

logger = logging.getLogger("crawlrx.kayak")
router = APIRouter(prefix="/scrape/kayak", tags=["kayak"])

KAYAK_BASE_URL = "https://www.kayak.com"


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class KayakFlight(BaseModel):
    """Single Kayak flight listing."""
    airline: str = Field(...)
    price: Optional[str] = Field(default=None)
    departure_time: Optional[str] = Field(default=None)
    arrival_time: Optional[str] = Field(default=None)
    duration: Optional[str] = Field(default=None)
    stops: Optional[str] = Field(default=None)
    booking_url: Optional[str] = Field(default=None)
    deal_score: Optional[str] = Field(default=None)


class KayakHotel(BaseModel):
    """Single Kayak hotel listing."""
    name: str = Field(...)
    price_per_night: Optional[str] = Field(default=None)
    rating: Optional[float] = Field(default=None)
    review_count: Optional[int] = Field(default=None)
    location: Optional[str] = Field(default=None)
    hotel_url: Optional[str] = Field(default=None)
    image_url: Optional[str] = Field(default=None)
    deal_score: Optional[str] = Field(default=None)


class KayakFlightRequest(BaseModel):
    """Request model for POST /scrape/kayak/flights."""
    origin: str = Field(...)
    destination: str = Field(...)
    depart_date: str = Field(...)
    return_date: Optional[str] = Field(default=None)
    passengers: int = Field(default=1, ge=1, le=9)
    cabin: str = Field(default="economy")
    limit: int = Field(default=10, ge=1, le=50)


class KayakHotelRequest(BaseModel):
    """Request model for POST /scrape/kayak/hotels."""
    location: str = Field(...)
    checkin: str = Field(...)
    checkout: str = Field(...)
    guests: int = Field(default=1, ge=1, le=10)
    limit: int = Field(default=10, ge=1, le=50)


class KayakFlightResponse(BaseModel):
    """Response model for POST /scrape/kayak/flights."""
    success: bool
    flights: List[KayakFlight] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class KayakHotelResponse(BaseModel):
    """Response model for POST /scrape/kayak/hotels."""
    success: bool
    hotels: List[KayakHotel] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 1500, max_ms: int = 3000) -> None:
    """Sleep for a random duration between min and max milliseconds."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def _get_cabin_code(cabin: str) -> str:
    """Convert cabin class to Kayak cabin code."""
    return {"economy": "economy", "business": "business", "first": "first"}.get(
        cabin.lower(), "economy",
    )


def _flight_search_url(req: KayakFlightRequest) -> str:
    """Construct the Kayak flights search URL for ``req``."""
    origin = urllib.parse.quote_plus(req.origin)
    dest = urllib.parse.quote_plus(req.destination)
    if req.return_date:
        url = f"{KAYAK_BASE_URL}/flights/{origin}-{dest}/{req.depart_date}/{req.return_date}"
    else:
        url = f"{KAYAK_BASE_URL}/flights/{origin}-{dest}/{req.depart_date}"
    cabin_code = _get_cabin_code(req.cabin)
    if cabin_code != "economy":
        url += f"?cabins={cabin_code}"
    return url


def _hotel_search_url(req: KayakHotelRequest) -> str:
    """Construct the Kayak hotels search URL for ``req``."""
    location = urllib.parse.quote_plus(req.location)
    return (
        f"{KAYAK_BASE_URL}/hotels/{location}/"
        f"{req.checkin}/{req.checkout}/{req.guests}adults"
    )


async def _wait_first_selector(
    page: Any, selectors: tuple[str, ...], timeout_ms: int,
) -> None:
    """Wait for any of ``selectors``; log when none matched."""
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=timeout_ms)
            return
        except PlaywrightTimeoutError:
            continue
    logger.warning("Timeout waiting for Kayak results selectors")


async def _scroll_page(page: Any, scrolls: int = 3) -> None:
    """Scroll repeatedly to lazy-load more cards."""
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, 800)")
        await _random_delay(1000, 2000)


async def _find_cards(page: Any, selectors: tuple[str, ...]) -> list:
    """Try each selector in order; return the first non-empty card list."""
    for sel in selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            return cards
    return []


# ─────────────────────────────────────────────
# Generic search pipeline (shared by flights + hotels)
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class _SearchSpec:
    """Bundle of per-vertical selectors + URL passed to ``_run_search``."""

    label: str
    search_url: str
    wait_selectors: tuple[str, ...]
    card_selectors: tuple[str, ...]
    dedupe_field: str


CardDataFn = Callable[[Any], Awaitable[Optional[dict]]]


async def _collect_via(
    page: Any, spec: _SearchSpec, card_data_fn: CardDataFn, limit: int,
) -> list[dict]:
    """Iterate cards, build records via ``card_data_fn``, dedupe."""
    cards = await _find_cards(page, spec.card_selectors)
    logger.info("Found %d Kayak %s cards", len(cards), spec.label)
    out: list[dict] = []
    seen: set[str] = set()
    for card in cards[: limit * 2]:
        record = await card_data_fn(card)
        if record is None:
            continue
        url = record.get(spec.dedupe_field)
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        out.append(record)
        if len(out) >= limit:
            break
    return out


async def _run_search(
    spec: _SearchSpec, card_data_fn: CardDataFn, limit: int,
) -> tuple[list[dict], int]:
    """Drive the Playwright pipeline (goto -> wait -> scroll -> collect)."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info(
                "[Kayak] Navigating to %s search: %s", spec.label, spec.search_url,
            )
            await page.goto(
                spec.search_url, wait_until="domcontentloaded", timeout=45000,
            )
            await _random_delay(2000, 3500)
            await _wait_first_selector(page, spec.wait_selectors, 20000)
            await _scroll_page(page)
            records = await _collect_via(page, spec, card_data_fn, limit)
            return records, int(timer_ms() - start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Flight + Hotel public entry points
# ─────────────────────────────────────────────

_FLIGHT_CARD_SELECTORS: tuple[str, ...] = (
    "[data-testid='flightResult']",
    ".Flights-Results-FlightResultItem",
    "[class*='result']",
)
_FLIGHT_WAIT_SELECTORS: tuple[str, ...] = (
    "[data-testid='flightResult']",
    ".Flights-Results-FlightResultItem",
)

_HOTEL_CARD_SELECTORS: tuple[str, ...] = (
    "[data-testid='hotelResult']",
    ".Hotels-Results-HotelResultItem",
    "[class*='hotel-result']",
    "[data-testid='property-card']",
)
_HOTEL_WAIT_SELECTORS: tuple[str, ...] = (
    "[data-testid='hotelResult']",
    ".Hotels-Results-HotelResultItem",
)


async def _search_flights(request: KayakFlightRequest) -> KayakFlightResponse:
    """Search Kayak flights using Playwright CDP."""
    spec = _SearchSpec(
        label="flight",
        search_url=_flight_search_url(request),
        wait_selectors=_FLIGHT_WAIT_SELECTORS,
        card_selectors=_FLIGHT_CARD_SELECTORS,
        dedupe_field="booking_url",
    )
    try:
        records, elapsed = await _run_search(spec, _kx.flight_card_data, request.limit)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Kayak flight search error: %s", type(exc).__name__)
        return KayakFlightResponse(success=False, error=str(exc), elapsed_ms=0)
    flights = [KayakFlight(**rec) for rec in records]
    logger.info(
        "Kayak flight search returned %d flights in %dms", len(flights), elapsed,
    )
    return KayakFlightResponse(
        success=True, flights=flights, count=len(flights), elapsed_ms=elapsed,
    )


async def _search_hotels(request: KayakHotelRequest) -> KayakHotelResponse:
    """Search Kayak hotels using Playwright CDP."""
    spec = _SearchSpec(
        label="hotel",
        search_url=_hotel_search_url(request),
        wait_selectors=_HOTEL_WAIT_SELECTORS,
        card_selectors=_HOTEL_CARD_SELECTORS,
        dedupe_field="hotel_url",
    )
    try:
        records, elapsed = await _run_search(spec, _kx.hotel_card_data, request.limit)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Kayak hotel search error: %s", type(exc).__name__)
        return KayakHotelResponse(success=False, error=str(exc), elapsed_ms=0)
    hotels = [KayakHotel(**rec) for rec in records]
    logger.info(
        "Kayak hotel search returned %d hotels in %dms", len(hotels), elapsed,
    )
    return KayakHotelResponse(
        success=True, hotels=hotels, count=len(hotels), elapsed_ms=elapsed,
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/flights", response_model=KayakFlightResponse)
async def kayak_flights(request: KayakFlightRequest) -> KayakFlightResponse:
    """Search Kayak flights (Playwright CDP, stealth delays)."""
    return await _search_flights(request)


@router.post("/hotels", response_model=KayakHotelResponse)
async def kayak_hotels(request: KayakHotelRequest) -> KayakHotelResponse:
    """Search Kayak hotels (Playwright CDP, stealth delays)."""
    return await _search_hotels(request)
