"""
angellist.py -- AngelList/Wellfound Scraper
===========================================
Endpoints: POST /scrape/angellist/startup
           POST /scrape/angellist/jobs

Uses Playwright CDP for JS-heavy Wellfound pages.
Stealth delays: 800-2000ms between actions.
"""

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, List, Optional, TypeVar
from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from .config import USER_AGENTS
from ._autotrader_helpers import fresh_browser_session
from ._angellist_parsers import (
    COMPANY_LOOKUP_JS,
    JOBS_EXTRACT_JS,
    STARTUP_EXTRACT_JS,
)
from ._scraper_helpers import playwright_exception_types

logger = logging.getLogger("crawlrx.angellist")
router = APIRouter(prefix="/scrape/angellist", tags=["angellist"])

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
# Pydantic Models
# ─────────────────────────────────────────────

class AngelListStartupRequest(BaseModel):
    """Request model for POST /scrape/angellist/startup"""

    company_name: Optional[str] = Field(default=None, description="Company name to search for")
    url: Optional[str] = Field(default=None, description="Direct Wellfound/AngelList company URL")


class AngelListJobsRequest(BaseModel):
    """Request model for POST /scrape/angellist/jobs"""

    query: str = Field(..., description="Job search query (role, company, etc.)")
    location: Optional[str] = Field(default=None, description="Location filter")
    remote: bool = Field(default=False, description="Remote jobs only")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")


class AngelListInvestor(BaseModel):
    """Investor in a startup"""

    name: str = Field(..., description="Investor name")
    type: Optional[str] = Field(default=None, description="Investor type (VC, Angel, etc.)")


class AngelListStartupData(BaseModel):
    """Startup/company data from AngelList/Wellfound"""

    name: str = Field(default="")
    tagline: str = Field(default="")
    description: str = Field(default="")
    founded: Optional[str] = None
    team_size: Optional[str] = None
    location: Optional[str] = None
    funding_stage: Optional[str] = None
    total_raised: Optional[str] = None
    investors: List[AngelListInvestor] = Field(default_factory=list)
    jobs_count: int = Field(default=0)
    website: Optional[str] = None
    markets: List[str] = Field(default_factory=list)
    logo_url: Optional[str] = None
    wellfound_url: str = Field(default="")


class AngelListJobData(BaseModel):
    """Job listing data from AngelList/Wellfound"""

    title: str = Field(...)
    company: str = Field(...)
    location: Optional[str] = None
    remote: bool = Field(default=False)
    salary_range: Optional[str] = None
    equity_range: Optional[str] = None
    job_url: str = Field(...)
    skills_required: List[str] = Field(default_factory=list)
    posted_date: Optional[str] = None
    job_type: Optional[str] = None


class AngelListStartupResponse(BaseModel):
    """Response model for POST /scrape/angellist/startup"""

    success: bool = Field(...)
    startup: Optional[AngelListStartupData] = None
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = None


class AngelListJobsResponse(BaseModel):
    """Response model for POST /scrape/angellist/jobs"""

    success: bool = Field(...)
    jobs: List[AngelListJobData] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = None


# ─────────────────────────────────────────────
# Utility Functions
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Random delay for stealth (800-2000ms)."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _is_valid_company_url(url: str) -> bool:
    """Check if URL is a valid Wellfound/AngelList company URL."""
    valid_prefixes = (
        "https://wellfound.com/company/",
        "https://angel.co/company/",
        "https://wellfound.com/companies/",
        "https://angel.co/companies/",
    )
    return any(url.startswith(p) for p in valid_prefixes)


