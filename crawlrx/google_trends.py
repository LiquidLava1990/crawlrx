"""
Google Trends Scraper -- CrawlRX
=================================
Google Trends data via pytrends library or Playwright fallback.

Endpoints:
  POST /scrape/trends/interest -- get interest over time
  POST /scrape/trends/related -- get related queries and topics
  POST /scrape/trends/trending -- get currently trending searches
"""

import asyncio
import logging
import random
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["google-trends"])

# Try to import pytrends
PYTRENDS_AVAILABLE = False
try:
    from pytrends.request import TrendReq
    PYTRENDS_AVAILABLE = True
    logger.info("pytrends library available")
except ImportError:
    logger.debug("pytrends not installed -- using the Playwright fallback (pip install 'crawlrx[extras]')")


# pytrends raises a wide variety of HTTP / decode / shape errors. We narrow
# to a stable tuple so a swallow site never accidentally hides an unrelated
# bug (e.g. AttributeError from a typo).
_PYTRENDS_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    LookupError,
    RuntimeError,
)


# ─────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────

class TrendsInterestRequest(BaseModel):
    """Request model for POST /scrape/trends/interest"""
    keywords: List[str] = Field(..., min_length=1, max_length=5, description="List of keywords (max 5)")
    timeframe: str = Field(default="today 1-m", description="Timeframe: 'now 1-d', 'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m'")
    geo: str = Field(default="US", description="Geographic region (e.g., 'US', 'GB', 'CA')")


class TrendsRelatedRequest(BaseModel):
    """Request model for POST /scrape/trends/related"""
    keyword: str = Field(..., description="Keyword to analyze")
    geo: str = Field(default="US", description="Geographic region (e.g., 'US', 'GB', 'CA')")
    timeframe: str = Field(default="today 1-m", description="Timeframe: 'now 1-d', 'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m'")


class TrendsTrendingRequest(BaseModel):
    """Request model for POST /scrape/trends/trending"""
    geo: str = Field(default="US", description="Geographic region (e.g., 'US', 'GB', 'CA')")
    limit: int = Field(default=20, ge=1, le=50, description="Number of trending searches to return")


class TrendsInterestResponse(BaseModel):
    """Response model for interest over time"""
    success: bool
    data: Optional[Dict[str, List[Dict[str, Any]]]] = None
    elapsed_ms: int
    error: Optional[str] = None


