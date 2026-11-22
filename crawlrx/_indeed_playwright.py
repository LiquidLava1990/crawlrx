"""Indeed Playwright-driven scrape helpers (split from indeed.py)."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any, Callable, List, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._indeed_parsers import (
    INDEED_BASE_URL,
    INDEED_PARSE_EXCEPTIONS,
    extract_job_id_from_url,
    is_remote_job,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.indeed.playwright")


def _build_search_url(request: Any) -> str:
    """Build a Playwright-friendly Indeed search URL."""
    q = urllib.parse.quote_plus(request.query)
    url = f"{INDEED_BASE_URL}/jobs?q={q}"
    if request.location:
        url += f"&l={urllib.parse.quote_plus(request.location)}"
    if request.remote:
        url += "&remotejob=032b3046-06a5-11ea-8e86-13a99bc6aaac"
    return url


async def _wait_for_results(page: Any) -> None:
    """Soft-wait for the search-result container to render."""
    try:
        await page.wait_for_selector(
            "[data-testid='jobTitle'], .job_seen_beacon", timeout=10000
        )
    except PlaywrightTimeoutError:
        pass


async def _text_or_none(card: Any, selector: str) -> Optional[str]:
    """Return the first matching descendant's stripped inner text."""
    elem = await card.query_selector(selector)
    if elem is None:
        return None
    text = (await elem.inner_text()).strip()
    return text or None


async def _build_pw_search_job(card: Any, job_factory: Any) -> Optional[Any]:
    """Convert a Playwright ``ElementHandle`` card into an IndeedJob."""
    job_id = await card.get_attribute("data-jk")
    if not job_id:
        return None
    title = await _text_or_none(card, "h2 a, [data-testid='job-title']")
    if not title:
        return None
    company = await _text_or_none(card, "[data-testid='company-name']")
    location = await _text_or_none(card, "[data-testid='job-location']")
    salary = await _text_or_none(card, "[data-testid='job-salary']")
    snippet = await _text_or_none(card, "[class*='snippet']")
    easy_apply_el = await card.query_selector("[class*='easyApply']")
    return job_factory(
        title=title,
        company=company,
        location=location,
        salary_range=salary,
        job_url=f"{INDEED_BASE_URL}/viewjob?jk={job_id}",
        snippet=snippet,
        easy_apply=easy_apply_el is not None,
        remote=is_remote_job(title, location, snippet),
        job_id=job_id,
    )


async def _collect_search_jobs(
    page: Any, limit: int, job_factory: Any
) -> List[Any]:
    """Walk Indeed search-result Playwright cards into IndeedJob rows."""
    cards = await page.query_selector_all("[data-jk], .job_seen_beacon")
    jobs: List[Any] = []
    seen: set = set()
    for card in cards[: limit * 2]:
        job = await safe_extract_async(
            lambda card=card: _build_pw_search_job(card, job_factory),
            field="indeed_pw_card",
            log=logger,
        )
        if not job or not getattr(job, "job_id", None):
            continue
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


async def search_via_playwright(
    request: object,
    response_factory: "Callable[..., Any]",
    job_factory: "Callable[..., Any]",
) -> object:
    """Search Indeed using Playwright CDP."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = _build_search_url(request)
            logger.info("[Indeed] Navigating to search: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_results(page)
            jobs = await _collect_search_jobs(page, request.limit, job_factory)
            elapsed = int(timer_ms() - start)
            logger.info(
                "Indeed Playwright search returned %d jobs in %dms",
                len(jobs),
                elapsed,
            )
            return response_factory(
                success=True,
                jobs=jobs,
                count=len(jobs),
                elapsed_ms=elapsed,
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Indeed Playwright search error: %s", exc)
        return response_factory(
            success=False,
            jobs=[],
            count=0,
            error=str(exc),
            elapsed_ms=elapsed,
        )
    finally:
        await close_stealth_context(browser, ctx, page)


async def _pw_text(page: Any, selector: str) -> Optional[str]:
    """Return ``inner_text`` of the first matching element on ``page``."""
    elem = await page.query_selector(selector)
    if elem is None:
        return None
    text = (await elem.inner_text()).strip()
    return text or None


async def _populate_pw_field(
    page: Any, job: Any, field: str, selector: str
) -> None:
    """Set ``job.<field>`` to the text of ``selector`` on ``page``."""
    text = await _pw_text(page, selector)
    if text:
        setattr(job, field, text)


_PW_DETAIL_PROBES: tuple[tuple[str, str], ...] = (
    ("title", "h1"),
    ("company", "[data-testid='company-name']"),
    ("location", "[data-testid='job-location']"),
    ("salary", "[data-testid='job-salary']"),
    ("description", "[id='jobDescriptionText']"),
)


async def _populate_job_detail(page: Any, job: Any) -> None:
    """Run each Playwright field-probe under narrow error handling."""
    for field, selector in _PW_DETAIL_PROBES:
        try:
            await _populate_pw_field(page, job, field, selector)
        except (*BROWSER_SCRAPE_EXCEPTIONS, *INDEED_PARSE_EXCEPTIONS) as exc:
            logger.debug("Indeed pw %s probe error: %s", field, exc)


async def get_job_via_playwright(
    url: str, detail_factory: "Callable[..., Any]"
) -> Optional[Any]:
    """Get Indeed job details using Playwright CDP."""
    browser = ctx = page = None
    try:
        job_id = extract_job_id_from_url(url)
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Indeed] Navigating to job: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)
            job = detail_factory(title="", job_id=job_id or "")
            await _populate_job_detail(page, job)
            return job if job.title else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Indeed Playwright job fetch error: %s", exc)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)
