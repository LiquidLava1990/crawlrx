"""cars_com.py -- Cars.com vehicle scraper

Endpoints:
- POST /scrape/cars/search -- Search Cars.com listings
- POST /scrape/cars/vehicle -- Scrape single vehicle detail

Uses Playwright with stealth (JS-rendered). Includes random delays.
"""

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional, TypeVar

from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import playwright_exception_types
from ._cars_com_parsers import (
    CarListing,
    CarsSearchArgs,
    DealerInfo,
    PriceHistoryItem,
    VehicleDetail,
    LISTING_EXTRACT_JS,
    VEHICLE_EXTRACT_JS,
    listing_from_payload,
    vehicle_detail_from_payload,
)

logger = logging.getLogger("crawlrx.cars_com")
router = APIRouter(prefix="/scrape/cars", tags=["cars"])

# Narrow exception families. Playwright + parse + runtime errors during a
# scrape are recoverable; anything else propagates.
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

class CarsSearchRequest(BaseModel):
    """Request model for POST /scrape/cars/search"""

    make: Optional[str] = Field(default=None, description="Vehicle make (e.g., 'Toyota')")
    model: Optional[str] = Field(default=None, description="Vehicle model (e.g., 'Camry')")
    zip_code: str = Field(default="40202", description="ZIP code for search location")
    max_price: Optional[int] = Field(default=None, description="Maximum price filter")
    min_year: Optional[int] = Field(default=None, description="Minimum year filter")
    max_year: Optional[int] = Field(default=None, description="Maximum year filter")
    max_mileage: Optional[int] = Field(default=None, description="Maximum mileage filter")
    listing_type: str = Field(
        default="any", description="Listing type: 'used', 'new', 'certified', or 'any'"
    )
    limit: int = Field(default=20, ge=1, le=50, description="Maximum listings to return")


