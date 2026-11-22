"""LinkedIn Profile Scraper -- CrawlRX.

Endpoints:
  POST /scrape/linkedin           -- text-only public profile
  POST /scrape/linkedin/profile   -- structured person profile
  POST /scrape/linkedin/company   -- company page
  POST /scrape/linkedin/jobs      -- search job listings
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from ._linkedin_company import extract_company
from ._linkedin_extract import extract_flat_profile, is_login_page
from ._linkedin_jobs import JobsSearchParams, search_jobs
from ._linkedin_profile import extract_structured_profile
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .models import (
    LinkedInCompanyPost,
    LinkedInCompanyRequest,
    LinkedInCompanyResponse,
    LinkedInEducation,
    LinkedInExperience,
    LinkedInJob,
    LinkedInJobsRequest,
    LinkedInJobsResponse,
    LinkedInProfileRequest,
    LinkedInProfileResponse,
    LinkedInRequest,
    LinkedInResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import detect_bot_block

logger = logging.getLogger(__name__)
router = APIRouter(tags=["linkedin"])

_DEFAULT_TIMEOUT_MS = 45000


# ---------------------------------------------------------------------------
# Common navigation helpers
# ---------------------------------------------------------------------------

async def _goto_and_settle(page: Any, url: str, timeout_ms: int) -> None:
    """Navigate and wait briefly for the page to render."""
    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)


async def _check_login_or_block(
    page: Any, url: str
) -> Optional[str]:
    """Return an error string if the page is login-walled or bot-blocked."""
    html = await page.content()
    current_url = page.url
    if is_login_page(html, current_url):
        logger.info("LinkedIn login required for: %s", url)
        return "Login required"
    if detect_bot_block(html, await page.title()):
        logger.warning("Environment check triggered for: %s", url)
        return "Environment check triggered  -- try again later"
    return None


def _elapsed_since(start: float) -> int:
    """Milliseconds since ``start``."""
    return int((time.time() - start) * 1000)


# ---------------------------------------------------------------------------
# Flat profile (POST /scrape/linkedin)
# ---------------------------------------------------------------------------

async def _run_in_stealth_page(
    url: str,
    timeout_ms: int,
    on_page: Any,
    on_timeout: Any,
    on_error: Any,
) -> Any:
    """Open a stealth Playwright page, navigate, and call ``on_page``.

    ``on_timeout(start_time)`` and ``on_error(exc, start_time)`` build the
    failure response when navigation / extraction raise.
    """
    start_time = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await _goto_and_settle(page, url, timeout_ms)
            return await on_page(url, page, start_time)
    except PlaywrightTimeout:
        return on_timeout(start_time)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        return on_error(exc, start_time)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _flat_profile_payload(
    url: str, page: Any, start_time: float
) -> LinkedInResponse:
    """Build the flat-profile response from a settled page."""
    error = await _check_login_or_block(page, url)
    if error:
        return LinkedInResponse(
            success=False,
            url=url,
            error=error,
            elapsed_ms=_elapsed_since(start_time),
        )
    data = await extract_flat_profile(page)
    elapsed = _elapsed_since(start_time)
    if not data["name"] and not data["headline"]:
        logger.warning("No profile data extracted from: %s", url)
        return LinkedInResponse(
            success=False,
            url=url,
            error="Could not extract profile data  -- profile may be private or unavailable",
            elapsed_ms=elapsed,
        )
    logger.info(
        "Successfully scraped LinkedIn profile: %s from %s",
        data.get("name", "Unknown"),
        url,
    )
    return LinkedInResponse(
        success=True,
        url=url,
        name=data.get("name"),
        headline=data.get("headline"),
        location=data.get("location"),
        about=data.get("about"),
        experience=data.get("experience", []),
        education=data.get("education", []),
        skills=data.get("skills", []),
        elapsed_ms=elapsed,
    )


def _flat_profile_timeout(url: str, timeout_ms: int):
    """Timeout factory for the flat-profile scraper."""
    def build(start_time: float) -> LinkedInResponse:
        logger.error("Timeout scraping LinkedIn profile: %s", url)
        return LinkedInResponse(
            success=False,
            url=url,
            error=f"Timeout after {timeout_ms}ms",
            elapsed_ms=_elapsed_since(start_time),
        )
    return build


def _flat_profile_error(url: str):
    """Error factory for the flat-profile scraper."""
    def build(exc: BaseException, start_time: float) -> LinkedInResponse:
        logger.error("Error scraping LinkedIn profile %s: %s", url, exc)
        return LinkedInResponse(
            success=False,
            url=url,
            error=str(exc),
            elapsed_ms=_elapsed_since(start_time),
        )
    return build


async def _scrape_linkedin_sync(url: str, timeout_ms: int) -> LinkedInResponse:
    """Run the flat (text-only) profile scrape."""
    return await _run_in_stealth_page(
        url,
        timeout_ms,
        _flat_profile_payload,
        _flat_profile_timeout(url, timeout_ms),
        _flat_profile_error(url),
    )


def _validate_linkedin_url(url: str) -> Optional[str]:
    """Return an error message if ``url`` isn't a linkedin.com URL."""
    try:
        parsed = urlparse(url)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        return f"Invalid URL: {exc}"
    if not parsed.netloc or "linkedin.com" not in parsed.netloc.lower():
        return "Invalid URL  -- must be a linkedin.com URL"
    return None


