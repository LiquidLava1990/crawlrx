"""tripadvisor.py -- TripAdvisor scraper.

Endpoints:
  POST /scrape/tripadvisor/search  -- search hotels/restaurants/attractions
  POST /scrape/tripadvisor/reviews -- fetch reviews for a listing

Uses Playwright CDP 9229 with stealth delays.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from .config import USER_AGENTS
from .stealth_context import make_stealth_context, close_stealth_context
from . import _tripadvisor_js as _ta_js

logger = logging.getLogger("crawlrx.tripadvisor")
router = APIRouter(prefix="/scrape/tripadvisor", tags=["tripadvisor"])

TRIPADVISOR_BASE_URL = "https://www.tripadvisor.com"


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class TripAdvisorSearchRequest(BaseModel):
    """Request model for POST /scrape/tripadvisor/search."""
    query: str = Field(..., description="Search query")
    location: str = Field(..., description="Location (e.g., 'Paris')")
    category: str = Field(default="all", description="hotels/restaurants/attractions/all")
    limit: int = Field(default=10, ge=1, le=30, description="Max results")
    timeout: int = Field(default=30000, ge=5000, le=60000, description="Timeout ms")


class TripAdvisorReviewRequest(BaseModel):
    """Request model for POST /scrape/tripadvisor/reviews."""
    url: str = Field(..., description="TripAdvisor listing URL")
    limit: int = Field(default=10, ge=1, le=50, description="Max reviews")
    timeout: int = Field(default=30000, ge=5000, le=60000, description="Timeout ms")


class TripAdvisorResult(BaseModel):
    """Single TripAdvisor search result."""
    name: str = Field(default="")
    category: str = Field(default="")
    rating: Optional[float] = Field(default=None)
    review_count: int = Field(default=0)
    price_range: str = Field(default="")
    address: str = Field(default="")
    listing_url: str = Field(default="")
    image_url: Optional[str] = Field(default=None)
    ranking: Optional[str] = Field(default=None)
    awards: List[str] = Field(default_factory=list)


class TripAdvisorReview(BaseModel):
    """Single TripAdvisor review."""
    title: str = Field(default="")
    rating: Optional[float] = Field(default=None)
    text: str = Field(default="")
    author: str = Field(default="")
    date: str = Field(default="")
    trip_type: Optional[str] = Field(default=None)
    helpful_votes: int = Field(default=0)


class TripAdvisorSearchResponse(BaseModel):
    """Response model for TripAdvisor search."""
    success: bool
    results: List[TripAdvisorResult] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = Field(default=None)


class TripAdvisorReviewsResponse(BaseModel):
    """Response model for TripAdvisor reviews."""
    success: bool
    reviews: List[TripAdvisorReview] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = Field(default=None)


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

# Concrete Playwright + asyncio failure surface (R-05 narrowed).
_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PlaywrightTimeoutError, OSError, RuntimeError, ValueError,
    AttributeError, TypeError, KeyError, asyncio.TimeoutError,
)

_REQUEST_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError, asyncio.CancelledError, RuntimeError,
    OSError, ValueError, TypeError, AttributeError,
)


async def _random_delay(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Random stealth delay."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _build_search_url(query: str, location: str, category: str) -> str:
    """Build TripAdvisor search URL."""
    search_query = f"{query} {location}"
    category_filters = {
        "hotels": "&searchTarget=hotels",
        "restaurants": "&searchTarget=restaurants",
        "attractions": "&searchTarget=attractions",
        "all": "",
    }
    filter_param = category_filters.get(category.lower(), "")
    return f"{TRIPADVISOR_BASE_URL}/Search?q={quote(search_query)}{filter_param}"


# ─────────────────────────────────────────────
# Playwright Search Functions
# ─────────────────────────────────────────────

async def _wait_networkidle(page: Any, timeout_ms: int) -> None:
    """Best-effort networkidle wait that tolerates timeout."""
    try:
        await page.wait_for_load_state(
            "networkidle", timeout=min(timeout_ms // 2, 10000),
        )
    except PlaywrightTimeoutError:
        return


async def _open_search_page(
    p: Any, url: str, timeout_ms: int,
) -> tuple[Any, Any, Any]:
    """Boot stealth context, navigate, settle, return ``(browser, ctx, page)``."""
    browser, ctx, page = await make_stealth_context(p)
    await page.set_viewport_size({"width": 1280, "height": 800})
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _random_delay()
    await _wait_networkidle(page, timeout_ms)
    await _random_delay()
    return browser, ctx, page


async def _scroll_for_reviews(page: Any, scrolls: int = 3) -> None:
    """Scroll the page to lazy-load review cards."""
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(0.5)


@dataclass(frozen=True)
class _TAExtractSpec:
    """Bundle of per-call parameters for `_run_ta_playwright_extract`."""
    target_url: str
    timeout_ms: int
    pre_extract: Optional[Callable[[Any], Awaitable[None]]]
    extract_js: str
    extract_arg: int
    error_label: str


async def _run_ta_playwright_extract(spec: _TAExtractSpec) -> List[Dict[str, Any]]:
    """Open stealth page -> optional pre-extract hook -> page.evaluate -> raw rows."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await _open_search_page(
                p, spec.target_url, spec.timeout_ms,
            )
            if spec.pre_extract is not None:
                await spec.pre_extract(page)
            return await page.evaluate(spec.extract_js, spec.extract_arg)
    except _PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "[TripAdvisor] %s error: %s", spec.error_label, type(exc).__name__,
        )
        # explicit recovery: _PLAYWRIGHT_EXCEPTIONS non-fatal; caller treats as empty
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


