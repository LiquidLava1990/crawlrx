"""Single-field extractors for Glassdoor scrapers.

Each helper drives one Playwright probe + one parse step; failures fall
through ``safe_extract_async`` instead of bare ``except Exception``. Keeping
these as ten-line probes lets the orchestrators in ``_glassdoor_pages``
stay below the code-quality cognitive / nesting / cyclomatic ceilings.

Re-imported by :mod:`crawlrx.glassdoor` -- callers should not depend on this
private surface.

Type hints use ``Any`` for Playwright handles to keep this module importable
without a hard ``playwright`` dependency at import time.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import safe_extract_async

logger = logging.getLogger("crawlrx.glassdoor.extractors")


# ---------------------------------------------------------------------------
# Page primitives -- thin wrappers around Playwright with safe_extract_async.
# ---------------------------------------------------------------------------

async def _query_first(page: Any, selectors: Iterable[str]) -> Any:
    """Return the first selector match or None (network/DOM-safe)."""
    for selector in selectors:
        element = await safe_extract_async(
            lambda sel=selector: page.query_selector(sel),
            field=f"query:{selector}",
        )
        if element:
            return element
    return None


async def _inner_text(element: Any) -> Optional[str]:
    """``element.inner_text()`` returning ``None`` on probe failure."""
    if element is None:
        return None
    text = await safe_extract_async(element.inner_text, field="inner_text")
    if isinstance(text, str):
        return text
    return None


# ---------------------------------------------------------------------------
# Parsing primitives reused across fields.
# ---------------------------------------------------------------------------

def parse_rating(rating_text: Optional[str]) -> Optional[float]:
    """Extract rating from text like '4.5' or '4.5 out of 5'."""
    if not rating_text:
        return None
    match = re.search(r"([\d.]+)", rating_text)
    if not match:
        return None
    try:
        rating = float(match.group(1))
    except ValueError:
        return None
    return rating if 0 <= rating <= 5 else None


def parse_percentage(text: Optional[str]) -> Optional[str]:
    """Extract percentage like '87%' from free text."""
    if not text:
        return None
    match = re.search(r"(\d+)%", text)
    return f"{match.group(1)}%" if match else None


def _match_int(text: Optional[str], pattern: str) -> Optional[int]:
    """Run ``pattern`` against ``text`` and return ``int(match.group(1))``."""
    if not text:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_int_with_commas(text: Optional[str]) -> Optional[int]:
    """Pull the first integer (commas allowed) out of free text."""
    return _match_int(text, r"([\d,]+)")


# ---------------------------------------------------------------------------
# Company-overview field extractors.
# ---------------------------------------------------------------------------

NAME_SELECTORS = (
    "[data-test='employer-name']",
    "h1.employerName",
    "h1[data-test='employer-overview-heading']",
)

OVERALL_RATING_SELECTORS = (
    "[data-test='overallRating']",
    ".ratingNum",
    "[data-test='rating']",
)

RATING_BREAKDOWN_SELECTORS: tuple[tuple[str, str], ...] = (
    ("[data-test='cultureAndValues']", "culture"),
    ("[data-test='workLife']", "work_life"),
    ("[data-test='seniorManagement']", "management"),
    ("[data-test='compAndBenefits']", "compensation"),
    ("[data-test='careerOpportunities']", "career"),
)

CEO_APPROVAL_SELECTORS = (
    "[data-test='ceo-approval']",
    "[data-test='ceoRating']",
)

RECOMMEND_SELECTORS = (
    "[data-test='recommend-to-friend']",
    "[data-test='recommendRating']",
)

REVIEW_COUNT_SELECTORS = (
    "[data-test='review-count']",
    "[data-test='reviews']",
)

SALARY_COUNT_SELECTORS = (
    "[data-test='salary-count']",
    "[data-test='salaries']",
)

JOB_COUNT_SELECTORS = (
    "[data-test='job-count']",
    "[data-test='jobs']",
)

DESCRIPTION_SELECTORS = (
    "[data-test='employer-description']",
    "[data-test='employerDescription']",
    ".employerDescription",
)


async def extract_company_name(page: "Page") -> Optional[str]:
    element = await _query_first(page, NAME_SELECTORS)
    text = await _inner_text(element)
    return text.strip() if text else None


async def extract_overall_rating(page: "Page") -> Optional[float]:
    element = await _query_first(page, OVERALL_RATING_SELECTORS)
    text = await _inner_text(element)
    return parse_rating(text)


async def extract_ratings_breakdown(page: "Page") -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {}
    for selector, attr in RATING_BREAKDOWN_SELECTORS:
        element = await safe_extract_async(
            lambda sel=selector: page.query_selector(sel),
            field=f"breakdown:{attr}",
        )
        if not element:
            continue
        text = await _inner_text(element)
        out[attr] = parse_rating(text)
    return out


async def extract_ceo_approval(page: "Page") -> Optional[str]:
    element = await _query_first(page, CEO_APPROVAL_SELECTORS)
    text = await _inner_text(element)
    return parse_percentage(text)


async def extract_recommend_pct(page: "Page") -> Optional[str]:
    element = await _query_first(page, RECOMMEND_SELECTORS)
    text = await _inner_text(element)
    return parse_percentage(text)


async def extract_review_count(page: "Page") -> Optional[int]:
    element = await _query_first(page, REVIEW_COUNT_SELECTORS)
    text = await _inner_text(element)
    return parse_int_with_commas(text)


async def extract_salary_count(page: "Page") -> Optional[int]:
    element = await _query_first(page, SALARY_COUNT_SELECTORS)
    text = await _inner_text(element)
    return parse_int_with_commas(text)


async def extract_job_count(page: "Page") -> Optional[int]:
    element = await _query_first(page, JOB_COUNT_SELECTORS)
    text = await _inner_text(element)
    return parse_int_with_commas(text)


async def extract_description(page: "Page") -> Optional[str]:
    element = await _query_first(page, DESCRIPTION_SELECTORS)
    text = await _inner_text(element)
    return text.strip() if text else None


async def extract_pros_cons(page: "Page") -> tuple[Optional[str], Optional[str]]:
    pros_element = await _query_first(page, ("[data-test='pros']", ".pros"))
    pros = await _inner_text(pros_element)
    cons_element = await _query_first(page, ("[data-test='cons']", ".cons"))
    cons = await _inner_text(cons_element)
    return (
        pros.strip() if pros else None,
        cons.strip() if cons else None,
    )


async def extract_website(page: "Page") -> Optional[str]:
    element = await _query_first(
        page,
        ("[data-test='employer-website']", "a[href^='http']"),
    )
    if element is None:
        return None
    href = await safe_extract_async(
        lambda: element.get_attribute("href"),
        field="website_href",
    )
    if not href or href.startswith("/"):
        return None
    return href


# ---------------------------------------------------------------------------
# Company-detail row classifier (size / founded / HQ / industry).
# ---------------------------------------------------------------------------

INDUSTRY_KEYWORDS = (
    "software",
    "technology",
    "healthcare",
    "finance",
    "retail",
    "manufacturing",
)


def classify_detail_row(text: str) -> Optional[str]:
    """Map a company-info row text to a `GlassdoorCompany` attribute name."""
    lowered = text.lower()
    if "employees" in lowered:
        return "size"
    if "founded" in lowered:
        return "founded"
    if "headquarters" in lowered or "hq:" in lowered:
        return "headquarters"
    if any(keyword in lowered for keyword in INDUSTRY_KEYWORDS):
        return "industry"
    return None


async def extract_detail_rows(page: "Page") -> dict[str, str]:
    """Return ``{attr_name: text}`` for company-info rows we recognise."""
    out: dict[str, str] = {}
    elements = await safe_extract_async(
        lambda: page.query_selector_all(
            "[data-test='employer-details'] div, .employer-info div"
        ),
        field="detail_rows",
        default=[],
    )
    for element in elements or []:
        text = await _inner_text(element)
        if not text:
            continue
        stripped = text.strip()
        attr = classify_detail_row(stripped)
        if attr and attr not in out:
            out[attr] = stripped
    return out


# ---------------------------------------------------------------------------
# Review-card field extractors (operates on a card element).
# ---------------------------------------------------------------------------

REVIEW_TITLE_SELECTORS = (
    "[data-test='review-title'] a",
    ".reviewLink",
    "h2 a",
)

REVIEW_RATING_SELECTORS = (
    "[data-test='rating'] span",
    ".ratingNum",
    ".rating span",
)

REVIEW_PROS_SELECTORS = (
    "[data-test='pros']",
    ". pros",
    "span[data-test='pros']",
)

REVIEW_CONS_SELECTORS = (
    "[data-test='cons']",
    ".cons",
    "span[data-test='cons']",
)

REVIEW_AUTHOR_SELECTORS = (
    "[data-test='author-info']",
    ".authorInfo",
    ".author",
)

REVIEW_DATE_SELECTORS = (
    "[data-test='review-date']",
    ".date",
    "time",
)


async def _card_query_first(card: Any, selectors: Iterable[str]) -> Any:
    """First matching child of `card`, or None on probe failure."""
    for selector in selectors:
        element = await safe_extract_async(
            lambda sel=selector: card.query_selector(sel),
            field=f"card_query:{selector}",
        )
        if element:
            return element
    return None


async def _card_inner_text(card: Any, selectors: Iterable[str]) -> Optional[str]:
    element = await _card_query_first(card, selectors)
    text = await _inner_text(element)
    return text.strip() if text else None


async def extract_review_title(card: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(card, REVIEW_TITLE_SELECTORS)


async def extract_review_rating(card: "ElementHandle") -> Optional[int]:
    text = await _card_inner_text(card, REVIEW_RATING_SELECTORS)
    return _match_int(text, r"(\d)")


async def extract_review_pros(card: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(card, REVIEW_PROS_SELECTORS)


async def extract_review_cons(card: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(card, REVIEW_CONS_SELECTORS)


async def extract_review_advice(card: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(
        card, ("[data-test='advice-management']", ".adviceMgmt")
    )


def parse_author_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(employment_status, job_title)`` from author free text."""
    if "Current" not in text and "Former" not in text:
        return (None, text or None)
    parts = text.split(",")
    if len(parts) < 2:
        return (text, None)
    return (parts[0].strip(), ",".join(parts[1:]).strip())


