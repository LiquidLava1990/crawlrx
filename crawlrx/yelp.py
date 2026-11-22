"""
yelp.py -- Yelp Scraper
======================
Endpoints: POST /scrape/yelp/search
           POST /scrape/yelp/business

Strategy:
  Search  -- Playwright CDP (JS-rendered results) with httpx mobile fallback
  Business -- Playwright CDP for full business detail + review pagination
"""

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, TypeVar
from urllib.parse import quote

import httpx
from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from .config import USER_AGENTS
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import playwright_exception_types
from ._yelp_helpers import (
    BUSINESS_EXTRACT_JS,
    REVIEWS_EXTRACT_JS,
    SEARCH_EXTRACT_JS,
    parse_mobile_search_html,
)

logger = logging.getLogger("crawlrx.yelp")
router = APIRouter(prefix="/scrape/yelp", tags=["yelp"])

# Narrow exception families.
_SCRAPE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError, ValueError, KeyError, TypeError)
)
_HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    OSError,
    UnicodeDecodeError,
    ValueError,
)
_REQUEST_FAIL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    RuntimeError,
    OSError,
)

T = TypeVar("T")


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class YelpSearchRequest(BaseModel):
    query: str = Field(..., description="Search query (e.g., 'coffee', 'plumber')")
    location: str = Field(..., description="Location (e.g., 'San Francisco, CA')")
    limit: int = Field(default=10, ge=1, le=30, description="Maximum businesses to return")
    sort_by: str = Field(default="best_match", description="Sort: best_match|rating|review_count|distance")
    timeout: int = Field(default=30000, ge=5000, le=60000, description="Timeout in ms")


class YelpBusinessRequest(BaseModel):
    url: str = Field(..., description="Yelp business URL")
    include_reviews: bool = Field(default=True, description="Fetch top 10 reviews (paginates if needed)")
    timeout: int = Field(default=30000, ge=5000, le=60000, description="Timeout in ms")


class YelpBusiness(BaseModel):
    name: str = Field(default="")
    rating: Optional[float] = None
    review_count: int = Field(default=0)
    price_range: str = Field(default="")
    categories: List[str] = Field(default_factory=list)
    address: str = Field(default="")
    phone: str = Field(default="")
    url: str = Field(default="")
    is_closed: bool = Field(default=False)
    hours_summary: str = Field(default="")


class YelpBusinessDetail(YelpBusiness):
    hours: Dict[str, str] = Field(default_factory=dict)
    about: str = Field(default="")
    top_reviews: List[Dict[str, Any]] = Field(default_factory=list)
    photos_count: int = Field(default=0)
    website: str = Field(default="")


class YelpSearchResponse(BaseModel):
    success: bool
    businesses: List[YelpBusiness] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = None


class YelpBusinessResponse(BaseModel):
    success: bool
    business: Optional[YelpBusinessDetail] = None
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = None


_VALID_SORTS = ("best_match", "rating", "review_count", "distance")


@dataclass(frozen=True)
class _SearchArgs:
    """Bundle of Yelp search parameters; keeps inner helpers ≤5 args."""

    query: str
    location: str
    limit: int
    sort_by: str
    timeout_ms: int


async def _random_delay(min_ms: int = 300, max_ms: int = 900) -> None:
    """Sleep a random number of milliseconds in ``[min_ms, max_ms]``."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _get_ua() -> str:
    """Pick a random user-agent from the configured pool."""
    return random.choice(USER_AGENTS)


# ─────────────────────────────────────────────
# Stealth session
# ─────────────────────────────────────────────


@asynccontextmanager
async def _stealth_session() -> AsyncIterator[Any]:
    """Open a stealth Playwright session and yield the live page."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.set_viewport_size({"width": 1280, "height": 800})
            yield page
    finally:
        await close_stealth_context(browser, ctx, page)


