"""Indeed HTML parsing helpers (split from indeed.py).

Splits the search-results parser and the job-detail parser into focused
helpers with narrow exception handling. Heavy lifting lives here so
indeed.py stays inside the code-quality ceilings.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from bs4 import BeautifulSoup

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    safe_extract,
)

logger = logging.getLogger("crawlrx.indeed.parsers")

INDEED_BASE_URL = "https://www.indeed.com"

INDEED_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *PARSE_EXCEPTIONS,
    *ELEMENT_PROBE_EXCEPTIONS,
)

_REMOTE_KEYWORDS = (
    "remote",
    "work from home",
    "wfh",
    "telecommute",
    "virtual",
    "fully remote",
    "100% remote",
)

_JOB_TYPE_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("full-time", "full time"), "Full-time"),
    (("part-time", "part time"), "Part-time"),
    (("contract",), "Contract"),
    (("internship", "intern"), "Internship"),
    (("temporary",), "Temporary"),
)

# Compile-once: `_job_id_from_card` runs once per result card; compiling
# these patterns inside the function paid the cost on every iteration.
_JK_HREF_RE = re.compile(r"jk=")
_JK_VALUE_RE = re.compile(r"jk=([a-zA-Z0-9]+)")


def extract_job_id_from_url(url: str) -> Optional[str]:
    """Extract job ID from an Indeed URL."""
    for pattern in (r"[?&]jk=([a-zA-Z0-9]+)", r"/viewjob.*?jk=([a-zA-Z0-9]+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_relative_date(date_text: Optional[str]) -> Optional[str]:
    """Parse relative dates like '3 days ago' or 'Just posted' into ISO."""
    if not date_text:
        return None
    text = date_text.lower().strip()
    if "just posted" in text or "today" in text:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    if "yesterday" in text:
        return (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return _parse_relative_unit(text) or text


def _parse_relative_unit(text: str) -> Optional[str]:
    """Convert ``"<N> day|hour|minute|week|month"`` to an ISO date string."""
    match = re.search(r"(\d+)\s+(day|hour|minute|week|month)", text)
    if not match:
        return None
    num = int(match.group(1))
    unit = match.group(2)
    now = datetime.now(tz=timezone.utc)
    if unit == "day":
        return (now - timedelta(days=num)).strftime("%Y-%m-%d")
    if unit in ("hour", "minute"):
        return now.strftime("%Y-%m-%d")
    if unit == "week":
        return (now - timedelta(weeks=num)).strftime("%Y-%m-%d")
    if unit == "month":
        return (now - timedelta(days=num * 30)).strftime("%Y-%m-%d")
    return None


def is_remote_job(
    title: str, location: Optional[str], snippet: Optional[str]
) -> bool:
    """True if any remote-job keyword appears in title/location/snippet."""
    haystack = f"{title} {location or ''} {snippet or ''}".lower()
    return any(kw in haystack for kw in _REMOTE_KEYWORDS)


def extract_job_type(snippet: Optional[str]) -> Optional[str]:
    """Map a job snippet to ``Full-time | Part-time | Contract | ...``."""
    if not snippet:
        return None
    lower = snippet.lower()
    for needles, label in _JOB_TYPE_PATTERNS:
        if any(needle in lower for needle in needles):
            return label
    return None


# ---------------------------------------------------------------------------
# Search-results parsing
# ---------------------------------------------------------------------------

def _select_first(card: Any, *selectors: str) -> Optional[Any]:
    """Return the first element matching any of ``selectors`` on ``card``."""
    for selector in selectors:
        match = card.select_one(selector)
        if match:
            return match
    return None


def _select_text(card: Any, *selectors: str) -> Optional[str]:
    """Return the stripped text of the first matching element, or None."""
    elem = _select_first(card, *selectors)
    return elem.get_text(strip=True) if elem else None


def _job_id_from_card(card: Any) -> Optional[str]:
    """Pull a ``data-jk`` job ID off the card or any embedded link."""
    job_id = card.get("data-jk") or card.get("data-testid")
    if job_id:
        return job_id
    link = card.find("a", href=_JK_HREF_RE)
    if not link:
        return None
    match = _JK_VALUE_RE.search(link.get("href", ""))
    return match.group(1) if match else None


def _build_search_job(card: Any, job_factory: Any) -> Optional[Any]:
    """Convert a single search-result card into an ``IndeedJob``."""
    job_id = _job_id_from_card(card)
    if not job_id:
        return None
    title = _select_text(
        card,
        "h2.jobTitle a",
        "a[data-testid='job-title']",
        "h2 a",
    )
    if not title:
        return None
    location = _select_text(
        card, "[data-testid='job-location']", "[class*='location']"
    )
    snippet = _select_text(
        card, "[class*='job-snippet']", "[class*='summary']"
    )
    posted = _select_text(card, "[data-testid='job-date']", "[class*='date']")

    return job_factory(
        title=title,
        company=_select_text(
            card,
            "[data-testid='company-name']",
            ".companyName",
            "[class*='company']",
        ),
        location=location,
        salary_range=_select_text(
            card, "[data-testid='job-salary']", "[class*='salary']"
        ),
        job_type=extract_job_type(snippet),
        posted_date=parse_relative_date(posted),
        job_url=f"{INDEED_BASE_URL}/viewjob?jk={job_id}",
        snippet=snippet,
        easy_apply=bool(card.select_one("[class*='easyApply']")),
        remote=is_remote_job(title, location, snippet),
        job_id=job_id,
    )


def parse_search_results_html(
    html: str,
    limit: int,
    job_factory: "Callable[..., Any]",
) -> List[Any]:
    """Parse Indeed search-results HTML into ``IndeedJob`` rows."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except INDEED_PARSE_EXCEPTIONS as exc:
        logger.warning("Indeed search soup error: %s", exc)
        return []

    cards = soup.select(
        "div[data-testid='jobTitle'], .job_seen_beacon, [data-jk]"
    )
    jobs: List[Any] = []
    seen: set = set()
    for card in cards[: limit * 2]:
        job = safe_extract(
            lambda card=card: _build_search_job(card, job_factory),
            field="indeed_card",
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
    return jobs[:limit]


# ---------------------------------------------------------------------------
# Job-detail parsing
# ---------------------------------------------------------------------------

def _hydrate_job_title(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.title`` from rendered HTML."""
    elem = (
        soup.select_one("h1[data-testid='job-title']")
        or soup.select_one("h1.job-title")
        or soup.select_one("h1")
    )
    job.title = elem.get_text(strip=True) if elem else ""


def _hydrate_job_company(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.company`` from rendered HTML."""
    elem = soup.select_one(
        "[data-testid='company-name'], [class*='company']"
    )
    job.company = elem.get_text(strip=True) if elem else None


def _hydrate_company_rating(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.company_rating`` (float) from a rating element."""
    elem = soup.select_one("[class*='rating']")
    if not elem:
        return
    match = re.search(r"([\d.]+)", elem.get_text(strip=True))
    if not match:
        return
    try:
        job.company_rating = float(match.group(1))
    except ValueError:
        pass


def _hydrate_simple_field(
    soup: BeautifulSoup,
    job: Any,
    field: str,
    *selectors: str,
) -> None:
    """Set ``job.<field>`` to the first matching element's stripped text."""
    elem = _select_first(soup, *selectors)
    setattr(job, field, elem.get_text(strip=True) if elem else None)


def _hydrate_description(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.description`` from the rendered description block."""
    elem = (
        soup.select_one("[data-testid='job-description']")
        or soup.select_one("[id='jobDescriptionText']")
        or soup.select_one("[class*='job-description']")
    )
    job.description = (
        elem.get_text(separator="\n", strip=True) if elem else None
    )


_REQ_PATTERNS: tuple[str, ...] = (
    r"(?:Requirements?|Qualifications?):\s*([^\n]+(?:\n[^\n]+)*)",
    r"(?:What You Need|Must Have):\s*([^\n]+(?:\n[^\n]+)*)",
)


def _hydrate_requirements(job: Any) -> None:
    """Pull a bulleted requirements list out of ``job.description``."""
    if not job.description:
        return
    for pattern in _REQ_PATTERNS:
        match = re.search(pattern, job.description, re.I)
        if not match:
            continue
        req_text = match.group(1)
        items = [r.strip() for r in re.split(r"[\n\-]", req_text) if r.strip()]
        job.requirements = items[:10]
        return


def _hydrate_benefits(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.benefits`` from a benefits section."""
    section = soup.select_one("[class*='benefits']")
    if not section:
        return
    items = section.select("li, [class*='benefit']")
    job.benefits = [b.get_text(strip=True) for b in items[:10]]


def _hydrate_apply_url(soup: BeautifulSoup, job: Any) -> None:
    """Populate ``job.apply_url`` (absolute URL) if present."""
    elem = soup.select_one("a[href*='apply'], [data-testid='apply-button']")
    if not elem:
        return
    href = elem.get("href")
    if not href:
        return
    job.apply_url = href if href.startswith("http") else f"{INDEED_BASE_URL}{href}"


def _build_initial_detail(detail_factory: Any, url: str, job_id: Optional[str]) -> Any:
    """Construct an empty IndeedJobDetail; ``url`` reserved for future field."""
    del url  # job_url is not on the IndeedJobDetail model -- preserved by extras
    return detail_factory(title="", job_id=job_id or "")


def parse_job_html(
    html: str,
    url: str,
    job_id: Optional[str],
    detail_factory: "Callable[..., Any]",
) -> Optional[Any]:
    """Parse a single Indeed job-detail page into ``IndeedJobDetail``."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except INDEED_PARSE_EXCEPTIONS as exc:
        logger.warning("Indeed job soup error for %s: %s", url, exc)
        return None

    job = _build_initial_detail(detail_factory, url, job_id)
    try:
        _hydrate_job_title(soup, job)
        _hydrate_job_company(soup, job)
        _hydrate_company_rating(soup, job)
        _hydrate_simple_field(
            soup, job, "location",
            "[data-testid='job-location']", "[class*='location']",
        )
        _hydrate_simple_field(
            soup, job, "salary",
            "[data-testid='job-salary']", "[class*='salary']",
        )
        _hydrate_simple_field(soup, job, "job_type", "[class*='job-type']")
        _hydrate_description(soup, job)
        _hydrate_requirements(job)
        _hydrate_benefits(soup, job)
        _hydrate_apply_url(soup, job)
    except INDEED_PARSE_EXCEPTIONS as exc:
        logger.warning("Error parsing Indeed job HTML: %s", exc)
        return None

    return job if job.title else None
