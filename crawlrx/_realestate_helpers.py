"""Shared helpers for real-estate family scrapers (module split).

Absorbs duplicated listing-card / detail-page extraction patterns across
``redfin.py``, ``zillow.py``, ``trulia.py``, ``realtor.py`` and
``apartments.py``. Every helper here:

* Has a single, narrow responsibility.
* Has type hints on every signature.
* Catches only the explicit exception tuple it was written to swallow
  -- never a bare ``Exception`` (the no-broad-except rule enforced at zero).
* Documents the contract in one short docstring; no prose bloat.

Conventions
-----------
``RealEstateSearchParams`` -- bundles the six positional args that every
scraper's ``_do_search_*`` previously took (location, listing_type, four
filter args, limit). Removes ``the parameter-count limit`` blockers without
changing the public router signature.

``CAPTCHA_INDICATORS`` -- canonical list of substrings that mean the
target site is blocking us. Used by every ``_check_captcha_blocked``
helper across the family.
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    PARSE_EXCEPTIONS,
)


# ---------------------------------------------------------------------------
# Captcha detection (shared across the family)
# ---------------------------------------------------------------------------

CAPTCHA_INDICATORS: tuple[str, ...] = (
    "press & hold",
    "captcha",
    "robot",
    "access denied",
    "blocked",
)


def opt_value(data: dict, key: str) -> Optional[Any]:
    """``data[key]`` if truthy, else ``None`` (JS payloads use "" for missing)."""
    return data.get(key) or None


async def is_captcha_blocked(page: "Page", timeout_ms: int = 5000) -> bool:
    """True if the loaded page shows captcha / bot-block markers."""
    try:
        body_text = await page.inner_text("body", timeout=timeout_ms)
    except BROWSER_SCRAPE_EXCEPTIONS:
        return False
    if not isinstance(body_text, str):
        return False
    lowered = body_text.lower()
    return any(token in lowered for token in CAPTCHA_INDICATORS)


# ---------------------------------------------------------------------------
# Random-delay / scroll helpers (shared across stealth scrapers)
# ---------------------------------------------------------------------------

async def random_delay_ms(min_ms: int = 800, max_ms: int = 2000) -> None:
    """Sleep for a random millisecond duration in ``[min_ms, max_ms]``."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


@dataclass(frozen=True)
class ScrollPattern:
    """Random-scroll envelope -- bundles 4-tuple to satisfy param_count."""

    iterations: int = 4
    min_px: int = 200
    max_px: int = 500
    min_pause_s: float = 0.4
    max_pause_s: float = 0.8


_DEFAULT_SCROLL_PATTERN = ScrollPattern()


async def scroll_page_randomly(
    page: "Page",
    pattern: ScrollPattern = _DEFAULT_SCROLL_PATTERN,
) -> None:
    """Scroll a Playwright page in randomised chunks to fake human use."""
    for _ in range(max(1, pattern.iterations)):
        scroll_amount = random.randint(pattern.min_px, pattern.max_px)
        try:
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        except BROWSER_SCRAPE_EXCEPTIONS:
            return
        await asyncio.sleep(
            random.uniform(pattern.min_pause_s, pattern.max_pause_s)
        )


# ---------------------------------------------------------------------------
# Numeric extractors (re-exported as the canonical real-estate set)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"([\d,]+)")
_FLOAT_RE = re.compile(r"([\d.]+)")
_DIGITS_ONLY_RE = re.compile(r"\d+")


def extract_int(text: Optional[str]) -> Optional[int]:
    """First comma-tolerant integer in ``text`` -- ``None`` if missing."""
    if not text:
        return None
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, OverflowError):
        return None


def extract_float(text: Optional[str]) -> Optional[float]:
    """First float in ``text`` -- ``None`` if missing or unparseable."""
    if not text:
        return None
    match = _FLOAT_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def extract_price_int(price_text: Optional[str]) -> Optional[int]:
    """Strip non-digits then parse as int (``$450,000`` -> ``450000``)."""
    if not price_text:
        return None
    digits = re.sub(r"[^\d]", "", price_text)
    if not digits:
        return None
    try:
        return int(digits)
    except (ValueError, OverflowError):
        return None


