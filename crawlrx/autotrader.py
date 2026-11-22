"""autotrader.py -- AutoTrader vehicle scraper

Endpoints:
- POST /scrape/autotrader/search -- Search AutoTrader vehicle listings
- POST /scrape/autotrader/vehicle -- Scrape single vehicle detail

Uses Playwright with stealth (JS-rendered). Includes random delays.
"""

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

from fastapi import APIRouter
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from .config import USER_AGENTS
from ._autotrader_helpers import AutoTraderSearchArgs, fresh_browser_session
from ._autotrader_parsers import (
    AutoTraderListing,
    AutoTraderVehicleDetail,
    DealerInfo,
    LISTING_EXTRACT_JS,
    VEHICLE_EXTRACT_JS,
    listing_from_card,
    vehicle_detail_from_payload,
)
from ._scraper_helpers import playwright_exception_types

logger = logging.getLogger("crawlrx.autotrader")
router = APIRouter(prefix="/scrape/autotrader", tags=["autotrader"])

AUTOTRADER_BASE_URL = "https://www.autotrader.com"

# Narrow exception family for a Playwright-driven scrape (network + JS + parse).
_SCRAPE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError, ValueError, KeyError, TypeError)
)
_REQUEST_FAIL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    RuntimeError,
    OSError,
)

T = TypeVar("T")


# ─────────────────────────────────────────────
# Pydantic Request / Response Models
# ─────────────────────────────────────────────

class AutoTraderSearchRequest(BaseModel):
    """Request model for POST /scrape/autotrader/search"""

    make: Optional[str] = Field(default=None, description="Vehicle make (e.g., 'Toyota')")
    model: Optional[str] = Field(default=None, description="Vehicle model (e.g., 'Camry')")
    zip_code: str = Field(default="40202", description="ZIP code for search location")
    max_price: Optional[int] = Field(default=None, description="Maximum price filter")
    min_year: Optional[int] = Field(default=None, description="Minimum year filter")
    max_year: Optional[int] = Field(default=None, description="Maximum year filter")
    max_mileage: Optional[int] = Field(default=None, description="Maximum mileage filter")
    listing_type: str = Field(default="any", description="'used', 'new', 'certified', or 'any'")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum listings to return")