async def _wait_networkidle(page: Any, timeout_ms: int) -> None:
    """Best-effort ``networkidle`` wait that swallows the ``PlaywrightTimeoutError``."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def _settle_page(page: Any, networkidle_ms: int) -> None:
    """Common scroll + delay + networkidle settle sequence."""
    await _random_delay()
    await _wait_networkidle(page, networkidle_ms)
    await _random_delay()


# ─────────────────────────────────────────────
# Playwright scraping bodies
# ─────────────────────────────────────────────


async def _do_company_search(page: Any, company_name: str) -> Optional[str]:
    """Open the company-suggestions page and return the first company URL."""
    await page.set_viewport_size({"width": 1280, "height": 800})
    search_url = f"https://wellfound.com/company-suggestions?q={quote(company_name)}"
    logger.info("[AngelList] Searching for company: '%s'", company_name)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await _settle_page(page, networkidle_ms=10000)
    company_url = await page.evaluate(COMPANY_LOOKUP_JS)
    if company_url:
        logger.info("[AngelList] Found company URL: %s", company_url)
    return company_url


async def _search_company(company_name: str) -> Optional[str]:
    """Search for a company and return its Wellfound URL."""
    try:
        async with fresh_browser_session(USER_AGENTS) as session:
            if session.page is None:
                raise RuntimeError(
                    "[AngelList] fresh_browser_session yielded no page "
                    "for company search; browser launch failed silently"
                )
            return await _do_company_search(session.page, company_name)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[AngelList] Company search error (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None


async def _do_startup_extract(page: Any, url: str) -> AngelListStartupData:
    """Open the startup page, scroll, run the JS extractor."""
    await page.set_viewport_size({"width": 1280, "height": 800})
    logger.info("[AngelList] Scraping startup page: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _settle_page(page, networkidle_ms=15000)
    await page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight / 2); }")
    await _random_delay()
    raw_data = await page.evaluate(STARTUP_EXTRACT_JS)
    return AngelListStartupData(**raw_data)


async def _scrape_startup_page(url: str) -> Optional[AngelListStartupData]:
    """Scrape full startup page details using Playwright."""
    try:
        async with fresh_browser_session(USER_AGENTS) as session:
            if session.page is None:
                raise RuntimeError(
                    "[AngelList] fresh_browser_session yielded no page "
                    "for startup extract; browser launch failed silently"
                )
            return await _do_startup_extract(session.page, url)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[AngelList] Error scraping startup page %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
        )
        return None


def _build_jobs_url(query: str, location: Optional[str], remote: bool) -> str:
    """Build the Wellfound jobs search URL."""
    search_params = [f"q={quote(query)}"]
    if location:
        search_params.append(f"location={quote(location)}")
    if remote:
        search_params.append("remote=true")
    return f"https://wellfound.com/jobs?{'&'.join(search_params)}"


async def _scroll_jobs_results(page: Any, limit: int) -> None:
    """Scroll the jobs results page a few times to lazy-load more cards."""
    scroll_passes = min(3, max(0, limit // 10))
    for _ in range(scroll_passes):
        await page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
        await _random_delay(500, 1000)


async def _do_jobs_extract(
    page: Any, query: str, location: Optional[str], remote: bool, limit: int
) -> List[AngelListJobData]:
    """Open the jobs search page, scroll, run the JS extractor."""
    await page.set_viewport_size({"width": 1280, "height": 800})
    search_url = _build_jobs_url(query, location, remote)
    logger.info(
        "[AngelList] Searching jobs: query='%s', location='%s', remote=%s",
        query,
        location,
        remote,
    )
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await _settle_page(page, networkidle_ms=15000)
    await _scroll_jobs_results(page, limit)
    raw_results = await page.evaluate(JOBS_EXTRACT_JS, limit)
    jobs = [AngelListJobData(**job_data) for job_data in raw_results]
    logger.info("[AngelList] Found %d jobs", len(jobs))
    return jobs


async def _search_jobs(
    query: str, location: Optional[str], remote: bool, limit: int
) -> List[AngelListJobData]:
    """Search jobs on Wellfound using Playwright."""
    async with fresh_browser_session(USER_AGENTS) as session:
        if session.page is None:
            raise RuntimeError(
                "[AngelList] fresh_browser_session yielded no page "
                "for jobs search; browser launch failed silently"
            )
        return await _do_jobs_extract(session.page, query, location, remote, limit)


# ─────────────────────────────────────────────
# Async "do" wrappers
# ─────────────────────────────────────────────


async def _resolve_company_url(
    company_name: Optional[str], url: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(company_url, error)`` -- exactly one is non-None on resolution."""
    if not company_name and not url:
        return None, "Either company_name or url is required"
    if url:
        return url, None
    found_url = await _search_company(company_name or "")
    if not found_url:
        return None, f"Could not find company '{company_name}' on Wellfound"
    return found_url, None


def _startup_failure(error_message: str, start_time: float) -> AngelListStartupResponse:
    """Build a failed startup response with elapsed-ms computed from ``start_time``."""
    return AngelListStartupResponse(
        success=False,
        error=error_message,
        elapsed_ms=int((time.time() - start_time) * 1000),
    )