def parse_year(text: Optional[str]) -> Optional[int]:
    """First 4-digit year in ``text`` (``1900-2099`` would already match)."""
    if not text:
        return None
    match = re.search(r"(\d{4})", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def absolute_url(href: Optional[str], base_url: str) -> str:
    """Resolve a possibly-relative ``href`` against ``base_url``."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return base_url + href


# ---------------------------------------------------------------------------
# Param bundles (shrink positional surface to satisfy the parameter-count limit)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RealEstateSearchParams:
    """Bundled args for every ``_do_search_*`` real-estate scraper.

    Replaces the 6-7 positional arg explosion (location, listing_type,
    min_price, max_price, min_beds, min_baths, limit) flagged by
    ``the parameter-count limit``.
    """

    location: str
    listing_type: str = "for_sale"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    min_baths: Optional[float] = None
    limit: int = 20


@dataclass(frozen=True)
class ApartmentsSearchParams:
    """Apartments.com filters bundle (rent-specific surface)."""

    location: str
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    pet_friendly: Optional[bool] = None
    limit: int = 20


@dataclass(frozen=True)
class SearchUrlSpec:
    """URL-builder bundle for ``_build_search_url`` family helpers."""

    location: str
    listing_type: str = "for_sale"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    min_baths: Optional[float] = None


# ---------------------------------------------------------------------------
# Selector iteration (the "try selectors in order, use first hit" pattern)
# ---------------------------------------------------------------------------

async def _query_selector_safe(card: Any, selector: str) -> Any:
    """``card.query_selector`` that returns ``None`` on extraction errors."""
    try:
        return await card.query_selector(selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return None


async def _element_inner_text(element: Any) -> Optional[str]:
    """Stripped ``inner_text`` of an element, ``None`` on missing / error."""
    if element is None:
        return None
    try:
        text = await element.inner_text()
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return None
    if not text:
        return None
    stripped = text.strip()
    return stripped or None


async def _element_attribute(element: Any, attr_name: str) -> Optional[str]:
    """Read one attribute, ``None`` on missing / error / empty."""
    if element is None:
        return None
    try:
        value = await element.get_attribute(attr_name)
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return None
    return value if value else None


async def first_inner_text(card: "ElementHandle", selectors: Sequence[str]) -> Optional[str]:
    """Return ``inner_text`` of the first selector that matches ``card``."""
    for sel in selectors:
        element = await _query_selector_safe(card, sel)
        text = await _element_inner_text(element)
        if text is not None:
            return text
    return None


async def first_attribute(
    card: "ElementHandle",
    selectors: Sequence[str],
    *attribute_names: str,
) -> Optional[str]:
    """Return first non-empty attribute value across ``selectors`` / attrs."""
    for sel in selectors:
        element = await _query_selector_safe(card, sel)
        if element is None:
            continue
        for attr in attribute_names:
            value = await _element_attribute(element, attr)
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# JS-payload row helpers (used by zillow / trulia / realtor)
# ---------------------------------------------------------------------------

def _unwrap_js_payload(value: Any) -> Any:
    """Unwrap a JS regex-match payload to the inner scalar (or ``None``)."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _try_int(text: str) -> Optional[int]:
    """Best-effort int parse of a digit string, ``None`` on failure."""
    try:
        return int(text)
    except (ValueError, OverflowError):
        return None


def parse_js_int(value: object) -> Optional[int]:
    """Coerce a JS regex-match payload (``["12"]`` / ``"12"`` / ``None``)."""
    inner = _unwrap_js_payload(value)
    if inner is None:
        return None
    if isinstance(inner, bool):
        return None
    if isinstance(inner, int):
        return inner
    if not isinstance(inner, str):
        return None
    match = _DIGITS_ONLY_RE.search(inner.replace(",", ""))
    return _try_int(match.group(0)) if match else None


def parse_js_float(value: object) -> Optional[float]:
    """JS ``.match(/[\\d.]+/)`` payload -> ``float`` or ``None``."""
    inner = _unwrap_js_payload(value)
    if inner is None or isinstance(inner, bool):
        return None
    if isinstance(inner, (int, float)):
        return float(inner)
    if not isinstance(inner, str):
        return None
    match = _FLOAT_RE.search(inner)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_js_int_with_commas(value: object) -> Optional[int]:
    """Same as :func:`parse_js_int` but tolerates comma-formatted numbers."""
    inner = _unwrap_js_payload(value)
    if inner is None or isinstance(inner, bool):
        return None
    if isinstance(inner, int):
        return inner
    if not isinstance(inner, str):
        return None
    cleaned = inner.replace(",", "")
    direct = _try_int(cleaned)
    if direct is not None:
        return direct
    match = _DIGITS_ONLY_RE.search(cleaned)
    return _try_int(match.group(0)) if match else None


def or_none(value: object) -> object:
    """Return ``None`` for falsy values, the value itself otherwise."""
    return value if value else None


# ---------------------------------------------------------------------------
# User-agent set (deduplicated across realtor / apartments / target)
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
)


@dataclass(frozen=True)
class StealthNavSpec:
    """Navigation request bundle for :func:`stealth_navigate`."""

    url: str
    timeout_ms: int = 45000
    wait_until: str = "domcontentloaded"


__all__ = [
    "CAPTCHA_INDICATORS",
    "is_captcha_blocked",
    "opt_value",
    "random_delay_ms",
    "scroll_page_randomly",
    "ScrollPattern",
    "StealthNavSpec",
    "extract_int",
    "extract_float",
    "extract_price_int",
    "parse_year",
    "absolute_url",
    "RealEstateSearchParams",
    "ApartmentsSearchParams",
    "SearchUrlSpec",
    "first_inner_text",
    "first_attribute",
    "parse_js_int",
    "parse_js_float",
    "parse_js_int_with_commas",
    "or_none",
    "DEFAULT_USER_AGENTS",
]
