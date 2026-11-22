"""glassdoor.py -- Glassdoor company/reviews/salaries scraper

Endpoints
---------
POST /scrape/glassdoor/company  -- scrape company overview
POST /scrape/glassdoor/reviews  -- get company reviews
POST /scrape/glassdoor/salaries -- get salary data

Implementation lives in sibling modules:
- :mod:`._glassdoor_extractors` -- per-field DOM probes
- :mod:`._glassdoor_pages`      -- page orchestrators (search / overview /
                                   reviews / salaries)

This module owns only the public surface required by the snapshot tests
(pydantic models, ``router``, and the three endpoint coroutines).
Stealth delays: 800-2000ms. Uses Playwright CDP 9223 (logged-in session).
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scraper_helpers import safe_extract_async
from . import _glassdoor_pages as _pages
from .utils import timer_ms

logger = logging.getLogger("crawlrx.glassdoor")
router = APIRouter(prefix="/scrape/glassdoor", tags=["glassdoor"])

GLASSDOOR_BASE_URL = _pages.GLASSDOOR_BASE_URL


# ─────────────────────────────────────────────
# Pydantic Models (must remain in this module: signature snapshot
# filters by ``__module__`` and would drop re-exports.)
# ─────────────────────────────────────────────


class GlassdoorRatingsBreakdown(BaseModel):
    """Rating breakdown by category."""

    culture: Optional[float] = None
    work_life: Optional[float] = None
    management: Optional[float] = None
    compensation: Optional[float] = None
    career: Optional[float] = None


class GlassdoorCompany(BaseModel):
    """Glassdoor company overview data."""

    name: str
    overall_rating: Optional[float] = None
    ratings_breakdown: GlassdoorRatingsBreakdown = Field(
        default_factory=GlassdoorRatingsBreakdown
    )
    ceo_approval: Optional[str] = None
    recommend_to_friend_pct: Optional[str] = None
    review_count: Optional[int] = None
    salary_count: Optional[int] = None
    job_count: Optional[int] = None
    size: Optional[str] = None
    industry: Optional[str] = None
    founded: Optional[str] = None
    headquarters: Optional[str] = None
    description: Optional[str] = None
    pros_summary: Optional[str] = None
    cons_summary: Optional[str] = None
    website: Optional[str] = None


class GlassdoorReview(BaseModel):
    """Single Glassdoor review."""

    title: Optional[str] = None
    rating: Optional[int] = None
    pros: Optional[str] = None
    cons: Optional[str] = None
    advice_to_mgmt: Optional[str] = None
    job_title: Optional[str] = None
    employment_status: Optional[str] = None
    review_date: Optional[str] = None
    helpful_count: Optional[int] = None
    recommend: Optional[bool] = None
    outlook: Optional[str] = None
    ceo_approval: Optional[bool] = None


class GlassdoorSalary(BaseModel):
    """Single Glassdoor salary entry."""

    job_title: str
    base_salary_median: Optional[str] = None
    base_salary_range: Optional[str] = None
    total_comp_median: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    data_points_count: Optional[int] = None


class GlassdoorCompanyRequest(BaseModel):
    """Request model for POST /scrape/glassdoor/company."""

    company_name: Optional[str] = Field(
        default=None, description="Company name to search"
    )
    url: Optional[str] = Field(
        default=None, description="Direct Glassdoor company URL"
    )


class GlassdoorCompanyResponse(BaseModel):
    """Response model for POST /scrape/glassdoor/company."""

    success: bool
    company: Optional[GlassdoorCompany] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


class GlassdoorReviewsRequest(BaseModel):
    """Request model for POST /scrape/glassdoor/reviews."""

    company_name: Optional[str] = Field(
        default=None, description="Company name to search"
    )
    url: Optional[str] = Field(
        default=None, description="Direct Glassdoor reviews URL"
    )
    limit: int = Field(
        default=10, ge=1, le=50, description="Maximum reviews to return"
    )


class GlassdoorReviewsResponse(BaseModel):
    """Response model for POST /scrape/glassdoor/reviews."""

    success: bool
    reviews: List[GlassdoorReview] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class GlassdoorSalariesRequest(BaseModel):
    """Request model for POST /scrape/glassdoor/salaries."""

    company_name: Optional[str] = Field(
        default=None, description="Company name to filter by"
    )
    job_title: Optional[str] = Field(
        default=None, description="Job title to search"
    )
    location: Optional[str] = Field(default=None, description="Location filter")
    limit: int = Field(
        default=10, ge=1, le=50, description="Maximum salaries to return"
    )


class GlassdoorSalariesResponse(BaseModel):
    """Response model for POST /scrape/glassdoor/salaries."""

    success: bool
    salaries: List[GlassdoorSalary] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Endpoint helpers (shared "resolve URL or fail" path).
# ─────────────────────────────────────────────


async def _resolve_company_url(
    request_url: Optional[str],
    company_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(url, error)``: url if resolved, else error message."""
    if request_url:
        return (request_url, None)
    if not company_name:
        return (None, None)
    resolved = await safe_extract_async(
        lambda: _pages.search_company(company_name),
        field="search_company",
    )
    if resolved:
        return (resolved, None)
    return (None, f"Could not find company: {company_name}")