async def _wait_networkidle(page: Any, timeout_ms: int) -> None:
    """Best-effort ``networkidle`` wait that swallows the timeout."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


# ─────────────────────────────────────────────
# Playwright Search
# ─────────────────────────────────────────────


def _search_url(query: str, location: str, sort_by: str) -> str:
    """Build the standard Yelp search URL."""
    return "https://www.yelp.com/search?find_desc=%s&find_loc=%s&sortby=%s" % (
        quote(query),
        quote(location),
        sort_by,
    )


async def _do_search_extract(page: Any, args: _SearchArgs) -> List[YelpBusiness]:
    """Navigate + run the search-results JS extractor."""
    search_url = _search_url(args.query, args.location, args.sort_by)
    logger.info("[Yelp] Search: '%s' in '%s'", args.query, args.location)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
    await _random_delay()
    await _wait_networkidle(page, min(args.timeout_ms // 2, 10000))
    raw_results = await page.evaluate(SEARCH_EXTRACT_JS, args.limit)
    return [YelpBusiness(**biz_data) for biz_data in raw_results]


async def _search_yelp_playwright(args: _SearchArgs) -> List[YelpBusiness]:
    """Search Yelp via Playwright; returns ``[]`` on a controlled scrape failure."""
    try:
        async with _stealth_session() as page:
            return await _do_search_extract(page, args)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[Yelp] Playwright search error (%s): %s", type(exc).__name__, exc
        )
        return []


# ─────────────────────────────────────────────
# HTTP Mobile Fallback
# ─────────────────────────────────────────────


async def _search_yelp_http(
    query: str, location: str, limit: int, sort_by: str
) -> List[YelpBusiness]:
    """Search Yelp via mobile site HTTP -- fallback when Playwright is empty."""
    search_url = "https://m.yelp.com/search?find_desc=%s&find_loc=%s&sortby=%s" % (
        quote(query),
        quote(location),
        sort_by,
    )
    headers = {
        "User-Agent": _get_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
    }
    logger.info("[Yelp] HTTP fallback search: '%s' in '%s'", query, location)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(search_url, headers=headers)
        resp.raise_for_status()
        html = resp.text
    return [YelpBusiness(**biz) for biz in parse_mobile_search_html(html, limit)]


# ─────────────────────────────────────────────
# Business Detail + Review Pagination
# ─────────────────────────────────────────────


async def _try_paginate_reviews(page: Any, accumulated: List[Dict[str, Any]]) -> None:
    """Click "Next" once + extract one more review page, capping at 10 total."""
    try:
        next_btn = await page.query_selector(
            "a[aria-label='Next'], button[aria-label='Next'], a[class*='next-link']"
        )
    except _SCRAPE_EXCEPTIONS as exc:
        logger.debug("[Yelp] Review next-button lookup error: %s", exc)
        return
    if not next_btn:
        return
    try:
        await next_btn.click()
    except _SCRAPE_EXCEPTIONS as exc:
        logger.debug("[Yelp] Review pagination click failed: %s", exc)
        return
    await _random_delay(500, 1200)
    await _wait_networkidle(page, 8000)
    try:
        more_reviews = await page.evaluate(REVIEWS_EXTRACT_JS)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.debug("[Yelp] Review page2 extract failed: %s", exc)
        return
    for review in more_reviews:
        if len(accumulated) >= 10:
            return
        accumulated.append(review)


async def _collect_reviews(page: Any) -> List[Dict[str, Any]]:
    """Collect up to 10 reviews from the current page (with one paginate)."""
    all_reviews: List[Dict[str, Any]] = []
    page1 = await page.evaluate(REVIEWS_EXTRACT_JS)
    all_reviews.extend(page1)
    if len(all_reviews) < 10:
        await _try_paginate_reviews(page, all_reviews)
    return all_reviews[:10]


async def _do_business_extract(
    page: Any, url: str, include_reviews: bool, timeout_ms: int
) -> YelpBusinessDetail:
    """Navigate + run the business-detail JS extractor + (optionally) reviews."""
    logger.info("[Yelp] Scraping business: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _random_delay()
    await _wait_networkidle(page, min(timeout_ms // 2, 10000))
    await _random_delay()
    raw_data = await page.evaluate(BUSINESS_EXTRACT_JS)
    raw_data["top_reviews"] = (
        await _collect_reviews(page) if include_reviews else []
    )
    return YelpBusinessDetail(**raw_data)


async def _scrape_business_page(
    url: str, include_reviews: bool, timeout_ms: int
) -> Optional[YelpBusinessDetail]:
    """Scrape a Yelp business detail page (with optional review pagination)."""
    try:
        async with _stealth_session() as page:
            return await _do_business_extract(page, url, include_reviews, timeout_ms)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[Yelp] Business page error for %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
        )
        return None


# ─────────────────────────────────────────────
# Async "do" wrappers
# ─────────────────────────────────────────────


async def _run_search_with_fallback(args: _SearchArgs) -> List[YelpBusiness]:
    """Playwright search; if empty, retry via the mobile HTTP fallback."""
    businesses = await _search_yelp_playwright(args)
    if businesses:
        return businesses
    logger.info("[Yelp] Playwright returned 0 results, trying HTTP fallback")
    try:
        return await _search_yelp_http(args.query, args.location, args.limit, args.sort_by)
    except _HTTP_EXCEPTIONS as exc:
        logger.warning(
            "[Yelp] HTTP fallback also failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return []


async def _do_search_yelp(args: _SearchArgs) -> YelpSearchResponse:
    """Async Yelp search wrapper (validates sort + falls back on empties)."""
    start_time = time.time()
    if args.sort_by not in _VALID_SORTS:
        return YelpSearchResponse(
            success=False,
            error="Invalid sort_by. Must be one of: %s" % str(_VALID_SORTS),
            elapsed_ms=0,
        )
    try:
        businesses = await _run_search_with_fallback(args)
    except _SCRAPE_EXCEPTIONS as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(
            "[Yelp] Search error (%s): %s", type(exc).__name__, exc
        )
        return YelpSearchResponse(
            success=False, error=str(exc), elapsed_ms=elapsed_ms
        )
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "[Yelp] Search returned %d businesses in %dms", len(businesses), elapsed_ms
    )
    return YelpSearchResponse(
        success=True,
        businesses=businesses,
        count=len(businesses),
        elapsed_ms=elapsed_ms,
    )


def _is_valid_yelp_business_url(url: str) -> bool:
    """Check the URL is a wellfound/yelp business URL."""
    return url.startswith("https://www.yelp.com/biz/") or url.startswith(
        "https://m.yelp.com/biz/"
    )


async def _do_scrape_business(
    url: str, include_reviews: bool, timeout_ms: int
) -> YelpBusinessResponse:
    """Async Yelp business scrape wrapper."""
    start_time = time.time()
    if not _is_valid_yelp_business_url(url):
        return YelpBusinessResponse(
            success=False,
            error="Invalid Yelp URL. Must start with https://www.yelp.com/biz/",
            elapsed_ms=0,
        )
    try:
        business = await _scrape_business_page(url, include_reviews, timeout_ms)
    except _SCRAPE_EXCEPTIONS as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(
            "[Yelp] Business scrape error for %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
        )
        return YelpBusinessResponse(
            success=False, error=str(exc), elapsed_ms=elapsed_ms
        )
    elapsed_ms = int((time.time() - start_time) * 1000)
    if business is None:
        return YelpBusinessResponse(
            success=False,
            error="Failed to extract business data",
            elapsed_ms=elapsed_ms,
        )
    logger.info(
        "[Yelp] Scraped: %s, rating=%s, reviews=%d",
        business.name,
        business.rating,
        business.review_count,
    )
    return YelpBusinessResponse(
        success=True, business=business, elapsed_ms=elapsed_ms
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────


async def _run_with_request_timeout(
    label: str,
    timeout_seconds: int,
    fail: Callable[[str], T],
    body: Callable[[], Awaitable[T]],
) -> T:
    """Run ``body`` with a wall-clock cap + map known failures via ``fail``."""
    try:
        return await asyncio.wait_for(body(), timeout=timeout_seconds)
    except _REQUEST_FAIL_EXCEPTIONS as exc:
        logger.error("%s request failed (%s): %s", label, type(exc).__name__, exc)
        return fail("Request failed: %s" % exc)


@router.post(
    "/search",
    response_model=YelpSearchResponse,
    summary="Search Yelp businesses",
)
async def search_yelp(request: YelpSearchRequest) -> YelpSearchResponse:
    """Search for businesses on Yelp."""
    timeout_seconds = max(request.timeout // 1000, 10) + 5
    return await _run_with_request_timeout(
        "[Yelp] Search",
        timeout_seconds,
        lambda err: YelpSearchResponse(
            success=False,
            error=err,
            elapsed_ms=request.timeout,
        ),
        lambda: _do_search_yelp(_SearchArgs(
            query=request.query,
            location=request.location,
            limit=request.limit,
            sort_by=request.sort_by,
            timeout_ms=request.timeout,
        )),
    )


@router.post(
    "/business",
    response_model=YelpBusinessResponse,
    summary="Scrape Yelp business details",
)
async def scrape_business(request: YelpBusinessRequest) -> YelpBusinessResponse:
    """Get detailed information about a specific Yelp business."""
    timeout_seconds = max(request.timeout // 1000, 10) + 5
    return await _run_with_request_timeout(
        "[Yelp] Business",
        timeout_seconds,
        lambda err: YelpBusinessResponse(
            success=False,
            error=err,
            elapsed_ms=request.timeout,
        ),
        lambda: _do_scrape_business(
            request.url, request.include_reviews, request.timeout
        ),
    )
