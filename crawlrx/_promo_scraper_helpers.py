"""Helpers for ``promo_scraper.py`` -- coupon DOM parsing + HTTP fetch primitives.

Extracted to keep ``promo_scraper.py`` below complexity thresholds (cognitive,
nesting, duplication) and to give the two near-identical RetailMeNot /
Coupons.com paths a single shared scrape pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from bs4 import BeautifulSoup

from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger(__name__)

# Network failures swallowed at the HTTP boundary. ``requests`` is the only
# transport, so the surface is tightly bounded.
_REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    OSError,
    TimeoutError,
    UnicodeDecodeError,
)


# Default selector lists used by the coupon-card hunters in `_parse_coupon_elems`.
_CODE_SELECTORS: List[str] = [
    ".code-text",
    ".promo-code",
    '[data-testid="code"]',
    ".coupon-code",
    '[class*="code"]',
]
_DISCOUNT_SELECTORS: List[str] = [
    ".offer-title",
    ".discount-text",
    '[data-testid="title"]',
    "h3",
    "h4",
    ".title",
    '[class*="title"]',
]
_DESCRIPTION_SELECTORS: List[str] = [
    ".offer-description",
    ".description",
    '[data-testid="description"]',
    "p",
    '[class*="description"]',
]
_EXPIRES_SELECTORS: List[str] = [
    ".expiration",
    ".expiry",
    '[data-testid="expiration"]',
    '[class*="expir"]',
]
_VERIFIED_SELECTORS: List[str] = [
    ".verified",
    '[data-testid="verified"]',
    '[class*="verified"]',
]


def first_text(elem: "ElementHandle | None", selectors: List[str]) -> str:
    """Return text of first matching selector, or ''."""
    for sel in selectors:
        found = elem.select_one(sel)
        if found:
            return found.get_text(strip=True)
    return ""


def is_verified(elem: "ElementHandle | None") -> bool:
    """True if the coupon element is flagged as verified."""
    for sel in _VERIFIED_SELECTORS:
        if elem.select_one(sel):
            return True
    return "verified" in elem.get_text().lower()


def find_coupon_elements(soup: "BeautifulSoup", selectors: List[str]) -> list:
    """Return the first non-empty selector match, or a heuristic class scan."""
    for sel in selectors:
        elements = soup.select(sel)
        if elements:
            logger.debug("Found %d elements with selector %s", len(elements), sel)
            return elements
    return soup.find_all(
        ["div", "li", "article"],
        class_=lambda x: x and any(
            k in x.lower() for k in ("coupon", "offer", "promo", "code")
        ),
    )


def _resolve_code(elem: Any) -> str:
    """Pull the coupon code text from a card, falling back to a button value."""
    code = first_text(elem, _CODE_SELECTORS)
    if code:
        return code
    btn = elem.find("button") or elem.find("input")
    if not btn:
        return ""
    return btn.get("value", "") or btn.get_text(strip=True)


def _build_record(elem: Any, source: str) -> Optional[Dict[str, Any]]:
    """Map one card element to a coupon dict, or ``None`` if unusable."""
    try:
        code = _resolve_code(elem)
        discount = first_text(elem, _DISCOUNT_SELECTORS)
        description = first_text(elem, _DESCRIPTION_SELECTORS)
        expires = first_text(elem, _EXPIRES_SELECTORS) or None
        if not (code or discount):
            return None
        return {
            "code": code,
            "discount": discount or description,
            "description": description or discount,
            "expires": expires,
            "source": source,
            "verified": is_verified(elem),
        }
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug(
            "Error parsing coupon element (%s, %s): %s",
            source,
            type(exc).__name__,
            exc,
        )
        return None


def parse_coupon_elements(elements: list, source: str) -> List[Dict[str, Any]]:
    """Apply ``_build_record`` to each candidate, dropping non-records."""
    results: List[Dict[str, Any]] = []
    for elem in elements:
        record = _build_record(elem, source)
        if record is not None:
            results.append(record)
    return results


@dataclass(frozen=True)
class CouponSiteSpec:
    """Describes a static-HTML coupon site for the shared scraper pipeline."""

    source: str
    url_template: str
    card_selectors: List[str]
    timeout_s: int
    user_agent: str


def build_request_headers(user_agent: str) -> Dict[str, str]:
    """Return a default header bag for a coupon-site GET."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    }


def scrape_coupon_site(
    spec: CouponSiteSpec,
    merchant: str,
) -> List[Dict[str, Any]]:
    """Shared HTML coupon scrape -- previously duplicated across two sites.

    Returns ``[]`` on any HTTP / parse failure (these are non-fatal data-source
    misses, not application errors).
    """
    if not merchant:
        return []
    url = spec.url_template % merchant.lower().strip()
    headers = build_request_headers(spec.user_agent)
    try:
        resp = requests.get(url, headers=headers, timeout=spec.timeout_s)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        elements = find_coupon_elements(soup, spec.card_selectors)
        results = parse_coupon_elements(elements, spec.source)
    except _REQUESTS_EXCEPTIONS as exc:
        logger.warning(
            "%s scrape failed for %s (%s): %s",
            spec.source,
            merchant,
            type(exc).__name__,
            exc,
        )
        return []
    except PARSE_EXCEPTIONS as exc:
        logger.warning(
            "%s parse failed for %s (%s): %s",
            spec.source,
            merchant,
            type(exc).__name__,
            exc,
        )
        return []
    logger.info("%s: %d coupons for %s", spec.source, len(results), merchant)
    return results


def deduplicate_promos(
    raw_promos: List[Dict[str, Any]],
    sort_key: Callable[[Dict[str, Any]], Any],
) -> List[Dict[str, Any]]:
    """Drop duplicate codes (case-insensitive) and apply a sort key."""
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for coupon in raw_promos:
        code = coupon.get("code", "").upper().strip()
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        unique.append(coupon)
    unique.sort(key=sort_key)
    return unique


def _safe_run_fetcher(
    merchant: str,
    fetcher: Callable[[str], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Run one fetcher; return [] on recoverable scrape failure."""
    try:
        return fetcher(merchant)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.warning(
            "Aggregator fetcher %s failed (%s): %s",
            getattr(fetcher, "__name__", "fetcher"),
            type(exc).__name__,
            exc,
        )
        # explicit recovery: SCRAPE_FAILURE_EXCEPTIONS non-fatal; one fetcher cannot poison aggregation
        return []


def safe_aggregate_promos(merchant: str, fetchers: List[Callable[[str], List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """Run each fetcher; suppress narrow scrape failures so one site never poisons the rest."""
    aggregated: List[Dict[str, Any]] = []
    for fetcher in fetchers:
        aggregated.extend(_safe_run_fetcher(merchant, fetcher))
    return aggregated
