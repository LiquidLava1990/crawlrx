"""Helpers for the Trustpilot scraper.

Split out of ``trustpilot.py`` for readability: removes deeply-
nested ``_extract_star_percentages`` + the duplicated review-card
extractors that previously appeared in both the HTTP and Playwright
paths.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright
    from bs4 import BeautifulSoup


from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.trustpilot._helpers")

TRUSTPILOT_BASE_URL = "https://www.trustpilot.com"

# Module-level compiled regex constants (hoisted from function bodies for perf).
_RE_NON_WORD_NORMALIZE = re.compile(r"[^\w\s-]")
_RE_DASH_SPACE_NORMALIZE = re.compile(r"[-\s]+")
_RE_PERCENT = re.compile(r"(\d+)%")
_RE_SINGLE_DIGIT = re.compile(r"(\d)")
_RE_DIGITS = re.compile(r"(\d+)")
_RE_FLOAT = re.compile(r"(\d+\.?\d*)")
_RE_FLOAT_STRICT = re.compile(r"(\d+\.\d+)")
_RE_DIGITS_COMMA = re.compile(r"([\d,]+)")
_RE_STAR_IMG_ALT = re.compile(r"star")
_RE_REVIEW_CONTENT = re.compile("reviewContent")
_RE_CONSUMER_NAME = re.compile("consumerName")
_RE_HELPFUL = re.compile(r"helpful", re.I)
_RE_VERIFIED = re.compile(r"verified|confirmed", re.I)
_RE_REPLY_RESPONSE = re.compile(r"reply|response")
_RE_COMPANY_NAME_CLS = re.compile("companyName")
_RE_TRUSTSCORE_CLS = re.compile("trustScore")
_RE_TRUSTSCORE_TEXT = re.compile(r"TrustScore")
_RE_REVIEWS_TEXT = re.compile(r"\d+[,.]?\d*\s+reviews", re.I)
_RE_VISIT_WEBSITE = re.compile(r"Visit|Website", re.I)
_RE_RESPONDED_TO = re.compile(r"responded to", re.I)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

async def random_delay(min_ms: int = 300, max_ms: int = 800) -> None:
    """Sleep for a random duration between ``min_ms`` and ``max_ms``."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def normalize_company_name(name: str) -> str:
    """Lowercase + dasherise a company name for the Trustpilot URL."""
    normalized = _RE_NON_WORD_NORMALIZE.sub("", name.lower())
    normalized = _RE_DASH_SPACE_NORMALIZE.sub("-", normalized)
    return normalized.strip("-")


def get_company_url(
    company_name: Optional[str], url: Optional[str],
) -> str:
    """Resolve the Trustpilot company URL from ``url`` or ``company_name``."""
    if url:
        return url if url.startswith("http") else f"{TRUSTPILOT_BASE_URL}{url}"
    if company_name:
        slug = normalize_company_name(company_name)
        return f"{TRUSTPILOT_BASE_URL}/review/{slug}"
    raise ValueError("Either company_name or url must be provided")


# ---------------------------------------------------------------------------
# Star-percent extraction (was 6-deep nested loop)
# ---------------------------------------------------------------------------

_STAR_FIELDS = {5: "five_star", 4: "four_star", 3: "three_star",
                2: "two_star", 1: "one_star"}

# Pre-compiled "{N} star" needles, keyed by digit. Avoids re.compile in the
# hot path that runs once per star tier.
_STAR_NEEDLES = {n: re.compile(re.escape(str(n)) + " star", re.I)
                 for n in _STAR_FIELDS}


def _first_number_near_node(
    node: Any, pattern: "re.Pattern[str]", caster: Any,
) -> Any:
    """Run ``pattern.search`` on ``node.parent.get_text()`` and cast it."""
    parent = getattr(node, "parent", None)
    if parent is None:
        return None
    match = pattern.search(parent.get_text())
    if not match:
        return None
    try:
        return caster(match.group(1))
    except (ValueError, TypeError):
        return None


def _percent_near_node(node: Any) -> Optional[float]:
    """Search ``node.parent.get_text()`` for a ``NN%`` value."""
    return _first_number_near_node(node, _RE_PERCENT, float)


