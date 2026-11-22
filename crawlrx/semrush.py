"""
SEMrush Scraper -- CrawlRX
===========================
SEMrush domain and keyword data via Playwright.
Requires SEMrush login (already logged in browser).

Endpoints:
  POST /scrape/semrush/domain   -- get domain overview
  POST /scrape/semrush/keywords -- get keyword data
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
from urllib.parse import quote

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS
from ._search_helpers import SERP_BROWSER_EXCEPTIONS
from ._semrush_helpers import (
    DOMAIN_AUTHORITY_SELECTORS,
    DOMAIN_BACKLINK_SELECTORS,
    DOMAIN_KEYWORDS_SELECTORS,
    DOMAIN_PAID_SELECTORS,
    DOMAIN_TRAFFIC_SELECTORS,
    DomainOverview,
    collect_top_keywords,
    collect_top_pages,
    collect_trend_bars,
    first_number,
    is_logged_in,
    normalize_domain,
    parse_keyword_metrics,
    random_delay,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["semrush"])

_NAV_TIMEOUT_MS = 60000


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class SEMrushDomainRequest(BaseModel):
    """Request model for POST /scrape/semrush/domain."""

    domain: str = Field(..., description="Domain to analyze (e.g., 'example.com')")


class SEMrushKeywordsRequest(BaseModel):
    """Request model for POST /scrape/semrush/keywords."""

    keywords: List[str] = Field(
        ..., min_length=1, max_length=100,
        description="List of keywords to analyze",
    )
    database: str = Field(
        default="us", description="Database to use (e.g., 'us', 'uk', 'ca')",
    )


class SEMrushDomainResponse(BaseModel):
    """Response model for domain overview."""

    success: bool
    overview: Optional[Dict[str, Any]] = None
    elapsed_ms: int
    error: Optional[str] = None


class SEMrushKeywordsResponse(BaseModel):
    """Response model for keyword data."""

    success: bool
    keywords: List[Dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int
    error: Optional[str] = None


# ─────────────────────────────────────────────
# Domain overview scrape
# ─────────────────────────────────────────────

_LOGIN_REQUIRED_MSG = (
    "SEMrush login required. Please log in via browser at semrush.com."
)
_LOGIN_REQUIRED_KEY = "NOT_LOGGED_IN"


async def _populate_domain_metrics(
    page: Any, overview: DomainOverview,
) -> None:
    overview.organic_traffic = await first_number(page, DOMAIN_TRAFFIC_SELECTORS)
    overview.paid_traffic = await first_number(page, DOMAIN_PAID_SELECTORS)
    overview.backlinks = await first_number(page, DOMAIN_BACKLINK_SELECTORS)
    overview.organic_keywords = await first_number(page, DOMAIN_KEYWORDS_SELECTORS)
    overview.domain_authority = await first_number(page, DOMAIN_AUTHORITY_SELECTORS)


async def _populate_domain_tables(page: Any, overview: DomainOverview) -> None:
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    await random_delay(1000, 1500)
    overview.top_keywords = await collect_top_keywords(page)
    overview.top_pages = await collect_top_pages(page)


async def _scrape_domain(page: Any, domain: str) -> Dict[str, Any]:
    clean_domain = normalize_domain(domain)
    url = (
        "https://www.semrush.com/analytics/overview/?q=%s&searchType=domain"
        % quote(clean_domain)
    )
    await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
    await random_delay(1500, 2500)
    if not await is_logged_in(page):
        return {"error": _LOGIN_REQUIRED_KEY, "message": _LOGIN_REQUIRED_MSG}
    overview = DomainOverview(domain=clean_domain)
    await _populate_domain_metrics(page, overview)
    await _populate_domain_tables(page, overview)
    return overview.to_dict()


_StealthResultT = TypeVar("_StealthResultT")


async def _run_with_stealth_session(
    work: Callable[[Any], Awaitable[_StealthResultT]],
    on_error: Callable[[BaseException], _StealthResultT],
    label: str,
) -> _StealthResultT:
    """Open a stealth Playwright session, run ``work(page)``, return its value.

    On ``SERP_BROWSER_EXCEPTIONS``: log + delegate to ``on_error(exc)``.
    Always closes the browser context in ``finally``.
    """
    from playwright.async_api import async_playwright

    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await work(page)
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.error("Error fetching SEMrush %s: %s", label, type(exc).__name__)
        return on_error(exc)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _fetch_domain_overview(domain: str) -> Dict[str, Any]:
    """Scrape SEMrush domain overview inside a stealth session."""
    return await _run_with_stealth_session(
        work=lambda page: _scrape_domain(page, domain),
        on_error=lambda exc: {
            "error": "browser_error", "message": type(exc).__name__,
        },
        label="domain overview",
    )


# ─────────────────────────────────────────────
# Keyword scrape
# ─────────────────────────────────────────────

_KEYWORD_LIMIT = 10
_BETWEEN_KEYWORD_DELAY_MS = (500, 1000)


def _login_required_record(keyword: str) -> Dict[str, Any]:
    return {"keyword": keyword, "error": _LOGIN_REQUIRED_MSG}


def _error_record(keyword: str, exc: BaseException) -> Dict[str, Any]:
    return {"keyword": keyword, "error": type(exc).__name__}


async def _scrape_one_keyword(
    page: Any, keyword: str, database: str,
) -> Dict[str, Any]:
    url = (
        "https://www.semrush.com/analytics/keywordmagic/?q=%s&db=%s"
        % (quote(keyword), database)
    )
    await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)
    await random_delay(1500, 2500)
    if not await is_logged_in(page):
        return _login_required_record(keyword)
    metrics = await parse_keyword_metrics(page)
    trend = await collect_trend_bars(page)
    return {"keyword": keyword, **metrics, "trend": trend}


async def _safe_scrape_one_keyword(
    page: Any, keyword: str, database: str,
) -> tuple[Dict[str, Any], bool]:
    """Scrape one keyword. Returns (record, ok); ok=False on recoverable failure."""
    try:
        return await _scrape_one_keyword(page, keyword, database), True
    except (
        *SERP_BROWSER_EXCEPTIONS, *SELECTOR_EXTRACTION_EXCEPTIONS,
    ) as exc:
        logger.warning(
            "Error processing keyword '%s': %s", keyword, type(exc).__name__,
        )
        # explicit recovery: per-keyword failure is non-fatal; surface as error record
        return _error_record(keyword, exc), False


async def _scrape_each_keyword(
    page: Any, keywords: List[str], database: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for keyword in keywords[:_KEYWORD_LIMIT]:
        record, ok = await _safe_scrape_one_keyword(page, keyword, database)
        out.append(record)
        if ok:
            await random_delay(*_BETWEEN_KEYWORD_DELAY_MS)
    return out


async def _fetch_keyword_data(
    keywords: List[str], database: str,
) -> List[Dict[str, Any]]:
    """Scrape SEMrush per-keyword data inside a stealth session."""
    return await _run_with_stealth_session(
        work=lambda page: _scrape_each_keyword(page, keywords, database),
        on_error=lambda exc: [_error_record(kw, exc) for kw in keywords],
        label="keyword data",
    )


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

_ROUTE_EXCEPTIONS = (
    *SERP_BROWSER_EXCEPTIONS, asyncio.TimeoutError,
)


def _elapsed_ms(start: float) -> int:
    return int((time.time() - start) * 1000)


@router.post("/scrape/semrush/domain", response_model=SEMrushDomainResponse)
async def get_domain_overview(
    request: SEMrushDomainRequest,
) -> SEMrushDomainResponse:
    """Scrape SEMrush domain overview data."""
    start = time.time()
    try:
        data = await _fetch_domain_overview(request.domain)
    except _ROUTE_EXCEPTIONS as exc:
        logger.error(
            "Error in SEMrush domain endpoint: %s", type(exc).__name__,
        )
        return SEMrushDomainResponse(
            success=False,
            error=type(exc).__name__,
            elapsed_ms=_elapsed_ms(start),
        )
    if "error" in data:
        return SEMrushDomainResponse(
            success=False,
            error=data.get("message", data["error"]),
            elapsed_ms=_elapsed_ms(start),
        )
    return SEMrushDomainResponse(
        success=True,
        overview=data,
        elapsed_ms=_elapsed_ms(start),
    )


@router.post("/scrape/semrush/keywords", response_model=SEMrushKeywordsResponse)
async def get_keyword_data(
    request: SEMrushKeywordsRequest,
) -> SEMrushKeywordsResponse:
    """Scrape SEMrush per-keyword data."""
    start = time.time()
    try:
        data = await _fetch_keyword_data(request.keywords, request.database)
    except _ROUTE_EXCEPTIONS as exc:
        logger.error(
            "Error in SEMrush keywords endpoint: %s", type(exc).__name__,
        )
        return SEMrushKeywordsResponse(
            success=False,
            error=type(exc).__name__,
            elapsed_ms=_elapsed_ms(start),
        )
    return SEMrushKeywordsResponse(
        success=True,
        keywords=data,
        elapsed_ms=_elapsed_ms(start),
    )
