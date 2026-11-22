"""Expedia scraper helpers extracted from ``expedia.py``.

Module split: param-bag dataclasses, URL builders, JS evaluators and
the shared browser-driven search runner live here so ``expedia.py`` stays
inside the FILE_TOO_LARGE ceiling.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from urllib.parse import quote

from playwright.async_api import (
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    parse_price,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.expedia.helpers")

EXPEDIA_BASE_URL = "https://www.expedia.com"

SEARCH_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + SCRAPE_FAILURE_EXCEPTIONS
)

_CABIN_MAP: dict[str, str] = {
    "economy": "coach",
    "premium_economy": "premium",
    "business": "business",
    "first": "first",
}


# ─────────────────────────────────────────────
# Param bags (the parameter-count limit clean)
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class FlightQuery:
    """Bundled args for the flight URL builder + search runner."""

    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    passengers: int = 1
    cabin: str = "economy"
    limit: int = 10


@dataclass(frozen=True)
class HotelQuery:
    """Bundled args for the hotel URL builder + search runner."""

    location: str
    checkin: str
    checkout: str
    guests: int = 1
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    limit: int = 10


@dataclass(frozen=True)
class SearchOutcome:
    """Result of a single browser-driven search attempt."""

    success: bool
    items: List[Any]
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# URL builders
# ─────────────────────────────────────────────

def build_flight_search_url(query: FlightQuery) -> str:
    """Build Expedia flight search URL from a :class:`FlightQuery` bag."""
    cabin_code = _CABIN_MAP.get(query.cabin.lower(), "coach")
    if query.return_date:
        return (
            f"{EXPEDIA_BASE_URL}/Flights-Search?"
            f"leg1={query.origin},{query.destination},{query.depart_date}"
            f"&leg2={query.destination},{query.origin},{query.return_date}"
            f"&passengers=adults:{query.passengers}"
            f"&mode=search&options=cabinclass:{cabin_code}"
        )
    return (
        f"{EXPEDIA_BASE_URL}/Flights-Search?"
        f"leg1={query.origin},{query.destination},{query.depart_date}"
        f"&passengers=adults:{query.passengers}"
        f"&mode=search&trip=oneway&options=cabinclass:{cabin_code}"
    )


def build_hotel_search_url(query: HotelQuery) -> str:
    """Build Expedia hotel search URL from a :class:`HotelQuery` bag."""
    location_encoded = quote(query.location)
    url = (
        f"{EXPEDIA_BASE_URL}/Hotel-Search?destination={location_encoded}"
        f"&startDate={query.checkin}&endDate={query.checkout}"
        f"&adults={query.guests}"
    )
    if query.min_price:
        url += f"&minPrice={query.min_price}"
    if query.max_price:
        url += f"&maxPrice={query.max_price}"
    return url


# ─────────────────────────────────────────────
# Stealth helpers
# ─────────────────────────────────────────────

async def random_delay(min_ms: int = 1000, max_ms: int = 2500) -> None:
    """Sleep for random duration between ``min_ms`` and ``max_ms``."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


async def scroll_and_lazy_load(page: Page, *, steps: int = 3) -> None:
    """Trigger lazy loaded cards via incremental scroll."""
    for _ in range(steps):
        await page.evaluate("window.scrollBy(0, 800)")
        await random_delay(500, 1000)


async def wait_for_cards(page: Page, selector: str, label: str) -> bool:
    """Wait for cards matching ``selector``. False on timeout."""
    try:
        await page.wait_for_selector(selector, timeout=20000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for %s results to load", label)
        return False
    return True


# ─────────────────────────────────────────────
# Browser-driven search orchestration
# ─────────────────────────────────────────────

async def _open_and_navigate(playwright_obj: Any, url: str) -> Tuple[Any, Any, Page]:
    """Open a stealth context and navigate to ``url``."""
    browser, ctx, page = await make_stealth_context(playwright_obj)
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await random_delay(2000, 3000)
    return browser, ctx, page


async def run_expedia_search(
    *,
    url: str,
    parser: Callable[[Page, int], Awaitable[List[Any]]],
    limit: int,
    label: str,
) -> SearchOutcome:
    """Open Playwright, navigate, parse, close. Shared by flights/hotels."""
    start_time = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await _open_and_navigate(playwright_obj, url)
            items = await parser(page, limit)
            logger.info("Found %d Expedia %s results", len(items), label)
            return SearchOutcome(
                success=True,
                items=items,
                elapsed_ms=int((time.time() - start_time) * 1000),
            )
    except PlaywrightTimeoutError as exc:
        logger.error("Expedia %s search timeout: %s", label, exc)
        return SearchOutcome(
            success=False,
            items=[],
            error=f"Timeout: {exc}",
            elapsed_ms=int((time.time() - start_time) * 1000),
        )
    except SEARCH_EXCEPTIONS as exc:
        logger.error("Expedia %s search error (%s): %s", label, type(exc).__name__, exc)
        return SearchOutcome(
            success=False,
            items=[],
            error=str(exc),
            elapsed_ms=int((time.time() - start_time) * 1000),
        )
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Result conversion (kept here so expedia.py stays slim)
# ─────────────────────────────────────────────

def coerce_hotel_dict(data: dict) -> dict:
    """Apply ``parse_price`` + amenity slicing to raw JS hotel dicts."""
    return {
        "name": data.get("name", ""),
        "price_per_night": parse_price(data.get("price_per_night")),
        "total_price": parse_price(data.get("total_price")),
        "rating": data.get("rating"),
        "review_count": data.get("review_count"),
        "location": data.get("location") or None,
        "hotel_url": data.get("hotel_url", ""),
        "image_url": data.get("image_url") or None,
        "free_cancellation": bool(data.get("free_cancellation", False)),
        "amenities_summary": list(data.get("amenities", []))[:5],
    }


def coerce_flight_dict(data: dict) -> dict:
    """Apply ``parse_price`` to raw JS flight dicts."""
    return {
        "airline": data.get("airline", ""),
        "flight_number": data.get("flight_number") or None,
        "departure_time": data.get("departure_time", ""),
        "arrival_time": data.get("arrival_time", ""),
        "duration": data.get("duration", ""),
        "stops": data.get("stops", 0),
        "price": parse_price(data.get("price")) or "",
        "cabin_class": data.get("cabin_class", "Economy"),
        "booking_url": data.get("booking_url", ""),
    }
