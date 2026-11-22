"""Card-extraction helpers for ``kayak.py`` (split out for size limit)."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.kayak.extract")

KAYAK_BASE_URL = "https://www.kayak.com"

# ---------------------------------------------------------------------------
# Generic per-card probes (selector lookup + safe inner_text)
# ---------------------------------------------------------------------------

async def query_first(card: "ElementHandle", selectors: tuple[str, ...]) -> "ElementHandle | None":
    """First non-None ``query_selector`` match across ``selectors``."""
    for sel in selectors:
        el = await card.query_selector(sel)
        if el is not None:
            return el
    return None


async def safe_inner_text(el: "ElementHandle | None") -> str:
    """Best-effort ``el.inner_text().strip()`` (returns ``""`` when None)."""
    if el is None:
        return ""
    return (await el.inner_text()).strip()


async def resolve_link(card: "ElementHandle") -> Optional[str]:
    """Pull and absolutize the first ``<a href>`` URL on a card."""
    link_el = await card.query_selector("a[href]")
    if link_el is None:
        return None
    href = await link_el.get_attribute("href")
    if not href:
        return None
    return href if href.startswith("http") else f"{KAYAK_BASE_URL}{href}"


def parse_price(price_text: Optional[str]) -> Optional[str]:
    """Extract price from text."""
    if not price_text:
        return None
    match = re.search(r'\$[\d,]+', price_text)
    return match.group(0) if match else price_text.strip()


# ---------------------------------------------------------------------------
# Flight-card extraction
# ---------------------------------------------------------------------------

FLIGHT_AIRLINE_SELECTORS = (
    "[data-testid='flight-operator']", ".airline-name",
    "img[class*='airline']", "[class*='carrier']",
)
FLIGHT_PRICE_SELECTORS = (
    "[data-testid='flight-price']", ".price", "[class*='price']",
)
FLIGHT_DURATION_SELECTORS = (
    "[data-testid='flight-duration']", ".duration", "[class*='duration']",
)
FLIGHT_STOPS_SELECTORS = (
    "[data-testid='flight-stops']", ".stops", "[class*='stop']",
)


async def flight_airline(card: "ElementHandle") -> str:
    """Pull airline name from a flight card (alt-attr or inner text)."""
    el = await query_first(card, FLIGHT_AIRLINE_SELECTORS)
    if el is None:
        return "Unknown"
    alt = await el.get_attribute("alt")
    if alt:
        return alt
    text = await safe_inner_text(el)
    return text or "Unknown"


async def flight_times(card: "ElementHandle") -> tuple[Optional[str], Optional[str]]:
    """Return (departure, arrival) text from the card's time spans."""
    time_els = await card.query_selector_all(
        "[data-testid='flight-time'], .time, [class*='time']",
    )
    if len(time_els) < 2:
        return None, None
    return (
        (await safe_inner_text(time_els[0])) or None,
        (await safe_inner_text(time_els[1])) or None,
    )


async def deal_score(card: "ElementHandle") -> Optional[str]:
    """Pull the optional 'deal score' text from any card (None when absent)."""
    el = await card.query_selector("[class*='deal'], [class*='score']")
    if el is None:
        return None
    text = await safe_inner_text(el)
    return text or None


async def text_via_selectors(card: "ElementHandle", selectors: tuple[str, ...]) -> Optional[str]:
    """First non-empty inner-text under any of ``selectors``."""
    el = await query_first(card, selectors)
    if el is None:
        return None
    text = await safe_inner_text(el)
    return text or None


async def flight_card_data(card: "ElementHandle") -> Optional[dict]:
    """Render one flight card into a kwargs dict for ``KayakFlight``."""
    try:
        airline = await flight_airline(card)
        price_text = await text_via_selectors(card, FLIGHT_PRICE_SELECTORS)
        departure, arrival = await flight_times(card)
        duration = await text_via_selectors(card, FLIGHT_DURATION_SELECTORS)
        stops = (await text_via_selectors(card, FLIGHT_STOPS_SELECTORS)) or "Nonstop"
        booking_url = await resolve_link(card)
        score = await deal_score(card)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error parsing Kayak flight card: %s", type(exc).__name__)
        return None
    return {
        "airline": airline, "price": parse_price(price_text),
        "departure_time": departure, "arrival_time": arrival,
        "duration": duration, "stops": stops,
        "booking_url": booking_url, "deal_score": score,
    }


# ---------------------------------------------------------------------------
# Hotel-card extraction
# ---------------------------------------------------------------------------

HOTEL_NAME_SELECTORS = (
    "[data-testid='hotel-name']", "h3", "[class*='name']",
    "a[data-testid='property-title']",
)
HOTEL_PRICE_SELECTORS = (
    "[data-testid='hotel-price']", ".price", "[class*='price']",
)
HOTEL_RATING_SELECTORS = (
    "[data-testid='hotel-rating']", "[class*='rating']",
)
HOTEL_REVIEW_SELECTORS = (
    "[data-testid='hotel-reviews']", "[class*='review']",
)
HOTEL_LOCATION_SELECTORS = (
    "[data-testid='hotel-location']", "[class*='location']", "[class*='address']",
)


async def hotel_rating(card: "ElementHandle") -> Optional[float]:
    """Extract numeric rating from a rating element."""
    el = await query_first(card, HOTEL_RATING_SELECTORS)
    text = await safe_inner_text(el)
    if not text:
        return None
    match = re.search(r'(\d+\.?\d*)', text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def hotel_review_count(card: "ElementHandle") -> Optional[int]:
    """Extract review count from a review element."""
    el = await query_first(card, HOTEL_REVIEW_SELECTORS)
    text = await safe_inner_text(el)
    if not text:
        return None
    match = re.search(r'([\d,]+)', text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


async def _hotel_image(card: Any) -> Optional[str]:
    """Pull the hotel card image URL (first ``<img src>``)."""
    img_el = await card.query_selector("img")
    if img_el is None:
        return None
    return await img_el.get_attribute("src")


async def hotel_card_data(card: "ElementHandle") -> Optional[dict]:
    """Render one hotel card into a kwargs dict for ``KayakHotel``."""
    try:
        name = (await text_via_selectors(card, HOTEL_NAME_SELECTORS)) or "Unknown Hotel"
        price_text = await text_via_selectors(card, HOTEL_PRICE_SELECTORS)
        location = await text_via_selectors(card, HOTEL_LOCATION_SELECTORS)
        rating = await hotel_rating(card)
        review_count = await hotel_review_count(card)
        hotel_url = await resolve_link(card)
        image_url = await _hotel_image(card)
        score = await deal_score(card)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error parsing Kayak hotel card: %s", type(exc).__name__)
        return None
    return {
        "name": name, "price_per_night": parse_price(price_text),
        "rating": rating, "review_count": review_count,
        "location": location, "hotel_url": hotel_url,
        "image_url": image_url, "deal_score": score,
    }