@router.post(
    "/scrape/linkedin",
    response_model=LinkedInResponse,
    summary="Scrape LinkedIn public profile",
    tags=["scrape"],
)
async def scrape_linkedin(request: LinkedInRequest) -> LinkedInResponse:
    """Scrape a public LinkedIn profile (text-only payload)."""
    error = _validate_linkedin_url(request.url)
    if error:
        return LinkedInResponse(
            success=False,
            url=request.url,
            error=error,
            elapsed_ms=0,
        )
    loop = asyncio.get_running_loop()
    fn = functools.partial(_scrape_linkedin_sync, request.url, request.timeout)
    return await loop.run_in_executor(None, fn)


# ---------------------------------------------------------------------------
# Structured profile (POST /scrape/linkedin/profile)
# ---------------------------------------------------------------------------

async def _structured_profile_payload(
    url: str, page: Any, start_time: float
) -> Dict[str, Any]:
    """Build the structured-profile response payload."""
    error = await _check_login_or_block(page, url)
    if error:
        return {
            "success": False,
            "error": error,
            "profile_url": url,
            "elapsed_ms": _elapsed_since(start_time),
        }
    result = await extract_structured_profile(
        page, LinkedInExperience, LinkedInEducation
    )
    result["profile_url"] = url
    elapsed = _elapsed_since(start_time)
    if not result["name"]:
        return {
            "success": False,
            "error": "Could not extract profile data  -- profile may be private or unavailable",
            "profile_url": url,
            "elapsed_ms": elapsed,
        }
    logger.info("Successfully scraped LinkedIn profile: %s", result["name"])
    return {"success": True, **result, "elapsed_ms": elapsed}


def _structured_profile_timeout(url: str, timeout_ms: int):
    """Timeout factory for the structured-profile scraper."""
    def build(start_time: float) -> Dict[str, Any]:
        return {
            "success": False,
            "error": f"Timeout after {timeout_ms}ms",
            "profile_url": url,
            "elapsed_ms": _elapsed_since(start_time),
        }
    return build


def _structured_profile_error(url: str):
    """Error factory for the structured-profile scraper."""
    def build(exc: BaseException, start_time: float) -> Dict[str, Any]:
        logger.error("Error scraping LinkedIn profile: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "profile_url": url,
            "elapsed_ms": _elapsed_since(start_time),
        }
    return build


async def _scrape_linkedin_profile_sync(
    url: str, timeout_ms: int
) -> Dict[str, Any]:
    """Run the structured-profile scrape."""
    return await _run_in_stealth_page(
        url,
        timeout_ms,
        _structured_profile_payload,
        _structured_profile_timeout(url, timeout_ms),
        _structured_profile_error(url),
    )


def _resolve_profile_url(request: LinkedInProfileRequest) -> Optional[str]:
    """Return a normalised profile URL from ``url`` or ``username``."""
    if request.url:
        return request.url
    if request.username:
        return f"https://www.linkedin.com/in/{request.username}"
    return None