class AutoTraderSearchResponse(BaseModel):
    """Response model for POST /scrape/autotrader/search"""

    success: bool
    listings: List[AutoTraderListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class AutoTraderVehicleRequest(BaseModel):
    """Request model for POST /scrape/autotrader/vehicle"""

    url: str = Field(..., description="Full AutoTrader vehicle listing URL")


class AutoTraderVehicleResponse(BaseModel):
    """Response model for POST /scrape/autotrader/vehicle"""

    success: bool
    vehicle: Optional[AutoTraderVehicleDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# Public surface for callers that imported from this module historically.
__all__ = (
    "AutoTraderListing",
    "AutoTraderVehicleDetail",
    "DealerInfo",
    "AutoTraderSearchRequest",
    "AutoTraderSearchResponse",
    "AutoTraderVehicleRequest",
    "AutoTraderVehicleResponse",
    "search_autotrader",
    "scrape_autotrader_vehicle",
)


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Sleep for random duration between min_ms and max_ms."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


def _condition_segment(listing_type: str) -> str:
    """Map a listing-type filter to its URL path segment."""
    if listing_type == "new":
        return "new+cars"
    if listing_type == "used":
        return "used+cars"
    if listing_type == "certified":
        return "certified+cars"
    return "all+cars"


def _slugify(text: str) -> str:
    """Lowercase + dash-separated path slug."""
    return text.lower().replace(" ", "-")


def _build_search_url(args: AutoTraderSearchArgs) -> str:
    """Build AutoTrader search URL with filters."""
    path_parts = ["cars-for-sale", _condition_segment(args.listing_type)]
    if args.make:
        path_parts.append(_slugify(args.make))
        if args.model:
            path_parts.append(_slugify(args.model))
    base_path = "/".join(path_parts)

    params = [f"zip={args.zip_code}"]
    if args.max_price is not None:
        params.append(f"maxPrice={args.max_price}")
    if args.min_year is not None:
        params.append(f"startYear={args.min_year}")
    if args.max_year is not None:
        params.append(f"endYear={args.max_year}")
    if args.max_mileage is not None:
        params.append(f"maxMileage={args.max_mileage}")
    return f"{AUTOTRADER_BASE_URL}/{base_path}?{'&'.join(params)}"


async def _parse_search_listings(page: Any, limit: int) -> List[AutoTraderListing]:
    """Parse search results from AutoTrader page."""
    try:
        await page.wait_for_selector(
            "[data-testid='vehicle-card'], .vehicle-card, .listing-item",
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for AutoTrader listings to load")
        return []
    await _random_delay(500, 1000)
    listings_data = await page.evaluate(LISTING_EXTRACT_JS)
    return [listing_from_card(data) for data in listings_data[:limit]]


async def _parse_vehicle_detail(page: Any) -> AutoTraderVehicleDetail:
    """Parse single vehicle detail page from AutoTrader."""
    await page.wait_for_selector(
        "h1, [data-testid='vehicle-details'], .vehicle-details",
        timeout=20000,
    )
    await _random_delay(800, 1500)
    await page.evaluate("window.scrollBy(0, 500)")
    await _random_delay(300, 600)
    return vehicle_detail_from_payload(await page.evaluate(VEHICLE_EXTRACT_JS))


async def _navigate_search(page: Any, args: AutoTraderSearchArgs) -> str:
    """Navigate ``page`` to the search URL; returns the URL navigated to."""
    search_url = _build_search_url(args)
    logger.info("Searching AutoTrader: %s", search_url[:120])
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await _random_delay(2000, 3000)
    return search_url


async def _run_in_session(
    label: str,
    body: Callable[[Any], Awaitable[T]],
    fail: Callable[[str, str, int], T],
) -> T:
    """Open a fresh Playwright session, run ``body``, map failures via ``fail``.

    ``fail`` receives ``(reason_kind, message, elapsed_ms)`` where reason_kind
    is one of ``"timeout"`` / ``"error"`` so the response builder can
    customise prefixes (callers preserve the original wording).
    """
    start_time = time.time()
    try:
        async with fresh_browser_session(USER_AGENTS) as session:
            if session.page is None:
                raise RuntimeError(
                    f"[AutoTrader] fresh_browser_session yielded no page "
                    f"for {label!r}; browser launch failed silently"
                )
            return await body(session.page)
    except PlaywrightTimeoutError as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("%s timeout: %s", label, exc)
        return fail("timeout", str(exc), elapsed)
    except _SCRAPE_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Error %s (%s): %s", label, type(exc).__name__, exc)
        return fail("error", str(exc), elapsed)


def _search_failure(kind: str, message: str, elapsed: int) -> AutoTraderSearchResponse:
    """Build a search-failure response (timeout or error)."""
    error = "Timeout: %s" % message if kind == "timeout" else message
    return AutoTraderSearchResponse(
        success=False,
        error=error,
        elapsed_ms=elapsed,
    )


def _vehicle_failure(kind: str, message: str, elapsed: int) -> AutoTraderVehicleResponse:
    """Build a vehicle-failure response (timeout or error)."""
    error = "Timeout: %s" % message if kind == "timeout" else message
    return AutoTraderVehicleResponse(
        success=False,
        error=error,
        elapsed_ms=elapsed,
    )


async def _do_search_body(
    page: Any, args: AutoTraderSearchArgs, start_time: float
) -> AutoTraderSearchResponse:
    """Inner search body: navigate + parse listings + build success response."""
    await _navigate_search(page, args)
    listings = await _parse_search_listings(page, args.limit)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("Found %d AutoTrader listings in %dms", len(listings), elapsed)
    return AutoTraderSearchResponse(
        success=True,
        listings=listings,
        count=len(listings),
        elapsed_ms=elapsed,
    )


async def _do_vehicle_body(
    page: Any, url: str, start_time: float
) -> AutoTraderVehicleResponse:
    """Inner vehicle body: navigate + parse detail + build success response."""
    logger.info("Scraping AutoTrader vehicle: %s", url[:100])
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _random_delay(2000, 3000)
    vehicle_detail = await _parse_vehicle_detail(page)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info(
        "Scraped vehicle '%s %s %s' in %dms",
        vehicle_detail.year or "",
        vehicle_detail.make or "",
        vehicle_detail.model or "",
        elapsed,
    )
    return AutoTraderVehicleResponse(
        success=True,
        vehicle=vehicle_detail,
        elapsed_ms=elapsed,
    )


async def _do_search_autotrader(args: AutoTraderSearchArgs) -> AutoTraderSearchResponse:
    """Run an AutoTrader search inside a fresh Playwright session."""
    start_time = time.time()
    return await _run_in_session(
        "AutoTrader search",
        lambda page: _do_search_body(page, args, start_time),
        _search_failure,
    )


async def _do_scrape_vehicle(url: str) -> AutoTraderVehicleResponse:
    """Scrape one AutoTrader vehicle URL inside a fresh Playwright session."""
    start_time = time.time()
    return await _run_in_session(
        "AutoTrader vehicle scrape",
        lambda page: _do_vehicle_body(page, url, start_time),
        _vehicle_failure,
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

async def _run_with_request_timeout(
    label: str,
    fail: Callable[[str], T],
    body: Callable[[], Awaitable[T]],
) -> T:
    """Run ``body`` with a 60s wall clock + map known failures through ``fail``."""
    try:
        return await asyncio.wait_for(body(), timeout=60)
    except _REQUEST_FAIL_EXCEPTIONS as exc:
        logger.error("%s request failed (%s): %s", label, type(exc).__name__, exc)
        return fail("Request failed: %s" % exc)


def _search_args_from(request: AutoTraderSearchRequest) -> AutoTraderSearchArgs:
    """Pull request fields onto the dataclass arg bag."""
    return AutoTraderSearchArgs(
        make=request.make,
        model=request.model,
        zip_code=request.zip_code,
        max_price=request.max_price,
        min_year=request.min_year,
        max_year=request.max_year,
        max_mileage=request.max_mileage,
        listing_type=request.listing_type,
        limit=request.limit,
    )


@router.post(
    "/search",
    response_model=AutoTraderSearchResponse,
    summary="Search AutoTrader vehicle listings",
)
async def search_autotrader(request: AutoTraderSearchRequest) -> AutoTraderSearchResponse:
    """Search AutoTrader vehicle listings by filters (60s wall-clock cap)."""
    args = _search_args_from(request)
    return await _run_with_request_timeout(
        "AutoTrader search",
        lambda err: AutoTraderSearchResponse(
            success=False,
            error=err,
            elapsed_ms=60000,
        ),
        lambda: _do_search_autotrader(args),
    )


@router.post(
    "/vehicle",
    response_model=AutoTraderVehicleResponse,
    summary="Scrape AutoTrader vehicle detail",
)
async def scrape_autotrader_vehicle(request: AutoTraderVehicleRequest) -> AutoTraderVehicleResponse:
    """Scrape detailed information from a single AutoTrader vehicle listing."""
    if not request.url:
        return AutoTraderVehicleResponse(
            success=False,
            error="'url' is required",
        )
    return await _run_with_request_timeout(
        "AutoTrader vehicle scrape",
        lambda err: AutoTraderVehicleResponse(
            success=False,
            error=err,
            elapsed_ms=60000,
        ),
        lambda: _do_scrape_vehicle(request.url),
    )