async def extract_review_author(card: "ElementHandle") -> tuple[Optional[str], Optional[str]]:
    text = await _card_inner_text(card, REVIEW_AUTHOR_SELECTORS)
    if not text:
        return (None, None)
    return parse_author_text(text)


async def extract_review_date(card: "ElementHandle") -> Optional[str]:
    return await _card_inner_text(card, REVIEW_DATE_SELECTORS)


async def extract_review_helpful(card: "ElementHandle") -> Optional[int]:
    text = await _card_inner_text(
        card, ("[data-test='helpful-count']", ".helpfulCount")
    )
    return _match_int(text, r"(\d+)")


async def extract_review_recommend(card: "ElementHandle") -> Optional[bool]:
    text = await _card_inner_text(card, ("[data-test='recommends']",))
    if not text:
        return None
    lowered = text.lower()
    return "recommend" in lowered and "not" not in lowered


def classify_outlook(text: str) -> Optional[str]:
    lowered = text.lower()
    if "positive" in lowered:
        return "Positive"
    if "negative" in lowered:
        return "Negative"
    return "Neutral"


async def extract_review_outlook(card: "ElementHandle") -> Optional[str]:
    text = await _card_inner_text(
        card, ("[data-test='positive-outlook']", "[data-test='outlook']")
    )
    if not text:
        return None
    return classify_outlook(text)


# ---------------------------------------------------------------------------
# Salary-row extractors live in `_glassdoor_salary_extractors.py` (split for
# code-quality file-size ceiling).
# ---------------------------------------------------------------------------
