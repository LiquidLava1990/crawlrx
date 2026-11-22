"""indeed.py -- Indeed job listing scraper.

Endpoints:
  POST /scrape/indeed/search -- search Indeed job listings
  POST /scrape/indeed/job -- scrape single job posting

Uses direct HTTP + BeautifulSoup for Indeed (mostly server-side).
Playwright fallback if blocked.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._indeed_parsers import (
    INDEED_BASE_URL,
    extract_job_id_from_url,
    extract_job_type,
    is_remote_job,
    parse_job_html,
    parse_relative_date,
    parse_search_results_html,
)
from ._indeed_playwright import get_job_via_playwright, search_via_playwright
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .utils import timer_ms

logger = logging.getLogger("crawlrx.indeed")
router = APIRouter(prefix="/scrape/indeed", tags=["indeed"])


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class IndeedJob(BaseModel):
    """Single Indeed job listing."""

    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    salary_range: Optional[str] = None
    job_type: Optional[str] = None
    posted_date: Optional[str] = None
    job_url: str
    snippet: Optional[str] = None
    easy_apply: bool = False
    remote: bool = False
    job_id: Optional[str] = None


class IndeedJobDetail(BaseModel):
    """Detailed Indeed job information."""

    title: str
    company: Optional[str] = None
    company_rating: Optional[float] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    job_type: Optional[str] = None
    description: Optional[str] = None
    requirements: List[str] = Field(default_factory=list)
    benefits: List[str] = Field(default_factory=list)
    posted_date: Optional[str] = None
    apply_url: Optional[str] = None
    job_id: Optional[str] = None


class IndeedSearchRequest(BaseModel):
    """Request model for POST /scrape/indeed/search."""

    query: str = Field(..., description="Job search query")
    location: Optional[str] = Field(default=None, description="Job location")
    remote: bool = Field(default=False, description="Remote jobs only")
    job_type: str = Field(
        default="any",
        description="Job type: fulltime, parttime, contract, internship, any",
    )
    min_salary: Optional[int] = Field(default=None, description="Minimum salary")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum jobs to return")
    days_old: int = Field(default=30, ge=1, le=365, description="Max days since posted")


class IndeedJobRequest(BaseModel):
    """Request model for POST /scrape/indeed/job."""

    url: str = Field(..., description="Indeed job URL")


class IndeedSearchResponse(BaseModel):
    """Response model for POST /scrape/indeed/search."""

    success: bool
    jobs: List[IndeedJob] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class IndeedJobResponse(BaseModel):
    """Response model for POST /scrape/indeed/job."""

    success: bool
    job: Optional[IndeedJobDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# --------------------------------------------------------------------------
# Backward-compat re-exports
# --------------------------------------------------------------------------

# These names are referenced by other modules / tests. Keep them callable.
_extract_job_id_from_url = extract_job_id_from_url
_parse_relative_date = parse_relative_date
_is_remote_job = is_remote_job
_extract_job_type = extract_job_type


# --------------------------------------------------------------------------
# HTTP search
# --------------------------------------------------------------------------

_DAYS_OLD_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "1"),
    (7, "3"),
    (14, "7"),
    (30, "14"),
)

_SALARY_BUCKETS: tuple[tuple[int, str], ...] = (
    (200000, " $200,000+"),
    (150000, " $150,000+"),
    (100000, " $100,000+"),
    (75000, " $75,000+"),
    (50000, " $50,000+"),
)

_JOB_TYPE_PARAM_MAP = {
    "fulltime": "fulltime",
    "parttime": "parttime",
    "contract": "contract",
    "internship": "internship",
}


def _fromage_for(days_old: int) -> str:
    """Map ``days_old`` onto Indeed's ``fromage`` bucket value."""
    for ceiling, value in _DAYS_OLD_BUCKETS:
        if days_old < ceiling:
            return value
    return "30"


def _augment_query_with_salary(query: str, min_salary: Optional[int]) -> str:
    """Append the highest matching salary token onto a query string."""
    if not min_salary:
        return query
    for floor, suffix in _SALARY_BUCKETS:
        if min_salary >= floor:
            return query + suffix
    return query