async def _resolved_startup_response(
    company_url: str, start_time: float
) -> AngelListStartupResponse:
    """Fetch + build the success response for an already-validated company URL."""
    try:
        startup = await _scrape_startup_page(company_url)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[AngelList] Startup scrape error (%s): %s", type(exc).__name__, exc
        )
        return _startup_failure(str(exc), start_time)
    elapsed_ms = int((time.time() - start_time) * 1000)
    if not startup:
        return AngelListStartupResponse(
            success=False,
            error="Failed to extract startup data",
            elapsed_ms=elapsed_ms,
        )
    logger.info(
        "[AngelList] Scraped startup: %s, %s employees, %d open jobs",
        startup.name,
        startup.team_size or "unknown",
        startup.jobs_count,
    )
    return AngelListStartupResponse(
        success=True,
        startup=startup,
        elapsed_ms=elapsed_ms,
    )


async def _do_scrape_startup(
    company_name: Optional[str], url: Optional[str]
) -> AngelListStartupResponse:
    """Async startup scrape (resolve URL → fetch page → build response)."""
    start_time = time.time()
    company_url, error = await _resolve_company_url(company_name, url)
    if error is not None:
        return _startup_failure(error, start_time)
    if company_url is None:
        raise RuntimeError(
            "[AngelList] _resolve_company_url returned no error AND no URL; "
            "contract violation -- exactly one must be non-None"
        )
    if not _is_valid_company_url(company_url):
        return _startup_failure(
            "Invalid Wellfound/AngelList company URL. "
            "Must be a wellfound.com or angel.co company page",
            start_time,
        )
    return await _resolved_startup_response(company_url, start_time)


async def _do_search_jobs(
    query: str, location: Optional[str], remote: bool, limit: int
) -> AngelListJobsResponse:
    """Async job search wrapper."""
    start_time = time.time()
    try:
        jobs = await _search_jobs(query, location, remote, limit)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[AngelList] Job search error (%s): %s", type(exc).__name__, exc
        )
        return AngelListJobsResponse(
            success=False,
            error=str(exc),
            elapsed_ms=int((time.time() - start_time) * 1000),
        )
    return AngelListJobsResponse(
        success=True,
        jobs=jobs,
        count=len(jobs),
        elapsed_ms=int((time.time() - start_time) * 1000),
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
        return fail(f"Request failed: {exc}")


@router.post(
    "/startup",
    response_model=AngelListStartupResponse,
    summary="Scrape AngelList/Wellfound startup",
)
async def scrape_angellist_startup(
    request: AngelListStartupRequest,
) -> AngelListStartupResponse:
    """Scrape a startup/company profile from AngelList/Wellfound."""
    return await _run_with_request_timeout(
        "[AngelList] Startup",
        lambda err: AngelListStartupResponse(
            success=False, error=err, elapsed_ms=0
        ),
        lambda: _do_scrape_startup(request.company_name, request.url),
    )


@router.post(
    "/jobs",
    response_model=AngelListJobsResponse,
    summary="Search AngelList/Wellfound jobs",
)
async def search_angellist_jobs(
    request: AngelListJobsRequest,
) -> AngelListJobsResponse:
    """Search for startup jobs on AngelList/Wellfound."""
    return await _run_with_request_timeout(
        "[AngelList] Jobs",
        lambda err: AngelListJobsResponse(
            success=False, error=err, elapsed_ms=0
        ),
        lambda: _do_search_jobs(
            request.query, request.location, request.remote, request.limit
        ),
    )


@router.post("/", summary="AngelList/Wellfound scraper info")
def angellist_base() -> dict:
    """Base endpoint - returns available AngelList scraper routes (no I/O)."""
    return {
        "success": True,
        "message": "AngelList/Wellfound scraper endpoints",
        "endpoints": [
            {
                "path": "/scrape/angellist/startup",
                "method": "POST",
                "params": {"company_name": "str (opt)", "url": "str (opt)"},
            },
            {
                "path": "/scrape/angellist/jobs",
                "method": "POST",
                "params": {
                    "query": "str",
                    "location": "str (opt)",
                    "remote": "bool (opt)",
                    "limit": "int (opt)",
                },
            },
        ],
    }
