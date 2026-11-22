"""Helpers for the Google Maps Playwright scrapers.

Splits the long ``_do_search_google_maps`` and ``_do_get_place_detail``
orchestrators in :mod:`google_maps` into per-field helpers so the
orchestrators stay below the complexity thresholds.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    find_us_phone,
)

logger = logging.getLogger("crawlrx.google_maps")


PLACE_EXTRACTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    SELECTOR_EXTRACTION_EXCEPTIONS + PLAYWRIGHT_EXCEPTIONS
)


# Coordinate-extraction patterns from Maps URLs.
_COORD_PATTERNS = (
    re.compile(r"!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)"),
    re.compile(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)"),
    re.compile(r"/(\d+\.?\d*),(-?\d+\.?\d*)"),
)


def _coords_from_match(match: re.Match[str]) -> Optional[Dict[str, float]]:
    """Parse a regex match into a ``{lat, lng}`` dict (or ``None`` on bad parse)."""
    try:
        return {"lat": float(match.group(1)), "lng": float(match.group(2))}
    except ValueError:
        return None


def extract_coordinates_from_url(url: str) -> Optional[Dict[str, float]]:
    """Pull the lat/lng pair out of a Google Maps URL, if present."""
    for pattern in _COORD_PATTERNS:
        match = pattern.search(url)
        if match is None:
            continue
        coords = _coords_from_match(match)
        if coords is not None:
            return coords
    return None


def parse_rating(rating_text: Optional[str]) -> Optional[float]:
    """Parse a rating like '4.5' or '4.5 out of 5' into a float."""
    if not rating_text:
        return None
    match = re.search(r"(\d+\.?\d*)", rating_text.replace(",", "."))
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_review_count(text: Optional[str]) -> Optional[int]:
    """Parse a review count like '(1,234)' or '1.2K reviews' into an int."""
    if not text:
        return None
    cleaned = text.lower().replace("(", "").replace(")", "").replace(",", "")
    if "k" in cleaned:
        match = re.search(r"(\d+\.?\d*)\s*k", cleaned)
        if match:
            return int(float(match.group(1)) * 1000)
    match = re.search(r"(\d+)", cleaned)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


_NAME_SELECTORS = (
    "h3",
    "div[role='heading'] span",
    "div.fontHeadlineSmall",
    ".qBF1Pd",
)
_RATING_SELECTORS = (
    "span[role='img']",
    "span.MW4etd",
    ".MW4etd",
    "span[aria-label*='star']",
)
_REVIEW_SELECTORS = (
    "span[aria-label*='review']",
    "span.MW4etd + span",
    ".UY7F9",
)
_ADDRESS_SELECTORS = (
    "span[jstcache] span",
    "div.W4Efsd span:last-child",
    "span[aria-label*='address']",
    ".W4Efsd span",
)


async def _first_inner_text(card: Any, selectors: tuple[str, ...]) -> Optional[str]:
    """Return ``inner_text`` of the first matching selector on ``card``."""
    for sel in selectors:
        el = card.query_selector(sel)
        if not el:
            continue
        text = (await el.inner_text()).strip()
        if text:
            return text
    return None


async def extract_card_name(card: "ElementHandle") -> Optional[str]:
    """Best-effort name extraction from a Google Maps search-result card."""
    return await _first_inner_text(card, _NAME_SELECTORS)


async def _rating_from_element(rating_el: Any) -> Optional[float]:
    """Pull the numeric rating off a rating element via aria/text."""
    aria = await rating_el.get_attribute("aria-label") or ""
    text = (await rating_el.inner_text()).strip()
    if "star" in aria.lower():
        parts = aria.split()
        if parts:
            return parse_rating(parts[0])
    if text:
        return parse_rating(text)
    return None


async def extract_card_rating(card: "ElementHandle") -> Optional[float]:
    """Numeric rating from a search-result card, walking known selectors."""
    for sel in _RATING_SELECTORS:
        rating_el = card.query_selector(sel)
        if not rating_el:
            continue
        value = await _rating_from_element(rating_el)
        if value is not None:
            return value
    return None


async def extract_card_review_count(card: "ElementHandle") -> Optional[int]:
    """Review-count integer from a search-result card."""
    for sel in _REVIEW_SELECTORS:
        review_el = card.query_selector(sel)
        if not review_el:
            continue
        text = (await review_el.inner_text()).strip()
        count = parse_review_count(text)
        if count is not None:
            return count
    return None


async def extract_card_address(card: "ElementHandle") -> Optional[str]:
    """Address string from a search-result card (must contain a digit)."""
    for sel in _ADDRESS_SELECTORS:
        addr_el = card.query_selector(sel)
        if not addr_el:
            continue
        text = (await addr_el.inner_text()).strip()
        if text and len(text) > 5 and any(c.isdigit() for c in text):
            return text
    return None


async def extract_card_phone(card: "ElementHandle") -> Optional[str]:
    """Phone via ``tel:`` link, with regex fallback against card text."""
    phone_el = card.query_selector("a[href^='tel:']")
    if phone_el:
        href = await phone_el.get_attribute("href") or ""
        return href.replace("tel:", "") or None
    return find_us_phone(card.inner_text())


async def extract_card_website(card: "ElementHandle") -> Optional[str]:
    """External (non-google) website href off a card."""
    website_el = card.query_selector(
        "a[href^='http']:not([href*='google.com']):not([href^='tel:'])"
    )
    if not website_el:
        return None
    href = await website_el.get_attribute("href")
    if href and not href.startswith("/"):
        return href
    return None


async def extract_card_hours_open(card: "ElementHandle") -> Optional[bool]:
    """Best-effort 'is the place open right now' boolean."""
    hours_el = card.query_selector(
        "span[aria-label*='hours'], span[aria-label*='Open'], span[aria-label*='Closed']"
    )
    if not hours_el:
        return None
    aria = (await hours_el.get_attribute("aria-label") or "").lower()
    if "open" in aria:
        return True
    if "closed" in aria:
        return False
    return None


async def extract_card_price_level(card: "ElementHandle") -> Optional[str]:
    """Price-level string ($$, $$$, ...) from a card."""
    price_el = card.query_selector("span[aria-label*='price'], span[aria-label*='Price']")
    if price_el:
        text = (await price_el.inner_text()).strip()
        if text:
            return text
    match = re.search(r"\$+", card.inner_text())
    return match.group(0) if match else None


async def extract_card_categories(card: "ElementHandle", name: Optional[str]) -> Optional[List[str]]:
    """Comma-split category list from a card, ignoring duplicates of ``name``."""
    cat_el = card.query_selector(
        "div.fontBodyMedium span, div.W4Efsd span:first-child, .W4Efsd:first-child"
    )
    if not cat_el:
        return None
    text = (await cat_el.inner_text()).strip()
    if not text or text == (name or ""):
        return None
    return [c.strip() for c in text.split(",")]


async def _resolve_maps_url_from_link(link_el: Any, page: Any, card: Any, fallback: str) -> str:
    """Resolve the Maps URL by reading href, else clicking and reading ``page.url``."""
    href = await link_el.get_attribute("href")
    if href:
        return href
    try:
        await card.click()
        await page.wait_for_timeout(1000)
        return page.url
    except PLAYWRIGHT_EXCEPTIONS:
        return fallback


async def extract_card_maps_url(card: "ElementHandle", page: "Page", fallback: str) -> str:
    """URL link for a card, falling back to ``fallback`` when unresolved."""
    link_el = card.query_selector("a[href*='google.com/maps']")
    if not link_el:
        return fallback
    return await _resolve_maps_url_from_link(link_el, page, card, fallback)


async def _populate_card_fields(card: Any, name: str, place: Dict[str, Any]) -> None:
    """Populate every per-card optional field on ``place`` (in-place)."""
    extractors = (
        ("rating", extract_card_rating(card)),
        ("review_count", extract_card_review_count(card)),
        ("address", extract_card_address(card)),
        ("phone", extract_card_phone(card)),
        ("website", extract_card_website(card)),
        ("hours_open_now", extract_card_hours_open(card)),
        ("price_level", extract_card_price_level(card)),
        ("categories", extract_card_categories(card, name)),
    )
    for key, awaitable in extractors:
        value = await awaitable
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        place[key] = value


async def extract_place_card(card: "ElementHandle", page: "Page", fallback_url: str) -> Optional[Dict[str, Any]]:
    """Build a place dict from a search-result card, or ``None`` if unnamed."""
    name = await extract_card_name(card)
    if not name:
        return None
    place: Dict[str, Any] = {"name": name}
    await _populate_card_fields(card, name, place)
    place["maps_url"] = await extract_card_maps_url(card, page, fallback_url)
    coords = extract_coordinates_from_url(place["maps_url"])
    if coords:
        place["coordinates"] = coords
    return place


# ---------------------------------------------------------------------------
# Place-detail (single place) helpers
# ---------------------------------------------------------------------------

async def _safe_inner_text(el: Any) -> Optional[str]:
    """``inner_text`` of an element handle, ``None`` on error/empty."""
    if el is None:
        return None
    try:
        return (await el.inner_text()).strip() or None
    except PLACE_EXTRACTION_EXCEPTIONS:
        return None


async def get_place_name(page: "Page") -> Optional[str]:
    """Place name from the detail panel."""
    try:
        return await _safe_inner_text(await page.query_selector("h1, h2"))
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting name: %s", exc)
        return None


async def get_place_rating(page: "Page") -> Optional[float]:
    """Numeric rating from the detail panel."""
    try:
        rating_el = await page.query_selector(
            "[role='img'][aria-label*='star'], span.MW4etd"
        )
        if not rating_el:
            return None
        return await _rating_from_element(rating_el)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting rating: %s", exc)
        return None


async def get_place_review_count(page: "Page") -> Optional[int]:
    """Review count from the detail panel."""
    try:
        review_el = await page.query_selector(
            "button[aria-label*='review'], span[aria-label*='review']"
        )
        if not review_el:
            return None
        aria = await review_el.get_attribute("aria-label") or ""
        return parse_review_count(aria)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting review count: %s", exc)
        return None


async def get_place_address(page: "Page") -> Optional[str]:
    """Address from the detail panel."""
    try:
        addr_el = await page.query_selector(
            "[data-item-id='address'], button[aria-label*='Address']"
        )
        return await _safe_inner_text(addr_el)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting address: %s", exc)
        return None


async def get_place_phone(page: "Page") -> Optional[str]:
    """Phone from the detail panel."""
    try:
        phone_el = await page.query_selector(
            "[data-item-id='phone'], a[href^='tel:']"
        )
        if not phone_el:
            return None
        href = await phone_el.get_attribute("href")
        if href:
            return href.replace("tel:", "")
        return await _safe_inner_text(phone_el)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting phone: %s", exc)
        return None


async def get_place_website(page: "Page") -> Optional[str]:
    """Website href from the detail panel."""
    try:
        website_el = await page.query_selector(
            "[data-item-id='authority'], a[aria-label*='Website']"
        )
        if not website_el:
            return None
        return await website_el.get_attribute("href")
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting website: %s", exc)
        return None


async def _read_hours_table(page: Any) -> Dict[str, str]:
    """Read the day -> hours mapping from the expanded hours table."""
    rows: Dict[str, str] = {}
    table_rows = await page.query_selector_all("table tr")
    for row in table_rows:
        cells = row.query_selector_all("td, th")
        if len(cells) >= 2:
            day = cells[0].inner_text().strip()
            hours = cells[1].inner_text().strip()
            rows[day] = hours
    return rows


async def get_place_hours(page: "Page") -> Dict[str, str]:
    """Day -> hours mapping from the detail panel."""
    try:
        hours_button = await page.query_selector(
            "[aria-label*='Hours'], button[aria-label*='hours']"
        )
        if not hours_button:
            return {}
        await hours_button.click()
        await page.wait_for_timeout(1000)
        return await _read_hours_table(page)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting hours: %s", exc)
        return {}


async def get_place_open_now(page: "Page") -> Optional[bool]:
    """Open-now boolean from the detail panel."""
    try:
        open_el = await page.query_selector(
            "[aria-label*='Open'], [aria-label*='Closed']"
        )
        if not open_el:
            return None
        aria = (await open_el.get_attribute("aria-label") or "").lower()
        if "open" in aria:
            return True
        if "closed" in aria:
            return False
        return None
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting open status: %s", exc)
        return None


async def _build_review(review_el: Any) -> Optional[Dict[str, Any]]:
    """Build a review dict from one review element handle."""
    try:
        author = review_el.query_selector("[aria-label*='review'], .d4r55")
        rating = review_el.query_selector("[role='img']")
        text = review_el.query_selector("span[data-expandable-section], .wiI7pd")
        date = review_el.query_selector(".rsqaWe")
    except PLACE_EXTRACTION_EXCEPTIONS:
        return None
    return {
        "author": author.inner_text().strip() if author else None,
        "rating": parse_rating(rating.get_attribute("aria-label")) if rating else None,
        "text": text.inner_text().strip() if text else None,
        "date": date.inner_text().strip() if date else None,
    }


async def get_place_reviews(page: "Page") -> List[Dict[str, Any]]:
    """Top-5 reviews from the detail panel."""
    try:
        reviews_tab = await page.query_selector(
            "button[aria-label*='Reviews'], div[role='tab']:has-text('Reviews')"
        )
        if not reviews_tab:
            return []
        await reviews_tab.click()
        await page.wait_for_timeout(1500)
        review_elements = (await page.query_selector_all(
            "[data-review-id], .jftiEf"
        ))[:5]
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error extracting reviews: %s", exc)
        return []
    reviews: List[Dict[str, Any]] = []
    for review_el in review_elements:
        review = await _build_review(review_el)
        if review is not None:
            reviews.append(review)
    return reviews
