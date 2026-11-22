"""Airbnb listing-detail field extractors.

Pulled out of :mod:`._airbnb_helpers` so each module stays under the 500
line FILE_TOO_LARGE threshold. The card-level extractors + URL builder
live in ``_airbnb_helpers``; this module concentrates on Playwright Page
level parsers used only by ``_get_listing_detail``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Playwright


from playwright.async_api import Page

from ._airbnb_helpers import (
    BROWSER_EXC,
    PROBE_EXC,
    detect_property_type,
    random_delay,
)

logger = logging.getLogger("crawlrx.airbnb.detail")


async def _safe_query(page: Page, selector: str) -> Any:
    """``page.query_selector`` that swallows known probe failures."""
    try:
        return await page.query_selector(selector)
    except PROBE_EXC as exc:
        logger.debug(
            "query_selector(%s) failed (%s): %s",
            selector, type(exc).__name__, exc,
        )
        return None


async def _inner_text_or_none(element: Any) -> Optional[str]:
    """Best-effort ``inner_text().strip()`` returning None on failure."""
    if element is None:
        return None
    try:
        text = await element.inner_text()
    except PROBE_EXC as exc:
        logger.debug("inner_text failed (%s): %s", type(exc).__name__, exc)
        return None
    return text.strip() if text else None


async def safe_first_text(page: Page, selectors: Sequence[str]) -> Optional[str]:
    """First non-empty ``inner_text`` among ``selectors``."""
    for selector in selectors:
        element = await _safe_query(page, selector)
        text = await _inner_text_or_none(element)
        if text:
            return text
    return None


async def safe_first_match(page: Page, selectors: Sequence[str]) -> "ElementHandle | None":
    """First non-None element among ``selectors``."""
    for selector in selectors:
        element = await _safe_query(page, selector)
        if element is not None:
            return element
    return None


# ─────────────────────────────────────────────
# Room details
# ─────────────────────────────────────────────

@dataclass
class RoomDetails:
    """Numeric counts parsed off the listing subtitle."""

    guests: Optional[int] = None
    bedrooms: Optional[int] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    property_type: Optional[str] = None


_ROOM_PATTERNS: Tuple[Tuple[str, str, type], ...] = (
    ("guests", r"(\d+)\s+guest", int),
    ("bedrooms", r"(\d+)\s+bedroom", int),
    ("beds", r"(\d+)\s+bed", int),
    ("baths", r"([\d.]+)\s+bath", float),
)


def parse_room_details(room_text: str) -> RoomDetails:
    """Parse guest/bedroom/bed/bath counts + property type from subtitle text."""
    out = RoomDetails(property_type=detect_property_type(room_text))
    for attr, pattern, caster in _ROOM_PATTERNS:
        match = re.search(pattern, room_text, re.I)
        if match is None:
            continue
        try:
            setattr(out, attr, caster(match.group(1)))
        except ValueError:
            continue
    return out


# ─────────────────────────────────────────────
# Amenities + house rules
# ─────────────────────────────────────────────

async def collect_amenities(page: Page, *, limit: int = 30) -> List[str]:
    """Click the show-all toggle, then collect distinct amenity strings."""
    toggle = await _safe_query(
        page,
        "button:has-text('Show all'), button:has-text('amenities')",
    )
    if toggle is not None:
        try:
            await toggle.click()
            await random_delay(500, 1000)
        except BROWSER_EXC as exc:
            logger.debug("amenity toggle click failed (%s): %s", type(exc).__name__, exc)

    elements = await page.query_selector_all(
        "[class*='amenity'], [data-testid*='amenity']"
    )
    amenities: List[str] = []
    for element in elements[:limit]:
        text = await _inner_text_or_none(element)
        if text and len(text) < 100 and text not in amenities:
            amenities.append(text)
    return amenities


async def collect_house_rules(page: Page) -> List[str]:
    """Pull every house-rule string off the listing page."""
    elements = await page.query_selector_all(
        "[class*='house-rule'], [data-testid*='house-rule']"
    )
    rules: List[str] = []
    for element in elements:
        text = await _inner_text_or_none(element)
        if text:
            rules.append(text)
    return rules


# ─────────────────────────────────────────────
# Reviews
# ─────────────────────────────────────────────

@dataclass
class ReviewBlock:
    """Single review block extracted off the listing page."""

    author: Optional[str] = None
    date: Optional[str] = None
    text: Optional[str] = None


async def _review_field(card: Any, selectors: Sequence[str]) -> Optional[str]:
    """First non-empty inner_text from selectors inside a review card."""
    for selector in selectors:
        element = await card.query_selector(selector)
        text = await _inner_text_or_none(element)
        if text:
            return text
    return None


async def collect_reviews(page: Page, *, limit: int = 5) -> List[ReviewBlock]:
    """Pull up to ``limit`` review blocks off the listing page."""
    cards = await page.query_selector_all("[data-testid='review-card']")
    out: List[ReviewBlock] = []
    for card in cards[:limit]:
        try:
            out.append(ReviewBlock(
                author=await _review_field(card, ("[class*='author']", "h3", "h4")),
                date=await _review_field(card, ("[class*='date']",)),
                text=await _review_field(card, ("[class*='text']", "span")),
            ))
        except PROBE_EXC as exc:
            logger.debug("Suppressed review parse error (%s): %s", type(exc).__name__, exc)
    return out


# ─────────────────────────────────────────────
# Host fields
# ─────────────────────────────────────────────

@dataclass
class HostFields:
    """Host fields collected off the listing detail page."""

    name: Optional[str] = None
    is_superhost: bool = False
    response_rate: Optional[str] = None
    response_time: Optional[str] = None


async def _host_response_metrics(
    host_section: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Parse response_rate + response_time strings from the host section."""
    response_rate: Optional[str] = None
    response_time: Optional[str] = None
    for el in await host_section.query_selector_all("[class*='response']"):
        text = await _inner_text_or_none(el)
        if text is None:
            continue
        lower = text.lower()
        if "response rate" in lower:
            response_rate = text
        elif "response time" in lower:
            response_time = text
    return response_rate, response_time


