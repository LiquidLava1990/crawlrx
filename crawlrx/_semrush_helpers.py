"""SEMrush helper utilities (split from semrush.py for code-quality hygiene).

Provides login detection, number extraction, generic ``first-match`` selector
helpers, and parsers for keyword / page table rows. ``semrush.py`` orchestrates
these into the two public scrape endpoints.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS
from ._search_helpers import SERP_BROWSER_EXCEPTIONS

logger = logging.getLogger("crawlrx.semrush.helpers")

Page = Any


# ─────────────────────────────────────────────
# Login / number / delay primitives
# ─────────────────────────────────────────────

_LOGGED_IN_SELECTORS: Tuple[str, ...] = (
    "[data-test='user-menu']",
    ".user-profile",
    "[data-qa='user-avatar']",
    "a[href*='logout']",
    ".s-navigation__profile",
)
_LOGIN_FORM_SELECTORS: Tuple[str, ...] = (
    "input[name='email']",
    "input[type='password']",
    "[data-test='login-form']",
    ".login-form",
)


async def _matches_any(page: Page, selectors: Tuple[str, ...]) -> bool:
    for selector in selectors:
        if await page.query_selector(selector):
            return True
    return False


async def is_logged_in(page: Page) -> bool:
    """Return True iff a logged-in indicator is visible (login form absent)."""
    try:
        if await _matches_any(page, _LOGGED_IN_SELECTORS):
            return True
        if await _matches_any(page, _LOGIN_FORM_SELECTORS):
            return False
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.warning("Error checking login status: %s", type(exc).__name__)
        return False
    return False


_NUMBER_MULTIPLIERS: Dict[str, int] = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def extract_number(text: Optional[str]) -> Optional[int]:
    """Parse ``"1.2M"`` / ``"45.6K"`` / ``"1,234"`` into an int."""
    if not text:
        return None
    cleaned = text.strip().upper().replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    try:
        for suffix, mult in _NUMBER_MULTIPLIERS.items():
            if suffix in cleaned:
                return int(float(cleaned.replace(suffix, "")) * mult)
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


async def random_delay(min_ms: int = 1000, max_ms: int = 2000) -> None:
    """Sleep a random fraction (1-2s default) for stealth."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


# ─────────────────────────────────────────────
# First-match selector helpers
# ─────────────────────────────────────────────

async def first_text(page: Page, selectors: Tuple[str, ...]) -> Optional[str]:
    """Return inner_text of the first matching selector (stripped)."""
    for selector in selectors:
        elem = await page.query_selector(selector)
        if not elem:
            continue
        try:
            return (await elem.inner_text()).strip()
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("inner_text on %s failed: %s", selector, type(exc).__name__)
            continue
    return None


async def first_number(
    page: Page, selectors: Tuple[str, ...],
) -> Optional[int]:
    """Read first matching selector's text and parse as a number."""
    text = await first_text(page, selectors)
    return extract_number(text) if text is not None else None


# ─────────────────────────────────────────────
# Domain-overview selector groups
# ─────────────────────────────────────────────

DOMAIN_TRAFFIC_SELECTORS: Tuple[str, ...] = (
    "[data-test='organic-traffic']",
    ".organic-traffic-value",
    "[data-qa='organic-traffic']",
    ".s-widget__traffic-value",
)
DOMAIN_PAID_SELECTORS: Tuple[str, ...] = (
    "[data-test='paid-traffic']",
    ".paid-traffic-value",
    "[data-qa='paid-traffic']",
)
DOMAIN_BACKLINK_SELECTORS: Tuple[str, ...] = (
    "[data-test='backlinks-count']",
    ".backlinks-value",
    "[data-qa='backlinks']",
)
DOMAIN_KEYWORDS_SELECTORS: Tuple[str, ...] = (
    "[data-test='organic-keywords-count']",
    ".organic-keywords-value",
    "[data-qa='organic-keywords']",
)
DOMAIN_AUTHORITY_SELECTORS: Tuple[str, ...] = (
    "[data-test='authority-score']",
    ".authority-score-value",
    "[data-qa='authority-score']",
    ".as-score",
)


KEYWORDS_TABLE_SELECTORS: Tuple[str, ...] = (
    "[data-test='organic-keywords-table'] tr",
    ".keywords-table tr",
    "[data-qa='keywords-table'] tr",
    ".s-table__row",
)
PAGES_TABLE_SELECTORS: Tuple[str, ...] = (
    "[data-test='top-pages-table'] tr",
    ".pages-table tr",
    "[data-qa='pages-table'] tr",
)


@dataclass
class DomainOverview:
    """Mutable accumulator for the domain-overview scrape."""

    domain: str
    organic_traffic: Optional[int] = None
    paid_traffic: Optional[int] = None
    backlinks: Optional[int] = None
    organic_keywords: Optional[int] = None
    domain_authority: Optional[int] = None
    top_keywords: List[Dict[str, Any]] = field(default_factory=list)
    top_pages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "organic_traffic": self.organic_traffic,
            "paid_traffic": self.paid_traffic,
            "backlinks": self.backlinks,
            "organic_keywords": self.organic_keywords,
            "domain_authority": self.domain_authority,
            "top_keywords": self.top_keywords,
            "top_pages": self.top_pages,
        }


