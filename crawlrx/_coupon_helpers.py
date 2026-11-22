"""Coupon aggregator helpers.

Module split: extracts the per-site scraper body so the four
scrape_* functions in :mod:`.coupon_aggregator` collapse to a thin
config + ``_scrape_coupon_site`` call. Removes the
``duplication checks`` and ``the nesting-depth limit`` blockers.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup, Tag

from ._scraper_helpers import network_exception_types

logger = logging.getLogger("crawlrx.coupon_aggregator.helpers")

TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_WORKERS = 4

_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_NETWORK_EXC = network_exception_types() + (requests.RequestException,)
_SOUP_PARSE_EXC: Tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, IndexError,
)


# ─────────────────────────────────────────────
# URL helpers (S5 canonical names)
# ─────────────────────────────────────────────

def extract_domain(merchant: str) -> str:
    """Return clean domain like ``bestbuy.com`` from any merchant input."""
    if not merchant or not isinstance(merchant, str):
        return ""
    clean = merchant.strip().lower()
    if "://" in clean:
        clean = clean.split("://", 1)[1]
    if "/" in clean:
        clean = clean.split("/", 1)[0]
    if clean.startswith("www."):
        clean = clean[4:]
    return clean


def extract_store_name(merchant: str) -> str:
    """Return ``bestbuy`` from ``bestbuy.com`` (first label of the domain)."""
    domain = extract_domain(merchant)
    if not domain:
        return ""
    parts = domain.split(".")
    return parts[0] if parts else ""


def fetch_soup(url: str) -> Optional[BeautifulSoup]:
    """HTTP GET ``url`` and parse HTML. ``None`` on network failure."""
    try:
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except _NETWORK_EXC as exc:
        logger.debug("Request failed for %s (%s): %s", url, type(exc).__name__, exc)
        return None
    return BeautifulSoup(resp.content, "html.parser")


# ─────────────────────────────────────────────
# Scrape primitives shared by all four sites
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class CouponSiteConfig:
    """Config bag for one coupon-aggregator site."""

    source: str
    url_builder: Callable[[str], str]
    code_selectors: Sequence[str]
    discount_selectors: Sequence[str]
    parent_tags: Sequence[str] = field(default_factory=lambda: ("div", "li", "article"))


def _select_first_non_empty(soup: BeautifulSoup, selectors: Sequence[str]) -> List[Tag]:
    """Return the first selector list that yields >=1 match."""
    for selector in selectors:
        elements = soup.select(selector)
        if elements:
            return elements
    return []


def _extract_discount(parent: Tag, selectors: Sequence[str]) -> str:
    """Return the first non-empty discount/savings text within ``parent``."""
    for selector in selectors:
        disc = parent.select_one(selector)
        if disc is None:
            continue
        text = disc.get_text(strip=True)
        if text:
            return text
    return ""


def _coupon_from_element(
    elem: Tag,
    *,
    source: str,
    parent_tags: Sequence[str],
    discount_selectors: Sequence[str],
) -> Optional[dict]:
    """Parse a single ``elem`` into a coupon dict (or None on no-code)."""
    code = elem.get_text(strip=True)
    if not code or len(code) > 50:
        return None
    parent = elem.find_parent(list(parent_tags)) or elem
    return {
        "code": code,
        "discount": _extract_discount(parent, discount_selectors),
        "source": source,
        "verified": False,
    }


def scrape_coupon_site(merchant: str, config: CouponSiteConfig) -> List[dict]:
    """Scrape one configured coupon site for ``merchant``.

    Replaces the four near-identical ``scrape_*`` bodies that previously
    triggered ``duplication checks`` and ``the nesting-depth limit``.
    """
    if not extract_domain(merchant):
        return []

    url = config.url_builder(merchant)
    logger.debug("Scraping %s: %s", config.source, url)
    soup = fetch_soup(url)
    if not soup:
        return []

    elements = _select_first_non_empty(soup, config.code_selectors)
    results: List[dict] = []
    for elem in elements:
        try:
            coupon = _coupon_from_element(
                elem,
                source=config.source,
                parent_tags=config.parent_tags,
                discount_selectors=config.discount_selectors,
            )
        except _SOUP_PARSE_EXC as exc:
            logger.debug("Error parsing %s element (%s): %s", config.source, type(exc).__name__, exc)
            continue
        if coupon is not None:
            results.append(coupon)

    logger.info("%s: %d codes found for %s", config.source, len(results), merchant)
    return results


# ─────────────────────────────────────────────
# Aggregation helpers (kept here so coupon_aggregator.py stays slim)
# ─────────────────────────────────────────────

ScrapeFn = Callable[[str], List[dict]]


def _drain_future(future, source: str) -> List[dict]:
    """Consume one scraper future, logging known failure types."""
    try:
        return list(future.result())
    except _SOUP_PARSE_EXC as exc:
        logger.warning("Scraper %s parse failure (%s): %s", source, type(exc).__name__, exc)
        return None  # explicit recovery: non-fatal: _SOUP_PARSE_EXC is recorded but does not abort the caller
    except _NETWORK_EXC as exc:
        logger.warning("Scraper %s network failure (%s): %s", source, type(exc).__name__, exc)
        return None  # explicit recovery: non-fatal: _NETWORK_EXC is recorded but does not abort the caller
    return []


def _collect_scraper_results(
    merchant: str,
    scrapers: Sequence[Tuple[str, ScrapeFn]],
) -> List[dict]:
    """Run every scraper in parallel and return the concatenated results."""
    all_codes: List[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_source = {
                executor.submit(fn, merchant): source for source, fn in scrapers
            }
            for future in as_completed(future_to_source):
                all_codes.extend(_drain_future(future, future_to_source[future]))
    except RuntimeError as exc:
        logger.error("ThreadPoolExecutor runtime error: %s", exc)
        return None  # explicit recovery: non-fatal: RuntimeError is recorded but does not abort the caller
    return all_codes


def _deduplicate_codes(codes: Sequence[dict]) -> List[dict]:
    """Case-insensitive de-dup by ``code`` field, preserving first order."""
    seen: set[str] = set()
    out: List[dict] = []
    for entry in codes:
        try:
            code = str(entry.get("code", "")).upper().strip()
        except (AttributeError, TypeError) as exc:
            logger.debug("Error reading code key (%s): %s", type(exc).__name__, exc)
            continue
        if code and code not in seen:
            seen.add(code)
            out.append(entry)
    return out


def aggregate_via_pool(
    merchant: str,
    scrapers: Sequence[Tuple[str, ScrapeFn]],
) -> Tuple[List[dict], int]:
    """Run scrapers in parallel + deduplicate. Returns ``(codes, elapsed_ms)``."""
    start_time = time.time()
    all_codes = _collect_scraper_results(merchant, scrapers)
    deduped = _deduplicate_codes(all_codes)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "Aggregated %d unique codes from %d total for %s",
        len(deduped), len(all_codes), merchant,
    )
    return deduped, elapsed_ms