async def collect_host_fields(page: Page) -> HostFields:
    """Pull host name + superhost flag + response metrics."""
    host_section = await safe_first_match(
        page,
        (
            "[data-section-id='HOST_OVERVIEW']",
            "[data-testid='host-profile']",
        ),
    )
    if host_section is None:
        return HostFields()
    name_el = await host_section.query_selector("h2, h3")
    badge_el = await host_section.query_selector("[class*='superhost']")
    rate, time_str = await _host_response_metrics(host_section)
    return HostFields(
        name=await _inner_text_or_none(name_el),
        is_superhost=badge_el is not None,
        response_rate=rate,
        response_time=time_str,
    )


# ─────────────────────────────────────────────
# Photos + price + cancellation
# ─────────────────────────────────────────────

async def count_photos(page: Page) -> int:
    """Number of <img> elements inside Airbnb's photo galleries."""
    photos = await page.query_selector_all(
        "[data-testid='photo'] img, [class*='photo'] img"
    )
    return len(photos)


async def parse_detail_price(page: Page) -> Optional[str]:
    """First ``$N.NN`` slice off the listing's book-it/price block."""
    el = await safe_first_match(
        page,
        ("[data-testid='book-it-default']", "[class*='price'] [class*='price']"),
    )
    text = await _inner_text_or_none(el)
    if text is None:
        return None
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    return match.group(0) if match else None


async def parse_cancellation_policy(page: Page) -> Optional[str]:
    """Walk a couple of parents up from the cancellation marker for context."""
    cancel_el = await _safe_query(page, "text=/cancellation/i")
    if cancel_el is None:
        return None
    parent = cancel_el
    for _ in range(2):
        parent = await _safe_query_via_xpath(parent)
        if parent is None:
            return None
        text = await _inner_text_or_none(parent)
        if text and len(text) > 20:
            return text
    return None


async def _safe_query_via_xpath(parent: Any) -> Any:
    """Walk one ancestor up, swallowing probe failures."""
    try:
        return await parent.query_selector("..")
    except PROBE_EXC as exc:
        logger.debug("ancestor walk failed (%s): %s", type(exc).__name__, exc)
        return None