async def _search_tripadvisor_playwright(
    query: str, location: str, category: str, limit: int, timeout_ms: int,
) -> List[TripAdvisorResult]:
    """Search TripAdvisor using Playwright CDP."""
    search_url = _build_search_url(query, location, category)
    logger.info(
        "[TripAdvisor] Playwright search: '%s' in '%s' (category=%s)",
        query, location, category,
    )
    spec = _TAExtractSpec(
        target_url=search_url,
        timeout_ms=timeout_ms,
        pre_extract=None,
        extract_js=_ta_js.SEARCH_EXTRACT_JS,
        extract_arg=limit,
        error_label="Playwright search",
    )
    raw_results = await _run_ta_playwright_extract(spec)
    results = [TripAdvisorResult(**item) for item in raw_results]
    logger.info("[TripAdvisor] Found %d results", len(results))
    return results


async def _scrape_reviews_playwright(
    url: str, limit: int, timeout_ms: int,
) -> List[TripAdvisorReview]:
    """Scrape reviews from a TripAdvisor listing using Playwright."""
    logger.info("[TripAdvisor] Scraping reviews from: %s", url)
    spec = _TAExtractSpec(
        target_url=url,
        timeout_ms=timeout_ms,
        pre_extract=_scroll_for_reviews,
        extract_js=_ta_js.REVIEWS_EXTRACT_JS,
        extract_arg=limit,
        error_label="Reviews scrape",
    )
    raw_reviews = await _run_ta_playwright_extract(spec)
    reviews = [TripAdvisorReview(**rev) for rev in raw_reviews]
    logger.info("[TripAdvisor] Scraped %d reviews", len(reviews))
    return reviews


# ─────────────────────────────────────────────
# Sync wrappers + endpoints
# ─────────────────────────────────────────────

_VALID_CATEGORIES: tuple[str, ...] = ("hotels", "restaurants", "attractions", "all")


async def _do_search_tripadvisor(
    query: str, location: str, category: str, limit: int, timeout_ms: int,
) -> TripAdvisorSearchResponse:
    """Validate + dispatch the search; return a fully-populated response."""
    start_time = time.time()
    if category.lower() not in _VALID_CATEGORIES:
        return TripAdvisorSearchResponse(
            success=False,
            error="Invalid category. Must be one of: %s" % str(_VALID_CATEGORIES),
            elapsed_ms=int((time.time() - start_time) * 1000),
        )
    try:
        results = await _search_tripadvisor_playwright(
            query, location, category, limit, timeout_ms,
        )
    except _REQUEST_FAILURE_EXCEPTIONS as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error("[TripAdvisor] Search error: %s", type(exc).__name__)
        return TripAdvisorSearchResponse(
            success=False, error=str(exc), elapsed_ms=elapsed_ms,
        )
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "[TripAdvisor] Search returned %d results in %dms", len(results), elapsed_ms,
    )
    return TripAdvisorSearchResponse(
        success=True, results=results, count=len(results), elapsed_ms=elapsed_ms,
    )


async def _do_scrape_reviews(
    url: str, limit: int, timeout_ms: int,
) -> TripAdvisorReviewsResponse:
    """Validate + dispatch the reviews scrape; return populated response."""
    start_time = time.time()
    if not url.startswith("https://www.tripadvisor.com/"):
        return TripAdvisorReviewsResponse(
            success=False,
            error="Invalid TripAdvisor URL. Must start with https://www.tripadvisor.com/",
            elapsed_ms=int((time.time() - start_time) * 1000),
        )
    try:
        reviews = await _scrape_reviews_playwright(url, limit, timeout_ms)
    except _REQUEST_FAILURE_EXCEPTIONS as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(
            "[TripAdvisor] Reviews scrape error for %s: %s", url, type(exc).__name__,
        )
        return TripAdvisorReviewsResponse(
            success=False, error=str(exc), elapsed_ms=elapsed_ms,
        )
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "[TripAdvisor] Scraped %d reviews in %dms", len(reviews), elapsed_ms,
    )
    return TripAdvisorReviewsResponse(
        success=True, reviews=reviews, count=len(reviews), elapsed_ms=elapsed_ms,
    )


@router.post(
    "/search", response_model=TripAdvisorSearchResponse, summary="Search TripAdvisor",
)
async def search_tripadvisor(
    request: TripAdvisorSearchRequest,
) -> TripAdvisorSearchResponse:
    """Search hotels/restaurants/attractions on TripAdvisor."""
    timeout_seconds = max(request.timeout // 1000, 10) + 5
    try:
        return await asyncio.wait_for(
            _do_search_tripadvisor(
                request.query, request.location, request.category,
                request.limit, request.timeout,
            ),
            timeout=timeout_seconds,
        )
    except _REQUEST_FAILURE_EXCEPTIONS as exc:
        logger.error("[TripAdvisor] Search request failed: %s", type(exc).__name__)
        return TripAdvisorSearchResponse(
            success=False,
            error="Request failed: %s" % type(exc).__name__,
            elapsed_ms=request.timeout,
        )


@router.post(
    "/reviews", response_model=TripAdvisorReviewsResponse,
    summary="Scrape TripAdvisor reviews",
)
async def scrape_reviews(
    request: TripAdvisorReviewRequest,
) -> TripAdvisorReviewsResponse:
    """Get reviews for a specific TripAdvisor listing."""
    timeout_seconds = max(request.timeout // 1000, 10) + 5
    try:
        return await asyncio.wait_for(
            _do_scrape_reviews(request.url, request.limit, request.timeout),
            timeout=timeout_seconds,
        )
    except _REQUEST_FAILURE_EXCEPTIONS as exc:
        logger.error("[TripAdvisor] Reviews request failed: %s", type(exc).__name__)
        return TripAdvisorReviewsResponse(
            success=False,
            error="Request failed: %s" % type(exc).__name__,
            elapsed_ms=request.timeout,
        )