class TrendsRelatedResponse(BaseModel):
    """Response model for related queries/topics"""
    success: bool
    rising_queries: List[Dict[str, Any]] = Field(default_factory=list)
    top_queries: List[Dict[str, Any]] = Field(default_factory=list)
    related_topics: List[Dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int
    error: Optional[str] = None


class TrendsTrendingResponse(BaseModel):
    """Response model for trending searches"""
    success: bool
    trending: List[Dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int
    error: Optional[str] = None


# ─────────────────────────────────────────────
# pytrends Implementation
# ─────────────────────────────────────────────

def _get_pytrends_client():
    """Create a pytrends client with proper headers."""
    return TrendReq(
        hl='en-US',
        tz=360,
        timeout=(10, 25),
        retries=2,
        backoff_factor=0.1,
    )


def _format_interest_date(value: Any) -> str:
    """Format an interest-timeline date column for output."""
    return value.strftime("%Y-%m-%d") if isinstance(value, datetime) else str(value)


def _interest_timeline(series) -> List[Dict[str, Any]]:
    """Convert a pytrends interest series into ``[{date, value}]`` rows."""
    return [
        {"date": _format_interest_date(date), "value": int(value)}
        for date, value in series.items()
    ]


def _fetch_interest_pytrends(keywords: List[str], timeframe: str, geo: str) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch interest over time using pytrends."""
    pytrends = _get_pytrends_client()
    pytrends.build_payload(keywords, cat=0, timeframe=timeframe, geo=geo)
    interest_df = pytrends.interest_over_time()

    if interest_df.empty:
        return {kw: [] for kw in keywords}

    return {
        keyword: _interest_timeline(interest_df[keyword]) if keyword in interest_df.columns else []
        for keyword in keywords
    }


def _query_rows(rising_df) -> List[Dict[str, Any]]:
    """Convert a related-queries DataFrame into ``[{query, value}]`` rows."""
    if rising_df is None or rising_df.empty:
        return []
    return [
        {"query": row.get("query", ""), "value": int(row.get("value", 0))}
        for _, row in rising_df.head(10).iterrows()
    ]


def _topic_rows(rising_topics) -> List[Dict[str, Any]]:
    """Convert a related-topics DataFrame into ``[{topic, type, value}]`` rows."""
    if rising_topics is None or rising_topics.empty:
        return []
    return [
        {
            "topic": row.get("topic_title", ""),
            "type": row.get("topic_type", ""),
            "value": int(row.get("value", 0)),
        }
        for _, row in rising_topics.head(10).iterrows()
    ]


def _safe_pytrends_call(label: str, fn: Callable[[], Any]) -> Any:
    """Run a pytrends fetch, returning ``None`` and logging if it fails."""
    try:
        return fn()
    except _PYTRENDS_FETCH_EXCEPTIONS as exc:
        logger.warning("Error fetching %s: %s", label, exc)
        return None


def _collect_related_queries(related: Dict[str, Any], keyword: str, bucket: str) -> List[Dict[str, Any]]:
    """Pull a single bucket (rising/top) from a pytrends related-queries result."""
    if not related or keyword not in related:
        return []
    payload = related[keyword]
    if not payload or bucket not in payload:
        return []
    return _query_rows(payload[bucket])


def _collect_related_topics(topics: Dict[str, Any], keyword: str) -> List[Dict[str, Any]]:
    """Pull rising topics out of a pytrends related-topics result."""
    if not topics or keyword not in topics:
        return []
    payload = topics[keyword]
    if not payload or "rising" not in payload:
        return []
    return _topic_rows(payload["rising"])


def _fetch_related_pytrends(keyword: str, timeframe: str, geo: str) -> Dict[str, Any]:
    """Fetch related queries and topics using pytrends."""
    pytrends = _get_pytrends_client()
    pytrends.build_payload([keyword], cat=0, timeframe=timeframe, geo=geo)

    related = _safe_pytrends_call("related queries", pytrends.related_queries)
    topics = _safe_pytrends_call("related topics", pytrends.related_topics)

    return {
        "rising_queries": _collect_related_queries(related or {}, keyword, "rising"),
        "top_queries": _collect_related_queries(related or {}, keyword, "top"),
        "related_topics": _collect_related_topics(topics or {}, keyword),
    }


def _trending_row(idx: Any, row) -> Dict[str, Any]:
    """Shape a single pytrends trending DataFrame row for the API response."""
    keyword = row.iloc[0] if len(row) > 0 else str(row)
    return {
        "rank": idx + 1,
        "keyword": keyword,
        "traffic_volume": None,
        "articles": [],
        "related_queries": [],
    }


def _fetch_trending_pytrends(geo: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch trending searches using pytrends."""
    pytrends = _get_pytrends_client()
    try:
        trending_df = pytrends.trending_searches(pn=geo.lower())
    except _PYTRENDS_FETCH_EXCEPTIONS as exc:
        logger.warning("Error fetching trending searches via pytrends: %s", exc)
        return []
    if trending_df is None or trending_df.empty:
        return []
    return [_trending_row(idx, row) for idx, row in trending_df.head(limit).iterrows()]


# ─────────────────────────────────────────────
# Playwright Fallback Implementation
# ─────────────────────────────────────────────

_TRENDING_VOLUME_PATTERN = re.compile(r"([\d.,]+[KMB]?)\+?")


async def _trending_item_title(item) -> Optional[str]:
    """Resolve the title for a trending list item, scanning known selectors."""
    for sel in (".mib-hve", "h3", ".title", "[role='heading']", "span"):
        title_elem = item.query_selector(sel)
        if not title_elem:
            continue
        title = title_elem.inner_text().strip()
        if title:
            return title
    return None


async def _trending_item_volume(item) -> Optional[str]:
    """Pull a normalized traffic-volume token off a trending list item."""
    for sel in (".sub-title", ".volume", "span span"):
        vol_elem = item.query_selector(sel)
        if not vol_elem:
            continue
        match = _TRENDING_VOLUME_PATTERN.search(vol_elem.inner_text().strip())
        if match:
            return match.group(1)
    return None


async def _scan_trending_items(page) -> list:
    """Try each known trending selector until one returns elements."""
    for selector in ("[data-row-id]", ".trending-item", "[role='listitem']", "mib-hve"):
        items = await page.query_selector_all(selector)
        if items:
            return items
    return []


_TRENDING_ITEM_SUPPRESS = SELECTOR_EXTRACTION_EXCEPTIONS + PLAYWRIGHT_EXCEPTIONS


async def _build_trending_row(idx: int, item) -> Optional[Dict[str, Any]]:
    """Build one trending dict from a Playwright element handle."""
    title = await _trending_item_title(item)
    if not title:
        return None
    return {
        "rank": idx,
        "keyword": title,
        "traffic_volume": await _trending_item_volume(item),
        "articles": [],
        "related_queries": [],
    }


async def _safe_build_trending_row(idx: int, item) -> Optional[Dict[str, Any]]:
    """``_build_trending_row`` with the per-item suppress applied."""
    try:
        return await _build_trending_row(idx, item)
    except _TRENDING_ITEM_SUPPRESS as exc:
        logger.debug("Error parsing trending item: %s", exc)
        return None


async def _collect_trending_rows(items: list, limit: int) -> List[Dict[str, Any]]:
    """Walk the trending items and assemble the response rows."""
    rows: List[Dict[str, Any]] = []
    for idx, item in enumerate(items[:limit], 1):
        row = await _safe_build_trending_row(idx, item)
        if row is not None:
            rows.append(row)
    return rows


async def _fetch_trending_playwright(geo: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch trending searches using Playwright."""
    from playwright.async_api import async_playwright

    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            url = f"https://trends.google.com/trending?geo={geo}"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(random.uniform(1, 2))
            items = await _scan_trending_items(page)
            return await _collect_trending_rows(items, limit)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Error fetching trending via Playwright: %s", exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

_TRENDS_HANDLER_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    RuntimeError,
    *PARSE_EXCEPTIONS,
)


def _elapsed_ms(start: float) -> int:
    """Whole-millisecond span since ``start`` (a ``time.time()`` reading)."""
    return int((time.time() - start) * 1000)


async def _run_trends_endpoint(
    start: float,
    fetcher: Callable[[], Any] | Callable[[], Awaitable[Any]],
    on_success: Callable[[Any], Any],
    error_response: Callable[[str], Any],
    label: str,
):
    """Generic trends endpoint runner: fetch -> shape success / error response."""
    try:
        data = await asyncio.to_thread(fetcher)
    except _TRENDS_HANDLER_EXCEPTIONS as exc:
        logger.error("Error fetching %s: %s", label, exc)
        return error_response(str(exc))
    return on_success(data)


@router.post("/scrape/trends/interest", response_model=TrendsInterestResponse)
async def get_interest_over_time(request: TrendsInterestRequest) -> object:
    """Get Google Trends interest over time for keywords."""
    start = time.time()
    if not PYTRENDS_AVAILABLE:
        return TrendsInterestResponse(
            success=False,
            error="pytrends not installed. Run: pip install 'crawlrx[extras]'",
            elapsed_ms=_elapsed_ms(start),
        )

    def _fetch():
        return _fetch_interest_pytrends(request.keywords, request.timeframe, request.geo)

    def _on_success(data):
        return TrendsInterestResponse(success=True, data=data, elapsed_ms=_elapsed_ms(start))

    def _on_error(msg: str):
        return TrendsInterestResponse(success=False, error=msg, elapsed_ms=_elapsed_ms(start))

    return await _run_trends_endpoint(start, _fetch, _on_success, _on_error, "interest over time")


@router.post("/scrape/trends/related", response_model=TrendsRelatedResponse)
async def get_related_queries(request: TrendsRelatedRequest) -> object:
    """Get related queries and topics for a keyword."""
    start = time.time()
    if not PYTRENDS_AVAILABLE:
        return TrendsRelatedResponse(
            success=False,
            error="pytrends not installed. Run: pip install 'crawlrx[extras]'",
            elapsed_ms=_elapsed_ms(start),
        )

    def _fetch():
        return _fetch_related_pytrends(request.keyword, request.timeframe, request.geo)

    def _on_success(data):
        return TrendsRelatedResponse(
            success=True,
            rising_queries=data["rising_queries"],
            top_queries=data["top_queries"],
            related_topics=data["related_topics"],
            elapsed_ms=_elapsed_ms(start),
        )

    def _on_error(msg: str):
        return TrendsRelatedResponse(success=False, error=msg, elapsed_ms=_elapsed_ms(start))

    return await _run_trends_endpoint(start, _fetch, _on_success, _on_error, "related queries")


@router.post("/scrape/trends/trending", response_model=TrendsTrendingResponse)
async def get_trending_searches(request: TrendsTrendingRequest) -> object:
    """Get currently trending searches."""
    start = time.time()

    def _fetch():
        if PYTRENDS_AVAILABLE:
            return _fetch_trending_pytrends(request.geo, request.limit)
        return _fetch_trending_playwright(request.geo, request.limit)

    def _on_success(data):
        return TrendsTrendingResponse(success=True, trending=data, elapsed_ms=_elapsed_ms(start))

    def _on_error(msg: str):
        return TrendsTrendingResponse(success=False, error=msg, elapsed_ms=_elapsed_ms(start))

    return await _run_trends_endpoint(start, _fetch, _on_success, _on_error, "trending searches")