@router.post("/scrape/linkedin/profile", response_model=LinkedInProfileResponse)
async def scrape_linkedin_profile(
    request: LinkedInProfileRequest,
) -> LinkedInProfileResponse:
    """Scrape a LinkedIn person profile with structured data."""
    profile_url = _resolve_profile_url(request)
    if not profile_url:
        return LinkedInProfileResponse(
            success=False,
            error="Either url or username is required",
            elapsed_ms=0,
        )
    error = _validate_linkedin_url(profile_url)
    if error:
        return LinkedInProfileResponse(success=False, error=error, elapsed_ms=0)
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        _scrape_linkedin_profile_sync, profile_url, _DEFAULT_TIMEOUT_MS
    )
    result = await loop.run_in_executor(None, fn)
    return LinkedInProfileResponse(**result)


# ---------------------------------------------------------------------------
# Company (POST /scrape/linkedin/company)
# ---------------------------------------------------------------------------

async def _company_payload(
    url: str, page: Any, start_time: float
) -> Dict[str, Any]:
    """Build the company-page response payload."""
    error = await _check_login_or_block(page, url)
    if error:
        return {
            "success": False,
            "error": error,
            "elapsed_ms": _elapsed_since(start_time),
        }
    result = await extract_company(page, LinkedInCompanyPost)
    elapsed = _elapsed_since(start_time)
    if not result["name"]:
        return {
            "success": False,
            "error": "Could not extract company data  -- page may be private or unavailable",
            "elapsed_ms": elapsed,
        }
    logger.info("Successfully scraped LinkedIn company: %s", result["name"])
    return {"success": True, **result, "elapsed_ms": elapsed}


def _company_timeout(timeout_ms: int):
    """Timeout factory for the company-page scraper."""
    def build(start_time: float) -> Dict[str, Any]:
        return {
            "success": False,
            "error": f"Timeout after {timeout_ms}ms",
            "elapsed_ms": _elapsed_since(start_time),
        }
    return build


def _company_error():
    """Error factory for the company-page scraper."""
    def build(exc: BaseException, start_time: float) -> Dict[str, Any]:
        logger.error("Error scraping LinkedIn company: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "elapsed_ms": _elapsed_since(start_time),
        }
    return build


async def _scrape_linkedin_company_sync(
    url: str, timeout_ms: int
) -> Dict[str, Any]:
    """Run the company-page scrape."""
    return await _run_in_stealth_page(
        url,
        timeout_ms,
        _company_payload,
        _company_timeout(timeout_ms),
        _company_error(),
    )


def _resolve_company_url(request: LinkedInCompanyRequest) -> Optional[str]:
    """Return a normalised company URL from ``url`` or ``company_name``."""
    if request.url:
        return request.url
    if request.company_name:
        slug = request.company_name.lower().replace(" ", "-").replace(".", "")
        return f"https://www.linkedin.com/company/{slug}"
    return None


@router.post("/scrape/linkedin/company", response_model=LinkedInCompanyResponse)
async def scrape_linkedin_company(
    request: LinkedInCompanyRequest,
) -> LinkedInCompanyResponse:
    """Scrape a LinkedIn company page."""
    company_url = _resolve_company_url(request)
    if not company_url:
        return LinkedInCompanyResponse(
            success=False,
            error="Either url or company_name is required",
            elapsed_ms=0,
        )
    error = _validate_linkedin_url(company_url)
    if error:
        return LinkedInCompanyResponse(success=False, error=error, elapsed_ms=0)
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        _scrape_linkedin_company_sync, company_url, _DEFAULT_TIMEOUT_MS
    )
    result = await loop.run_in_executor(None, fn)
    return LinkedInCompanyResponse(**result)


# ---------------------------------------------------------------------------
# Job search (POST /scrape/linkedin/jobs)
# ---------------------------------------------------------------------------

@router.post("/scrape/linkedin/jobs", response_model=LinkedInJobsResponse)
async def search_linkedin_jobs(
    request: LinkedInJobsRequest,
) -> LinkedInJobsResponse:
    """Search LinkedIn job listings."""
    params = JobsSearchParams(
        query=request.query,
        location=request.location,
        remote=request.remote,
        limit=request.limit,
        timeout_ms=_DEFAULT_TIMEOUT_MS,
    )
    loop = asyncio.get_running_loop()
    fn = functools.partial(search_jobs, params, LinkedInJob)
    result = await loop.run_in_executor(None, fn)
    return LinkedInJobsResponse(**result)