def normalize_domain(domain: str) -> str:
    """Strip protocol/www/trailing slash from a host string."""
    cleaned = domain.replace("https://", "").replace("http://", "")
    return cleaned.replace("www.", "").strip("/")


# ─────────────────────────────────────────────
# Table-row parsers
# ─────────────────────────────────────────────

async def _row_cells(row: Any) -> List[Any]:
    return await row.query_selector_all("td")


async def _cell_text(cell: Any) -> str:
    try:
        return (await cell.inner_text()).strip()
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Cell inner_text failed: %s", type(exc).__name__)
        return ""


async def _parse_keyword_row(row: Any) -> Optional[Dict[str, Any]]:
    cells = await _row_cells(row)
    if len(cells) < 3:
        return None
    keyword = await _cell_text(cells[0])
    if not keyword:
        return None
    return {
        "keyword": keyword,
        "volume": extract_number(await _cell_text(cells[1])),
        "position": extract_number(await _cell_text(cells[2])),
    }


async def _parse_page_row(row: Any) -> Optional[Dict[str, Any]]:
    cells = await _row_cells(row)
    if len(cells) < 2:
        return None
    page_url = await _cell_text(cells[0])
    if not page_url:
        return None
    return {
        "url": page_url,
        "traffic": extract_number(await _cell_text(cells[1])),
    }


async def _find_data_rows(
    page: Page, selectors: Tuple[str, ...],
) -> List[Any]:
    """Return the first non-trivial (>1 row) match -- header is at index 0."""
    for row_sel in selectors:
        rows = await page.query_selector_all(row_sel)
        if rows and len(rows) > 1:
            return rows
    return []


async def collect_top_keywords(page: Page, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the first ``limit`` parseable keyword rows."""
    rows = await _find_data_rows(page, KEYWORDS_TABLE_SELECTORS)
    out: List[Dict[str, Any]] = []
    for row in rows[1: limit + 1]:
        record = await _parse_keyword_row(row)
        if record:
            out.append(record)
    return out


async def collect_top_pages(page: Page, limit: int = 5) -> List[Dict[str, Any]]:
    """Return the first ``limit`` parseable top-page rows."""
    rows = await _find_data_rows(page, PAGES_TABLE_SELECTORS)
    out: List[Dict[str, Any]] = []
    for row in rows[1: limit + 1]:
        record = await _parse_page_row(row)
        if record:
            out.append(record)
    return out


# ─────────────────────────────────────────────
# Keyword-magic selector groups + parsers
# ─────────────────────────────────────────────

KW_VOLUME_SELECTORS: Tuple[str, ...] = (
    "[data-test='volume-value']", ".volume-cell", "[data-qa='volume']",
)
KW_CPC_SELECTORS: Tuple[str, ...] = (
    "[data-test='cpc-value']", ".cpc-cell", "[data-qa='cpc']",
)
KW_COMPETITION_SELECTORS: Tuple[str, ...] = (
    "[data-test='competition-value']",
    ".competition-cell",
    "[data-qa='competition']",
)
KW_DIFFICULTY_SELECTORS: Tuple[str, ...] = (
    "[data-test='kd-value']",
    ".kd-cell",
    "[data-qa='keyword-difficulty']",
    ".kd-score",
)


_COMPETITION_TIERS = (("high", 0.8), ("medium", 0.5), ("low", 0.2))


def _parse_competition(text: str) -> Optional[float]:
    lowered = text.lower()
    for token, score in _COMPETITION_TIERS:
        if token in lowered:
            return score
    parsed = extract_number(text)
    return float(parsed) if parsed is not None else None


def _parse_cpc(text: str) -> Optional[float]:
    cleaned = text.replace("$", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


_TREND_BAR_RE = re.compile(r"(\d+)%")


async def collect_trend_bars(page: Page, limit: int = 12) -> List[int]:
    """Read the ``style="...%"`` trend bars (first ``limit``)."""
    try:
        bars = await page.query_selector_all("[data-test='trend-bar']")
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.debug("Trend bar selector failed: %s", type(exc).__name__)
        return []
    out: List[int] = []
    for bar in bars[:limit]:
        try:
            style = await bar.get_attribute("style")
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Trend bar attr failed: %s", type(exc).__name__)
            continue
        match = _TREND_BAR_RE.search(style or "")
        if match:
            out.append(int(match.group(1)))
    return out


async def parse_keyword_metrics(page: Page) -> Dict[str, Any]:
    """Read volume / cpc / competition / difficulty from the loaded page."""
    cpc_text = await first_text(page, KW_CPC_SELECTORS)
    comp_text = await first_text(page, KW_COMPETITION_SELECTORS)
    diff_text = await first_text(page, KW_DIFFICULTY_SELECTORS)
    return {
        "volume": await first_number(page, KW_VOLUME_SELECTORS),
        "cpc": _parse_cpc(cpc_text) if cpc_text else None,
        "competition": _parse_competition(comp_text) if comp_text else None,
        "difficulty": (
            extract_number(diff_text.replace("%", "")) if diff_text else None
        ),
    }