class CarsSearchResponse(BaseModel):
    """Response model for POST /scrape/cars/search"""

    success: bool
    listings: List[CarListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class CarsVehicleRequest(BaseModel):
    """Request model for POST /scrape/cars/vehicle"""

    url: str = Field(..., description="Full Cars.com vehicle listing URL")


class CarsVehicleResponse(BaseModel):
    """Response model for POST /scrape/cars/vehicle"""

    success: bool
    vehicle: Optional[VehicleDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


__all__ = (
    "CarListing",
    "DealerInfo",
    "PriceHistoryItem",
    "VehicleDetail",
    "CarsSearchRequest",
    "CarsSearchResponse",
    "CarsVehicleRequest",
    "CarsVehicleResponse",
    "search_cars",
    "scrape_cars_vehicle",
)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Sleep for random duration between min_ms and max_ms."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


def _listing_path_segment(listing_type: str) -> str:
    """Map a listing-type filter to its URL path segment."""
    if listing_type == "used":
        return "used/"
    if listing_type == "new":
        return "new/"
    if listing_type == "certified":
        return "used/"
    return ""


def _build_search_url(args: CarsSearchArgs) -> str:
    """Build Cars.com search URL with filters."""
    parts = ["https://www.cars.com/shopping/", _listing_path_segment(args.listing_type)]
    if args.make:
        parts.append(args.make.lower().replace(" ", "-") + "/")
        if args.model:
            parts.append(args.model.lower().replace(" ", "-") + "/")
    base_url = "".join(parts)

    params = [f"zip={args.zip_code}"]
    if args.max_price is not None:
        params.append(f"list_price_max={args.max_price}")
    if args.min_year is not None:
        params.append(f"year_min={args.min_year}")
    if args.max_year is not None:
        params.append(f"year_max={args.max_year}")
    if args.max_mileage is not None:
        params.append(f"mileage_max={args.max_mileage}")
    if args.listing_type == "certified":
        params.append("stock_type=cpo")
    return f"{base_url}?{'&'.join(params)}"


async def _parse_search_listings(page: Any, limit: int) -> List[CarListing]:
    """Parse search results from Cars.com page."""
    try:
        await page.wait_for_selector(
            "[data-testid='listing-card'], .vehicle-card, .sds-listing-card",
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for listings to load")
        return []
    await _random_delay(800, 1500)
    listings_data = await page.evaluate(LISTING_EXTRACT_JS)
    return [listing_from_payload(data) for data in listings_data[:limit]]


async def _parse_vehicle_detail(page: Any) -> VehicleDetail:
    """Parse single vehicle detail page."""
    await page.wait_for_selector(
        "h1, .vehicle-title, [data-testid='vehicle-title']",
        timeout=20000,
    )
    await _random_delay(1000, 2000)
    return vehicle_detail_from_payload(await page.evaluate(VEHICLE_EXTRACT_JS))


@asynccontextmanager
async def _stealth_session() -> AsyncIterator[Any]:
    """Open a stealth Playwright session and yield the live page."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            yield page
    finally:
        await close_stealth_context(browser, ctx, page)


async def _run_in_session(
    label: str,
    body: Callable[[Any], Awaitable[T]],
    fail: Callable[[str, str, int], T],
) -> T:
    """Run ``body`` inside a stealth session; map known failures via ``fail``."""
    start_time = time.time()
    try:
        async with _stealth_session() as page:
            return await body(page)
    except PlaywrightTimeoutError as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("%s timeout: %s", label, exc)
        return fail("timeout", str(exc), elapsed)
    except _SCRAPE_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Error %s (%s): %s", label, type(exc).__name__, exc)
        return fail("error", str(exc), elapsed)


def _search_failure(kind: str, message: str, elapsed: int) -> CarsSearchResponse:
    """Build a search-failure response (timeout or error)."""
    error = f"Timeout: {message}" if kind == "timeout" else message
    return CarsSearchResponse(success=False, error=error, elapsed_ms=elapsed)


def _vehicle_failure(kind: str, message: str, elapsed: int) -> CarsVehicleResponse:
    """Build a vehicle-failure response (timeout or error)."""
    error = f"Timeout: {message}" if kind == "timeout" else message
    return CarsVehicleResponse(success=False, error=error, elapsed_ms=elapsed)


async def _do_search_body(
    page: Any, args: CarsSearchArgs, start_time: float
) -> CarsSearchResponse:
    """Inner search body: navigate + parse listings + build success response."""
    search_url = _build_search_url(args)
    logger.info("Searching Cars.com: %s", search_url[:100])
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await _random_delay(1500, 2500)
    listings = await _parse_search_listings(page, args.limit)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("Found %d car listings in %dms", len(listings), elapsed)
    return CarsSearchResponse(
        success=True,
        listings=listings,
        count=len(listings),
        elapsed_ms=elapsed,
    )


async def _do_vehicle_body(
    page: Any, url: str, start_time: float
) -> CarsVehicleResponse:
    """Inner vehicle body: navigate + parse detail + build success response."""
    logger.info("Scraping Cars.com vehicle: %s", url[:100])
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
    return CarsVehicleResponse(
        success=True,
        vehicle=vehicle_detail,
        elapsed_ms=elapsed,
    )


async def _do_search_cars(args: CarsSearchArgs) -> CarsSearchResponse:
    """Run a Cars.com search inside a stealth Playwright session."""
    start_time = time.time()
    return await _run_in_session(
        "Cars.com search",
        lambda page: _do_search_body(page, args, start_time),
        _search_failure,
    )


async def _do_scrape_vehicle(url: str) -> CarsVehicleResponse:
    """Scrape one Cars.com vehicle URL inside a stealth Playwright session."""
    start_time = time.time()
    return await _run_in_session(
        "Cars.com vehicle scrape",
        lambda page: _do_vehicle_body(page, url, start_time),
        _vehicle_failure,
    )


# ─────────────────────────────────────────────
# Endpoints
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
        logger.error(
            "%s request failed (%s): %s", label, type(exc).__name__, exc
        )
        return fail(f"Request failed: {exc}")


def _search_args_from(request: CarsSearchRequest) -> CarsSearchArgs:
    """Pull request fields onto the dataclass arg bag."""
    return CarsSearchArgs(
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
    response_model=CarsSearchResponse,
    summary="Search Cars.com listings",
)
async def search_cars(request: CarsSearchRequest) -> CarsSearchResponse:
    """Search Cars.com vehicle listings (60s wall-clock cap)."""
    args = _search_args_from(request)
    return await _run_with_request_timeout(
        "Cars.com search",
        lambda err: CarsSearchResponse(success=False, error=err, elapsed_ms=60000),
        lambda: _do_search_cars(args),
    )


@router.post(
    "/vehicle",
    response_model=CarsVehicleResponse,
    summary="Scrape Cars.com vehicle detail",
)
async def scrape_cars_vehicle(request: CarsVehicleRequest) -> CarsVehicleResponse:
    """Scrape detailed information from a single Cars.com vehicle listing."""
    if not request.url:
        return CarsVehicleResponse(
            success=False,
            error="'url' is required",
            elapsed_ms=0,
        )
    return await _run_with_request_timeout(
        "Cars.com vehicle scrape",
        lambda err: CarsVehicleResponse(success=False, error=err, elapsed_ms=60000),
        lambda: _do_scrape_vehicle(request.url),
    )
