"""LinkedIn job-search extraction (split from linkedin.py)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import quote

from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.linkedin.jobs")


@dataclass(frozen=True)
class JobsSearchParams:
    """Bundled args for the LinkedIn jobs search runner."""

    query: str
    location: Optional[str] = None
    remote: bool = False
    limit: int = 25
    timeout_ms: int = 45000


def _elapsed_since(start: float) -> int:
    """Milliseconds since ``start``."""
    return int((time.time() - start) * 1000)


def _build_jobs_search_url(
    query: str, location: Optional[str], remote: bool
) -> str:
    """Build a LinkedIn jobs search URL."""
    url = f"https://www.linkedin.com/jobs/search/?keywords={quote(query)}"
    if location:
        url += f"&location={quote(location)}"
    if remote:
        url += "&f_WT=2"
    return url


async def _resolve_job_url(card: Any) -> Optional[str]:
    """Return a fully-qualified LinkedIn job URL or ``None``."""
    link_el = await card.query_selector("a")
    if link_el is None:
        return None
    href = await link_el.get_attribute("href")
    if not href:
        return None
    if href.startswith("/"):
        return f"https://www.linkedin.com{href}"
    return href


async def _job_posted_date(card: Any) -> Optional[str]:
    """Return the job's posted date (datetime attr first, else inner text)."""
    date_el = await card.query_selector("time, .job-search-card__listdate")
    if date_el is None:
        return None
    posted = await date_el.get_attribute("datetime")
    if posted:
        return posted
    text = (await date_el.inner_text()).strip()
    return text or None


async def _build_job_entry(card: Any, job_factory: Any) -> Optional[Any]:
    """Convert one LinkedIn job card into a ``LinkedInJob`` row."""
    title_el = await card.query_selector(
        "h3, .job-search-card__title, .job-card-list__title"
    )
    company_el = await card.query_selector(
        "h4, .job-search-card__company-name, .job-card-container__company-name"
    )
    title = (await title_el.inner_text()).strip() if title_el else None
    company = (await company_el.inner_text()).strip() if company_el else None
    if not (title and company):
        return None

    location_el = await card.query_selector(
        ".job-search-card__location, .job-card-container__metadata-item"
    )
    salary_el = await card.query_selector(
        ".job-search-card__salary-info, .compensation__salary"
    )
    easy_apply_el = await card.query_selector(
        ".job-search-card__easy-apply, .jobs-apply-button--top-card"
    )

    job_location = (
        (await location_el.inner_text()).strip() if location_el else ""
    )
    salary = (await salary_el.inner_text()).strip() if salary_el else None
    posted_date = await _job_posted_date(card)
    job_url = await _resolve_job_url(card) or ""

    return job_factory(
        title=title,
        company=company,
        location=job_location,
        posted_date=posted_date,
        job_url=job_url,
        salary_range=salary,
        easy_apply=easy_apply_el is not None,
    )


async def collect_job_cards(
    page: "Page",
    limit: int,
    job_factory: "Callable[..., Any]",
) -> List[Any]:
    """Walk LinkedIn job-search Playwright cards into ``LinkedInJob`` rows."""
    cards = (await page.query_selector_all(
        ".job-search-card, .jobs-search-results__list-item, [data-job-id]"
    ))[:limit]
    out: List[Any] = []
    for card in cards:
        entry = await safe_extract_async(
            lambda card=card: _build_job_entry(card, job_factory),
            field="linkedin_job_card",
            log=logger,
        )
        if entry:
            out.append(entry)
    return out


async def scroll_for_more(page: "Page", repeats: int) -> None:
    """Scroll the search-results page to surface lazily-loaded jobs."""
    for _ in range(max(repeats, 0)):
        try:
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(800)
        except BROWSER_SCRAPE_EXCEPTIONS:
            break


def _empty_jobs_response(error_message: str, elapsed: int) -> Dict[str, Any]:
    """Construct a failed jobs-response dict."""
    return {
        "success": False,
        "jobs": [],
        "count": 0,
        "error": error_message,
        "elapsed_ms": elapsed,
    }


async def search_jobs(
    params: JobsSearchParams, job_factory: "Callable[..., Any]"
) -> Dict[str, Any]:
    """Run the LinkedIn jobs search under a Playwright stealth context."""
    start_time = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = _build_jobs_search_url(
                params.query, params.location, params.remote
            )
            logger.info("Searching LinkedIn jobs: %s", search_url)
            await page.goto(
                search_url,
                timeout=params.timeout_ms,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3000)
            await scroll_for_more(page, min(params.limit // 5, 5))
            jobs = await collect_job_cards(page, params.limit, job_factory)
            elapsed = _elapsed_since(start_time)
            logger.info("Found %d LinkedIn jobs in %dms", len(jobs), elapsed)
            return {
                "success": True,
                "jobs": jobs,
                "count": len(jobs),
                "elapsed_ms": elapsed,
            }
    except PlaywrightTimeout:
        return _empty_jobs_response(
            f"Timeout after {params.timeout_ms}ms", _elapsed_since(start_time)
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Error searching LinkedIn jobs: %s", exc)
        return _empty_jobs_response(str(exc), _elapsed_since(start_time))
    finally:
        await close_stealth_context(browser, ctx, page)