def _now_elapsed(start: float) -> int:
    return int(timer_ms() - start)


async def _resolve_or_fail(
    request_url: Optional[str],
    company_name: Optional[str],
    response_cls: type,
    start: float,
) -> tuple[Optional[str], Optional[Any]]:
    """Resolve URL; if not possible, return ``(None, response_cls(...))``."""
    url, error = await _resolve_company_url(request_url, company_name)
    if error is not None:
        return (None, response_cls(
            success=False, error=error, elapsed_ms=_now_elapsed(start),
        ))
    if url is None:
        return (None, response_cls(
            success=False,
            error="Provide either company_name or url",
            elapsed_ms=_now_elapsed(start),
        ))
    return (url, None)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────


@router.post("/company", response_model=GlassdoorCompanyResponse)
async def glassdoor_company(
    request: GlassdoorCompanyRequest,
) -> GlassdoorCompanyResponse:
    """Scrape a company's Glassdoor page for overview data.

    Provide either ``company_name`` or ``url``.
    """
    start = timer_ms()
    url, fail = await _resolve_or_fail(
        request.url, request.company_name, GlassdoorCompanyResponse, start,
    )
    if fail is not None:
        return fail
    company = await safe_extract_async(
        lambda: _pages.fetch_company_overview(url),
        field="company_overview",
    )
    elapsed = _now_elapsed(start)
    if company is not None:
        return GlassdoorCompanyResponse(
            success=True, company=company, elapsed_ms=elapsed,
        )
    return GlassdoorCompanyResponse(
        success=False,
        error="Failed to fetch company overview",
        elapsed_ms=elapsed,
    )


@router.post("/reviews", response_model=GlassdoorReviewsResponse)
async def glassdoor_reviews(
    request: GlassdoorReviewsRequest,
) -> GlassdoorReviewsResponse:
    """Get company reviews from Glassdoor.

    Provide either ``company_name`` or ``url``.
    """
    start = timer_ms()
    url, fail = await _resolve_or_fail(
        request.url, request.company_name, GlassdoorReviewsResponse, start,
    )
    if fail is not None:
        return fail
    reviews = await safe_extract_async(
        lambda: _pages.fetch_company_reviews(url, request.limit),
        field="company_reviews",
        default=[],
    )
    return GlassdoorReviewsResponse(
        success=True,
        reviews=reviews or [],
        count=len(reviews or []),
        elapsed_ms=_now_elapsed(start),
    )


def _build_salary_search_url(
    job_title: Optional[str], location: Optional[str],
) -> Optional[str]:
    """Compose the salaries search URL when no company_name is supplied."""
    parts = [p for p in (job_title, location) if p]
    if not parts:
        return None
    import urllib.parse
    query = urllib.parse.quote_plus(" ".join(parts))
    return f"{GLASSDOOR_BASE_URL}/Salaries/index.htm?jobType={query}"


@router.post("/salaries", response_model=GlassdoorSalariesResponse)
async def glassdoor_salaries(
    request: GlassdoorSalariesRequest,
) -> GlassdoorSalariesResponse:
    """Get salary data from Glassdoor for a company or role."""
    start = timer_ms()
    url: Optional[str] = None
    if request.company_name:
        url, error = await _resolve_company_url(None, request.company_name)
        if error is not None:
            return GlassdoorSalariesResponse(
                success=False, error=error, elapsed_ms=_now_elapsed(start),
            )
    if url is None:
        url = _build_salary_search_url(request.job_title, request.location)
    if url is None:
        return GlassdoorSalariesResponse(
            success=False,
            error="Provide company_name, job_title, or location",
            elapsed_ms=_now_elapsed(start),
        )
    salaries = await safe_extract_async(
        lambda: _pages.fetch_company_salaries(url, request.limit),
        field="company_salaries",
        default=[],
    )
    return GlassdoorSalariesResponse(
        success=True,
        salaries=salaries or [],
        count=len(salaries or []),
        elapsed_ms=_now_elapsed(start),
    )


# ─────────────────────────────────────────────
# Backwards-compat shim: tests / external callers may import these
# private names directly. Forward to the sibling implementation.
# ─────────────────────────────────────────────


__all__ = [
    "GLASSDOOR_BASE_URL",
    "GlassdoorCompany",
    "GlassdoorCompanyRequest",
    "GlassdoorCompanyResponse",
    "GlassdoorRatingsBreakdown",
    "GlassdoorReview",
    "GlassdoorReviewsRequest",
    "GlassdoorReviewsResponse",
    "GlassdoorSalariesRequest",
    "GlassdoorSalariesResponse",
    "GlassdoorSalary",
    "glassdoor_company",
    "glassdoor_reviews",
    "glassdoor_salaries",
    "router",
]