def _star_percent_for(soup: Any, count: int) -> Optional[float]:
    """First ``NN%`` value adjacent to a ``"{count} star"`` text node."""
    needle = _STAR_NEEDLES.get(count) or re.compile(
        re.escape(str(count)) + " star", re.I,
    )
    try:
        candidates = soup.find_all(text=needle)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("trustpilot star-find failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    for el in candidates:
        pct = _percent_near_node(el)
        if pct is not None:
            return pct
    return None


def extract_star_percentages(
    soup: "BeautifulSoup", breakdown_cls: "type[Any]",
) -> object:
    """Return a fresh ``RatingBreakdown`` filled from soup."""
    breakdown = breakdown_cls()
    for count, field in _STAR_FIELDS.items():
        pct = _star_percent_for(soup, count)
        if pct is not None:
            setattr(breakdown, field, pct)
    return breakdown


# ---------------------------------------------------------------------------
# Review-card extraction (HTTP + Playwright share these)
# ---------------------------------------------------------------------------

def _bs_text(el: Any) -> str:
    """``el.get_text(strip=True)`` if ``el`` is truthy, else ``""``."""
    if not el:
        return ""
    return el.get_text(strip=True)


def _bs_attr(el: Any, name: str, default: str = "") -> str:
    """``el.get(name, default)`` if ``el`` is truthy, else ``default``."""
    if not el:
        return default
    return el.get(name, default)


def _extract_review_title(card: Any) -> Optional[str]:
    """Title cell -- prefer ``<h2>``, fall back to ``data-testid``."""
    el = card.find("h2") or card.find(attrs={"data-testid": "review-title"})
    return _bs_text(el) or None


def _extract_review_rating(card: Any) -> Optional[int]:
    """Star count from the alt-text of the rating image / element."""
    el = (
        card.find("img", alt=_RE_STAR_IMG_ALT)
        or card.find(attrs={"data-testid": "review-rating"})
    )
    alt = _bs_attr(el, "alt")
    match = _RE_SINGLE_DIGIT.search(alt) if alt else None
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_review_text(card: Any) -> Optional[str]:
    """Body text -- inspect ``<p>``, ``data-testid``, then ``reviewContent``."""
    el = (
        card.find("p")
        or card.find(attrs={"data-testid": "review-text"})
        or card.find(class_=_RE_REVIEW_CONTENT)
    )
    return _bs_text(el) or None


def _extract_review_author(card: Any) -> Optional[str]:
    """Author cell -- ``data-testid`` first, then a ``consumerName`` class."""
    el = (
        card.find(attrs={"data-testid": "review-author"})
        or card.find(class_=_RE_CONSUMER_NAME)
    )
    return _bs_text(el) or None


def _extract_review_date(card: Any) -> Optional[str]:
    """Date cell -- ``data-testid`` first, fallback to any ``<time>`` tag."""
    el = card.find(attrs={"data-testid": "review-date"}) or card.find(time=True)
    if not el:
        return None
    return _bs_text(el) or _bs_attr(el, "datetime") or None


def _extract_review_helpful_count(card: Any) -> Optional[int]:
    """Find a digit count near a ``"helpful"`` text node."""
    helpful = card.find(text=_RE_HELPFUL)
    if not helpful:
        return None
    return _first_number_near_node(helpful, _RE_DIGITS, int)


def parse_http_review_card(card: "ElementHandle", review_cls: "type[Any]") -> object:
    """Build one ``TrustpilotReview`` from a BeautifulSoup card."""
    review = review_cls()
    review.title = _extract_review_title(card)
    review.rating = _extract_review_rating(card)
    review.text = _extract_review_text(card)
    review.author = _extract_review_author(card)
    review.date = _extract_review_date(card)
    verified = card.find(text=_RE_VERIFIED)
    review.verified_purchase = bool(verified)
    review.helpful_count = _extract_review_helpful_count(card)
    reply_el = card.find(class_=_RE_REPLY_RESPONSE)
    if reply_el:
        review.company_reply = _bs_text(reply_el) or None
    return review


def collect_http_reviews(
    cards: Iterable[Any],
    review_cls: "type[Any]",
    limit: Optional[int] = None,
) -> List[Any]:
    """Convert all BS4 review cards to typed reviews, swallowing parse errors."""
    out: List[Any] = []
    for card in cards:
        if limit is not None and len(out) >= limit:
            break
        try:
            review = parse_http_review_card(card, review_cls)
        except PARSE_EXCEPTIONS as exc:
            logger.debug("trustpilot review parse failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        out.append(review)
    return out


# ---------------------------------------------------------------------------
# Playwright review-card helpers
# ---------------------------------------------------------------------------

async def _pw_inner_text(card: Any, selector: str) -> Optional[str]:
    """``card.query_selector(selector).inner_text()`` -- None on miss/error."""
    try:
        el = card.query_selector(selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("trustpilot pw selector(%s) failed (%s): %s",
                     selector, type(exc).__name__, exc)
        return None
    if not el:
        return None
    try:
        text = await el.inner_text()
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("trustpilot pw inner_text(%s) failed (%s): %s",
                     selector, type(exc).__name__, exc)
        return None
    return text.strip() if isinstance(text, str) else None


async def _pw_attr(card: Any, selector: str, attr: str) -> Optional[str]:
    """Attribute fetch with a narrow except family."""
    try:
        el = card.query_selector(selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("trustpilot pw attr selector(%s) failed (%s): %s",
                     selector, type(exc).__name__, exc)
        return None
    if not el:
        return None
    try:
        return await el.get_attribute(attr)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("trustpilot pw get_attribute(%s) failed (%s): %s",
                     attr, type(exc).__name__, exc)
        return None


def _rating_from_alt(alt: Optional[str]) -> Optional[int]:
    """Pull a single digit out of star alt-text (``"5 star"`` etc.)."""
    if not alt:
        return None
    match = _RE_SINGLE_DIGIT.search(alt)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def parse_pw_review_card(card: "ElementHandle", review_cls: "type[Any]") -> object:
    """Build one ``TrustpilotReview`` from a Playwright card handle."""
    review = review_cls()
    review.title = await _pw_inner_text(card, "h2, [data-testid='review-title']")
    review.text = await _pw_inner_text(card, "[data-testid='review-text'], p")
    review.author = await _pw_inner_text(card, "[data-testid='review-author']")
    review.date = await _pw_inner_text(card, "[data-testid='review-date']")
    review.rating = _rating_from_alt(
        await _pw_attr(card, "[data-testid='review-rating']", "alt")
    )
    reply = await _pw_inner_text(card, "[data-testid='company-reply']")
    if reply:
        review.company_reply = reply
    return review


async def collect_pw_reviews(
    cards: Iterable[Any],
    review_cls: "type[Any]",
    limit: int,
) -> List[Any]:
    """Convert all Playwright review cards into typed reviews."""
    out: List[Any] = []
    for card in cards:
        if len(out) >= limit:
            break
        try:
            review = await parse_pw_review_card(card, review_cls)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("trustpilot pw card parse failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        out.append(review)
    return out


# ---------------------------------------------------------------------------
# Soup-driven company-page extractors
# ---------------------------------------------------------------------------

def extract_company_name(soup: "BeautifulSoup") -> str:
    """Pick the first plausible company-name element."""
    el = (
        soup.find("h1")
        or soup.find("span", class_=_RE_COMPANY_NAME_CLS)
        or soup.find("span", attrs={"data-testid": "company-name"})
    )
    return _bs_text(el) or "Unknown"


def _trustscore_from_element(el: Any) -> Optional[float]:
    """Pull a ``D.D`` float out of an element's text."""
    if not el:
        return None
    match = _RE_FLOAT.search(el.get_text())
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _trustscore_from_text_nodes(soup: Any) -> Optional[float]:
    """Fallback hunt for ``trustScore`` references across the soup."""
    for node in soup.find_all(text=_RE_FLOAT_STRICT):
        parent = getattr(node, "parent", None)
        if parent is None or parent.name not in ("span", "p", "div"):
            continue
        text = parent.get_text().lower()
        if "trust" not in text and "score" not in text:
            continue
        match = _RE_FLOAT_STRICT.search(parent.get_text())
        if not match:
            continue
        try:
            score = float(match.group(1))
        except ValueError:
            continue
        if 1 <= score <= 5:
            return score
    return None


def extract_trustscore(soup: "BeautifulSoup") -> Optional[float]:
    """Best-effort trust-score float in the range ``[1, 5]``."""
    el = (
        soup.find("p", text=_RE_TRUSTSCORE_TEXT)
        or soup.find("span", class_=_RE_TRUSTSCORE_CLS)
        or soup.find("span", attrs={"data-testid": "trust-score"})
    )
    score = _trustscore_from_element(el)
    if score is not None:
        return score
    return _trustscore_from_text_nodes(soup)


def extract_total_reviews(soup: "BeautifulSoup") -> Optional[int]:
    """Pull total-reviews integer from the canonical or fallback selectors."""
    el = (
        soup.find("span", attrs={"data-testid": "total-reviews"})
        or soup.find(text=_RE_REVIEWS_TEXT)
    )
    if not el:
        return None
    text = el.get_text() if hasattr(el, "get_text") else str(el)
    match = _RE_DIGITS_COMMA.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_website(soup: "BeautifulSoup") -> Optional[str]:
    """Best-effort website URL associated with the company page."""
    el = (
        soup.find("a", attrs={"data-testid": "company-website"})
        or soup.find("a", text=_RE_VISIT_WEBSITE)
    )
    return _bs_attr(el, "href") or None


def extract_response_rate(soup: "BeautifulSoup") -> Optional[str]:
    """Find the ``"responded to"`` snippet (or None)."""
    node = soup.find(text=_RE_RESPONDED_TO)
    if not node:
        return None
    parent = getattr(node, "parent", None)
    if parent is None:
        return None
    return parent.get_text(strip=True) or None


# ---------------------------------------------------------------------------
# Re-export bundle for the trustpilot module
# ---------------------------------------------------------------------------

__all__ = [
    "BROWSER_SCRAPE_EXCEPTIONS",
    "HTTP_HEADERS",
    "PARSE_EXCEPTIONS",
    "SCRAPE_FAILURE_EXCEPTIONS",
    "TRUSTPILOT_BASE_URL",
    "collect_http_reviews",
    "collect_pw_reviews",
    "extract_company_name",
    "extract_response_rate",
    "extract_star_percentages",
    "extract_total_reviews",
    "extract_trustscore",
    "extract_website",
    "get_company_url",
    "normalize_company_name",
    "random_delay",
]
