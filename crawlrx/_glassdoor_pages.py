"""Page-level orchestrators for Glassdoor scraping.

Each ``fetch_*`` here owns one Playwright session: open browser, navigate,
build the dataclass / pydantic result, and close the session in ``finally``.
Extraction logic per field lives in :mod:`._glassdoor_extractors`; this
module stays small enough to clear code-quality cognitive / nesting / cyclomatic
ceilings.

Re-imported by :mod:`crawlrx.glassdoor`. The pydantic model classes are
imported lazily inside each function to avoid a circular import.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import urllib.parse
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._scraper_helpers import safe_extract_async
from .stealth_context import close_stealth_context, make_stealth_context
from . import _glassdoor_extractors as _ex
from . import _glassdoor_salary_extractors as _sx

logger = logging.getLogger("crawlrx.glassdoor.pages")

GLASSDOOR_BASE_URL = "https://www.glassdoor.com"


# ---------------------------------------------------------------------------
# Shared utilities.
# ---------------------------------------------------------------------------

async def random_delay(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Sleep for a random duration between min and max milliseconds."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def extract_company_id_from_url(url: str) -> Optional[str]:
    """Extract company ID from a Glassdoor URL."""
    patterns = (
        r"/(?:Employer|Overview|Working-at)/[^/]+-([A-Z]+\d+)",
        r"Employer\.htm\?employerId=(\d+)",
        r"/([A-Z]+\d+)(?:\.htm|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def _wait_for_selector(page: Any, selector: str, timeout_ms: int) -> None:
    """``page.wait_for_selector`` that swallows timeout."""
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def _open_page(runtime: Any) -> tuple[Any, Any, Any]:
    """Open a stealth context and return ``(browser, ctx, page)``."""
    return await make_stealth_context(runtime)


async def _gather_items(
    page: Any,
    row_selector: str,
    builder: Callable[[Any], Awaitable[Optional[Any]]],
    limit: int,
    field: str,
) -> list[Any]:
    """Walk up to ``limit`` rows on ``page`` and run ``builder`` on each."""
    rows = await safe_extract_async(
        lambda: page.query_selector_all(row_selector),
        field=field,
        default=[],
    )
    out: list[Any] = []
    for row in (rows or [])[:limit]:
        item = await safe_extract_async(
            lambda r=row: builder(r),
            field=f"{field}_item",
        )
        if item is not None:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Company search.
# ---------------------------------------------------------------------------

SEARCH_RESULT_SELECTOR = (
    "[data-test='company-tile'], .company-tile, .employer-card"
)
COMPANY_LINK_SELECTOR = (
    "[data-test='company-tile'] a, .company-tile a, .employer-card a"
)


async def _resolve_company_link_href(link: Any) -> Optional[str]:
    """Pull href off the search-result link, normalising to absolute URL."""
    href = await safe_extract_async(
        lambda: link.get_attribute("href"),
        field="search_link_href",
    )
    if not href:
        return None
    if not href.startswith("http"):
        return f"{GLASSDOOR_BASE_URL}{href}"
    return href


async def search_company(company_name: str) -> Optional[str]:
    """Search Glassdoor for a company; return the first-result company URL."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as runtime:
            browser, ctx, page = await _open_page(runtime)
            search_url = (
                f"{GLASSDOOR_BASE_URL}/Search/results.htm"
                f"?keyword={urllib.parse.quote_plus(company_name)}"
            )
            logger.info("[Glassdoor] Searching for company: %s", company_name)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await random_delay(1500, 2500)
            await _wait_for_selector(page, SEARCH_RESULT_SELECTOR, 10000)
            link = await safe_extract_async(
                lambda: page.query_selector(COMPANY_LINK_SELECTOR),
                field="company_link",
            )
            if not link:
                return None
            return await _resolve_company_link_href(link)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Company overview.
# ---------------------------------------------------------------------------

COMPANY_NAME_WAIT_SELECTOR = (
    "[data-test='employer-name'], h1.employerName, "
    "h1[data-test='employer-overview-heading']"
)


async def _populate_company_basic(page: Any, company: Any) -> None:
    """Fill name + headline rating fields on a `GlassdoorCompany` model."""
    name = await _ex.extract_company_name(page)
    if name:
        company.name = name
    company.overall_rating = await _ex.extract_overall_rating(page)
    company.ceo_approval = await _ex.extract_ceo_approval(page)
    company.recommend_to_friend_pct = await _ex.extract_recommend_pct(page)
    company.review_count = await _ex.extract_review_count(page)
    company.salary_count = await _ex.extract_salary_count(page)
    company.job_count = await _ex.extract_job_count(page)
    company.description = await _ex.extract_description(page)
    company.website = await _ex.extract_website(page)


async def _populate_company_breakdown(page: Any, company: Any) -> None:
    """Fill `ratings_breakdown` from the per-category selector map."""
    breakdown = await _ex.extract_ratings_breakdown(page)
    for attr, value in breakdown.items():
        setattr(company.ratings_breakdown, attr, value)


async def _populate_company_details(page: Any, company: Any) -> None:
    """Apply size / industry / founded / HQ rows."""
    rows = await _ex.extract_detail_rows(page)
    for attr, value in rows.items():
        setattr(company, attr, value)


async def _populate_company_pros_cons(page: Any, company: Any) -> None:
    pros, cons = await _ex.extract_pros_cons(page)
    company.pros_summary = pros
    company.cons_summary = cons