def _build_search_params(request: IndeedSearchRequest) -> dict:
    """Build the dict of HTTP query params for an Indeed search."""
    params: dict = {"q": _augment_query_with_salary(request.query, request.min_salary)}
    if request.location:
        params["l"] = request.location
    if request.remote:
        params["remotejob"] = "032b3046-06a5-11ea-8e86-13a99bc6aaac"
    if request.job_type and request.job_type != "any":
        mapped = _JOB_TYPE_PARAM_MAP.get(request.job_type)
        if mapped:
            params["jt"] = mapped
    params["fromage"] = _fromage_for(request.days_old)
    return params


async def _fetch_indeed_html(url: str, params: Optional[dict] = None) -> str:
    """GET ``url`` with the Indeed default headers."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(
            url, params=params or {}, headers=_DEFAULT_HEADERS, timeout=30.0
        )
        resp.raise_for_status()
        return resp.text


def _empty_search_response(error_message: str, elapsed: int) -> IndeedSearchResponse:
    """Construct a failed empty IndeedSearchResponse."""
    return IndeedSearchResponse(
        success=False, jobs=[], count=0, error=error_message, elapsed_ms=elapsed,
    )


async def _search_indeed_http(
    request: IndeedSearchRequest,
) -> IndeedSearchResponse:
    """Search Indeed using direct HTTP requests."""
    start = timer_ms()
    try:
        html = await _fetch_indeed_html(
            f"{INDEED_BASE_URL}/jobs", _build_search_params(request)
        )
    except httpx.HTTPError as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Indeed HTTP search error for '%s': %s", request.query, exc)
        return _empty_search_response(f"HTTP error: {exc}", elapsed)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error(
            "Indeed HTTP search unexpected error for '%s': %s", request.query, exc
        )
        return _empty_search_response(str(exc), elapsed)

    jobs = parse_search_results_html(html, request.limit, IndeedJob)
    elapsed = int(timer_ms() - start)
    logger.info(
        "Indeed HTTP search returned %d jobs for '%s' in %dms",
        len(jobs),
        request.query,
        elapsed,
    )
    return IndeedSearchResponse(
        success=True,
        jobs=jobs,
        count=len(jobs),
        elapsed_ms=elapsed,
    )


async def _get_job_http(url: str) -> Optional[IndeedJobDetail]:
    """Get job details using direct HTTP request."""
    try:
        html = await _fetch_indeed_html(url)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Indeed HTTP job fetch failed for %s: %s", url, exc)
        return None
    job_id = extract_job_id_from_url(url)
    return parse_job_html(html, url, job_id, IndeedJobDetail)


# --------------------------------------------------------------------------
# Playwright fallback (delegates)
# --------------------------------------------------------------------------

async def _search_indeed_playwright(
    request: IndeedSearchRequest,
) -> IndeedSearchResponse:
    """Search Indeed via Playwright CDP (delegates)."""
    return await search_via_playwright(request, IndeedSearchResponse, IndeedJob)


async def _get_job_playwright(url: str) -> Optional[IndeedJobDetail]:
    """Get a job's detail via Playwright CDP (delegates)."""
    return await get_job_via_playwright(url, IndeedJobDetail)


# --------------------------------------------------------------------------
# API endpoints
# --------------------------------------------------------------------------

@router.post("/search", response_model=IndeedSearchResponse)
async def indeed_search(request: IndeedSearchRequest) -> IndeedSearchResponse:
    """Search Indeed job listings. HTTP first, Playwright fallback."""
    result = await _search_indeed_http(request)
    if not result.success or result.count == 0:
        logger.info("Indeed HTTP search failed/empty, trying Playwright fallback")
        result = await _search_indeed_playwright(request)
    return result


@router.post("/job", response_model=IndeedJobResponse)
async def indeed_job(request: IndeedJobRequest) -> IndeedJobResponse:
    """Scrape a single Indeed job posting."""
    start = timer_ms()
    job = await _get_job_http(request.url)
    if not job:
        logger.info("Indeed HTTP job fetch failed, trying Playwright fallback")
        job = await _get_job_playwright(request.url)
    elapsed = int(timer_ms() - start)
    if job:
        return IndeedJobResponse(success=True, job=job, elapsed_ms=elapsed)
    return IndeedJobResponse(
        success=False,
        error="Failed to fetch job details",
        elapsed_ms=elapsed,
    )
