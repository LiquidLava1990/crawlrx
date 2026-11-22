"""Airbnb scraper helpers extracted from ``airbnb.py``.

Module split: stealth Playwright launch, search URL builder and
card-level field extractors live here. Listing-detail field extractors
live in :mod:`._airbnb_detail` so each module stays under FILE_TOO_LARGE.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Playwright


from playwright.async_api import Page

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.airbnb.helpers")

AIRBNB_BASE_URL = "https://www.airbnb.com"

CARD_SELECTORS: Tuple[str, ...] = (
    "[data-testid='card-container']",
    "[itemprop='itemListElement']",
    "[data-testid='listing-card']",
    "a[href^='/rooms/']",
)

PROPERTY_TYPES: Tuple[str, ...] = (
    "Entire home",
    "Private room",
    "Shared room",
    "Entire condo",
    "Entire apartment",
)

PROBE_EXC: Tuple[type[BaseException], ...] = SELECTOR_EXTRACTION_EXCEPTIONS
BROWSER_EXC: Tuple[type[BaseException], ...] = PLAYWRIGHT_EXCEPTIONS


# ─────────────────────────────────────────────
# Listing-id + small parsers
# ─────────────────────────────────────────────

_ID_PATTERNS: Tuple[str, ...] = (
    r"/rooms/(\d+)",
    r"/listing/(\d+)",
    r"airbnb\.com/rooms/(\d+)",
)


def extract_listing_id(url: str) -> Optional[str]:
    """Return the numeric Airbnb listing id (or None)."""
    for pattern in _ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_rating(rating_text: Optional[str]) -> Optional[float]:
    """Return the first valid rating in ``[0, 5]`` from ``rating_text``."""
    if not rating_text:
        return None
    match = re.search(r"([\d.]+)", rating_text)
    if match is None:
        return None
    try:
        rating = float(match.group(1))
    except ValueError:
        return None
    return rating if 0 <= rating <= 5 else None


def parse_review_count(review_text: Optional[str]) -> Optional[int]:
    """Return the first integer in a parenthesised review-count string."""
    if not review_text:
        return None
    match = re.search(r"[(\s]([\d,]+)[)\s]", review_text)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def detect_property_type(text: Optional[str]) -> Optional[str]:
    """Return the first canonical property type substring found in ``text``."""
    if not text:
        return None
    lower = text.lower()
    for property_type in PROPERTY_TYPES:
        if property_type.lower() in lower:
            return property_type
    return None


# ─────────────────────────────────────────────
# Search URL builder
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class SearchUrlSpec:
    """Param bag for the search URL builder."""

    location: str
    checkin: Optional[str] = None
    checkout: Optional[str] = None
    guests: int = 1
    min_price: Optional[int] = None
    max_price: Optional[int] = None


def build_search_url(spec: SearchUrlSpec) -> str:
    """Build an Airbnb /s/<location>/homes URL with optional filters."""
    import urllib.parse

    location_encoded = urllib.parse.quote_plus(spec.location)
    base = f"{AIRBNB_BASE_URL}/s/{location_encoded}/homes"
    params: List[str] = []
    if spec.checkin:
        params.append(f"checkin={spec.checkin}")
    if spec.checkout:
        params.append(f"checkout={spec.checkout}")
    if spec.guests:
        params.append(f"adults={spec.guests}")
    if spec.min_price:
        params.append(f"price_min={spec.min_price}")
    if spec.max_price:
        params.append(f"price_max={spec.max_price}")
    if not params:
        return base
    return f"{base}?{'&'.join(params)}"


# ─────────────────────────────────────────────
# Stealth helpers
# ─────────────────────────────────────────────

async def random_delay(min_ms: int = 1000, max_ms: int = 2500) -> None:
    """Sleep for a random duration in ``[min_ms, max_ms]`` milliseconds."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def launch_stealth_browser(playwright_obj: "Playwright") -> Tuple[Any, Any, Page]:
    """Open a stealth Chromium context, return ``(browser, ctx, page)``."""
    browser = await playwright_obj.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    contexts = await browser.contexts
    ctx = contexts[0] if contexts else await browser.new_context()
    page = await ctx.new_page()
    return browser, ctx, page