async def _build_company(page: Any) -> Optional[Any]:
    """Pull every overview field from `page` and return the company model."""
    from .glassdoor import GlassdoorCompany  # local import: avoid cycle

    company = GlassdoorCompany(name="")
    await _populate_company_basic(page, company)
    await _populate_company_breakdown(page, company)
    await _populate_company_details(page, company)
    await _populate_company_pros_cons(page, company)
    return company if company.name else None


async def fetch_company_overview(url: str) -> Optional[Any]:
    """Get company overview from Glassdoor; returns a `GlassdoorCompany` or None."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as runtime:
            browser, ctx, page = await _open_page(runtime)
            logger.info("[Glassdoor] Navigating to company: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await random_delay(1500, 2500)
            await _wait_for_selector(page, COMPANY_NAME_WAIT_SELECTOR, 15000)
            return await _build_company(page)
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Reviews.
# ---------------------------------------------------------------------------

REVIEWS_WAIT_SELECTOR = (
    "[data-test='review-overview'], .review-container, .gdReview"
)


def _build_reviews_url(url: str) -> str:
    """Map an Overview / company URL to its Reviews URL."""
    if "/Reviews/" in url:
        return url
    company_id = extract_company_id_from_url(url)
    if company_id:
        return f"{GLASSDOOR_BASE_URL}/Reviews/company-reviews-{company_id}.htm"
    return url.replace("/Overview/", "/Reviews/").replace(
        "/Working-at-", "/Reviews/"
    )


async def _build_review(card: Any) -> Any:
    """Build a `GlassdoorReview` from one card element."""
    from .glassdoor import GlassdoorReview  # local import: avoid cycle

    review = GlassdoorReview()
    review.title = await _ex.extract_review_title(card)
    review.rating = await _ex.extract_review_rating(card)
    review.pros = await _ex.extract_review_pros(card)
    review.cons = await _ex.extract_review_cons(card)
    review.advice_to_mgmt = await _ex.extract_review_advice(card)
    employment_status, job_title = await _ex.extract_review_author(card)
    review.employment_status = employment_status
    review.job_title = job_title
    review.review_date = await _ex.extract_review_date(card)
    review.helpful_count = await _ex.extract_review_helpful(card)
    review.recommend = await _ex.extract_review_recommend(card)
    review.outlook = await _ex.extract_review_outlook(card)
    return review


async def fetch_company_reviews(url: str, limit: int) -> List[Any]:
    """Get up to ``limit`` reviews from Glassdoor."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as runtime:
            browser, ctx, page = await _open_page(runtime)
            reviews_url = _build_reviews_url(url)
            logger.info("[Glassdoor] Navigating to reviews: %s", reviews_url)
            await page.goto(reviews_url, wait_until="domcontentloaded", timeout=30000)
            await random_delay(1500, 2500)
            await _wait_for_selector(page, REVIEWS_WAIT_SELECTOR, 15000)
            return await _gather_items(
                page, REVIEWS_WAIT_SELECTOR, _build_review, limit, "review",
            )
    finally:
        await close_stealth_context(browser, ctx, page)


# ---------------------------------------------------------------------------
# Salaries.
# ---------------------------------------------------------------------------

SALARIES_WAIT_SELECTOR = (
    "[data-test='salary-list-item'], .salary-row, .css-1u7ch3d"
)
SALARY_ROW_SELECTOR = (
    "[data-test='salary-list-item'], .salary-row, [data-test='salary-card']"
)


def _build_salaries_url(url: str) -> str:
    """Map an Overview / company URL to its Salaries URL."""
    if "/Salary/" in url:
        return url
    company_id = extract_company_id_from_url(url)
    if company_id:
        return f"{GLASSDOOR_BASE_URL}/Salary/company-salary-{company_id}.htm"
    return url.replace("/Overview/", "/Salary/").replace(
        "/Working-at-", "/Salary/"
    )


async def _build_salary(row: Any) -> Optional[Any]:
    """Build a `GlassdoorSalary` from one row element. Returns None if no title."""
    from .glassdoor import GlassdoorSalary  # local import: avoid cycle

    title = await _sx.extract_salary_title(row)
    if not title:
        return None
    salary = GlassdoorSalary(job_title=title)
    salary.base_salary_median = await _sx.extract_salary_median(row)
    salary.base_salary_range = await _sx.extract_salary_range(row)
    salary.total_comp_median = await _sx.extract_salary_total_comp(row)
    salary.company = await _sx.extract_salary_company(row)
    salary.location = await _sx.extract_salary_location(row)
    salary.data_points_count = await _sx.extract_salary_data_points(row)
    return salary


async def fetch_company_salaries(url: str, limit: int) -> List[Any]:
    """Get up to ``limit`` salary rows from Glassdoor."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as runtime:
            browser, ctx, page = await _open_page(runtime)
            salaries_url = _build_salaries_url(url)
            logger.info("[Glassdoor] Navigating to salaries: %s", salaries_url)
            await page.goto(salaries_url, wait_until="domcontentloaded", timeout=30000)
            await random_delay(1500, 2500)
            await _wait_for_selector(page, SALARIES_WAIT_SELECTOR, 15000)
            return await _gather_items(
                page, SALARY_ROW_SELECTOR, _build_salary, limit, "salary",
            )
    finally:
        await close_stealth_context(browser, ctx, page)