async def close_browser_chain(*objects: 'Page | BrowserContext | Browser | None') -> None:
    """Close every non-None object, ignoring routine teardown errors."""
    for obj in objects:
        if obj is None:
            continue
        try:
            await obj.close()
        except BROWSER_EXC as exc:
            logger.debug(
                "Cleanup failed (non-critical, %s): %s",
                type(exc).__name__,
                exc,
            )


# ─────────────────────────────────────────────
# Card-level extractors
# ─────────────────────────────────────────────

@dataclass
class CardFields:
    """Flat field record extracted from a single search card."""

    listing_id: str
    href: str
    title: str
    price_per_night: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    location: Optional[str]
    image_url: Optional[str]
    superhost: bool
    property_type: Optional[str]


async def _query_first(card: Any, selectors: Sequence[str]) -> Any:
    """Return the first non-None ``query_selector`` match within ``card``."""
    for selector in selectors:
        element = await card.query_selector(selector)
        if element is not None:
            return element
    return None


async def _inner_text_or_none(element: Any) -> Optional[str]:
    """Best-effort `inner_text().strip()` returning None on failure."""
    if element is None:
        return None
    try:
        text = await element.inner_text()
    except PROBE_EXC as exc:
        logger.debug("inner_text failed (%s): %s", type(exc).__name__, exc)
        return None
    return text.strip() if text else None


async def _attr_or_none(element: Any, attr: str) -> Optional[str]:
    """Best-effort ``get_attribute`` returning None on failure or missing."""
    if element is None:
        return None
    try:
        value = await element.get_attribute(attr)
    except PROBE_EXC as exc:
        logger.debug("get_attribute failed (%s): %s", type(exc).__name__, exc)
        return None
    return value


async def _card_link_href(card: Any) -> str:
    """Return the listing href for a search-result card."""
    link = await card.query_selector("a[href^='/rooms/']") or card
    href = await _attr_or_none(link, "href")
    return href or ""


async def _card_price(card: Any) -> Optional[str]:
    """Return the ``$N.NN`` slice of the card's price element."""
    price_el = await _query_first(
        card,
        ("[data-testid='price-and-total']", "[class*='price']"),
    )
    text = await _inner_text_or_none(price_el)
    if text is None:
        return None
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    return match.group(0) if match else None


async def _card_rating_pair(card: Any) -> Tuple[Optional[float], Optional[int]]:
    """Return ``(rating, review_count)`` parsed from the card rating block."""
    rating_el = await _query_first(
        card, ("[data-testid='rating']", "[class*='rating']"),
    )
    rating_text = await _inner_text_or_none(rating_el)
    return parse_rating(rating_text), parse_review_count(rating_text)


async def _card_image_url(card: Any) -> Optional[str]:
    """First image src on the card."""
    img = await card.query_selector("img")
    return await _attr_or_none(img, "src") if img is not None else None


async def _card_is_superhost(card: Any) -> bool:
    """True if the card carries any superhost-styled element."""
    badge = await card.query_selector(
        "[class*='superhost'], [data-testid='superhost-badge']"
    )
    return badge is not None


async def card_to_fields(card: "ElementHandle") -> Optional[CardFields]:
    """Pull every fielded value off one card; None if it has no listing id."""
    href = await _card_link_href(card)
    listing_id = extract_listing_id(href)
    if not listing_id:
        return None

    title_el = await _query_first(
        card,
        (
            "[data-testid='listing-card-title']",
            "[id*='title']",
            "h2, h3",
        ),
    )
    title = await _inner_text_or_none(title_el) or ""
    rating, review_count = await _card_rating_pair(card)
    location_el = await _query_first(
        card,
        ("[data-testid='listing-card-subtitle']", "[class*='location']"),
    )

    return CardFields(
        listing_id=listing_id,
        href=href,
        title=title,
        price_per_night=await _card_price(card),
        rating=rating,
        review_count=review_count,
        location=await _inner_text_or_none(location_el),
        image_url=await _card_image_url(card),
        superhost=await _card_is_superhost(card),
        property_type=detect_property_type(title),
    )


def absolute_listing_url(href: str) -> str:
    """Make a fully-qualified Airbnb listing URL from a relative href."""
    if not href:
        return ""
    return f"{AIRBNB_BASE_URL}{href}" if href.startswith("/") else href
