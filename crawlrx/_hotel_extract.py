"""Per-card / per-page extraction helpers for ``hotel_scraper.py``.

Split out so the orchestrator can stay under 500 lines and individual
function bodies can stay under the 50/15/3 god-fn / cognitive / nesting
ceilings.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.booking.extract")


# ---------------------------------------------------------------------------
# Generic per-element probes
# ---------------------------------------------------------------------------

async def query_first(scope: "Page | ElementHandle", selectors: tuple[str, ...]) -> "ElementHandle | None":
    """First non-None ``query_selector`` match across ``selectors``."""
    for sel in selectors:
        el = await scope.query_selector(sel)
        if el is not None:
            return el
    return None


async def safe_inner_text(el: "ElementHandle | None") -> str:
    """Best-effort ``el.inner_text().strip()`` (returns ``""`` when None)."""
    if el is None:
        return ""
    return (await el.inner_text()).strip()


async def text_via_selectors(scope: "Page | ElementHandle", selectors: tuple[str, ...]) -> Optional[str]:
    """First non-empty inner-text among ``selectors`` (None when absent)."""
    el = await query_first(scope, selectors)
    if el is None:
        return None
    text = await safe_inner_text(el)
    return text or None


# ---------------------------------------------------------------------------
# Number parsers
# ---------------------------------------------------------------------------

def parse_rating(rating_text: Optional[str]) -> Optional[float]:
    """Extract rating from text like '8.5' or '8.5 Wonderful'."""
    if not rating_text:
        return None
    match = re.search(r'([\d.]+)', rating_text)
    if not match:
        return None
    try:
        rating = float(match.group(1))
    except ValueError:
        return None
    return rating if 0 <= rating <= 10 else None


def parse_review_count(review_text: Optional[str]) -> Optional[int]:
    """Extract review count from text like '(1,234 reviews)'."""
    if not review_text:
        return None
    match = re.search(r'([\d,]+)\s+review', review_text.lower())
    if not match:
        return None
    try:
        return int(match.group(1).replace(',', ''))
    except ValueError:
        return None


def parse_first_int(text: Optional[str]) -> Optional[int]:
    """Pull the first integer in ``text`` (None if absent)."""
    if not text:
        return None
    match = re.search(r'(\d+)', text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_prices(price_text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(per_night, total)`` from a price blob (best-effort)."""
    if not price_text:
        return None, None
    prices = re.findall(r'\$[\d,]+(?:\.\d{2})?', price_text)
    if len(prices) >= 2:
        return prices[0], prices[-1]
    if prices:
        return prices[0], None
    return None, None


# ---------------------------------------------------------------------------
# Search-card extraction
# ---------------------------------------------------------------------------

NAME_SELECTORS = (
    "[data-testid='title']", ".sr-hotel__name",
)
PRICE_SELECTORS = (
    "[data-testid='price-and-discounted-price']",
    ".prco-valign-middle-helper",
    "[data-testid='price']",
)
RATING_SELECTORS = (
    "[data-testid='review-score']", ".bui-review-score__badge",
)
REVIEW_SELECTORS = (
    "[data-testid='review-score-text']", ".bui-review-score__text",
)
LOCATION_SELECTORS = (
    "[data-testid='address']", ".sr_card_address_line",
)
LINK_SELECTORS = (
    "a[data-testid='title-link']", "h3 a", ".sr_item_photo_link",
)


def _classify_property_type(name: str) -> Optional[str]:
    """Best-effort property-type label drawn from the listing name."""
    name_lower = name.lower()
    for keyword, label in (
        ("hotel", "Hotel"),
        ("apartment", "Apartment"),
        ("residence", "Apartment"),
        ("hostel", "Hostel"),
        ("resort", "Resort"),
    ):
        if keyword in name_lower:
            return label
    return None


async def _flag_on_text(card: Any, pattern: str) -> bool:
    """True iff a regex-text-selector resolves on the card."""
    try:
        match = await card.query_selector(pattern)
        return match is not None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Flag probe suppressed: %s", type(exc).__name__)
        return False


async def _resolve_card_url(card: Any, base_url: str) -> Optional[str]:
    """Resolve the card's listing URL using documented anchor selectors."""
    link_el = await query_first(card, LINK_SELECTORS)
    if link_el is None:
        return None
    href = await link_el.get_attribute("href") or ""
    if not href:
        return None
    return href if href.startswith("http") else f"{base_url}{href}"


async def _review_count_and_label(card: Any) -> tuple[Optional[int], Optional[str]]:
    """Return (count, label) read from the review-text block."""
    review_el = await query_first(card, REVIEW_SELECTORS)
    if review_el is None:
        return None, None
    review_text = await safe_inner_text(review_el)
    count = parse_review_count(review_text)
    parts = review_text.split()
    label: Optional[str] = None
    if parts and not parts[0].replace('.', '').isdigit():
        label = parts[0]
    return count, label


async def search_card_data(card: "ElementHandle", base_url: str) -> Optional[dict]:
    """Render one search-result card into a kwargs dict for ``BookingProperty``."""
    try:
        listing_url = await _resolve_card_url(card, base_url)
        if not listing_url:
            return None
        name_el = await query_first(card, NAME_SELECTORS)
        name = (await safe_inner_text(name_el)) or "Unknown Property"
        price_text = await text_via_selectors(card, PRICE_SELECTORS)
        per_night, total = parse_prices(price_text)
        rating = parse_rating(await text_via_selectors(card, RATING_SELECTORS))
        review_count, review_label = await _review_count_and_label(card)
        location = await text_via_selectors(card, LOCATION_SELECTORS)
        distance_el = await card.query_selector(
            "[data-testid='distance'], .distfromdest",
        )
        distance = (await safe_inner_text(distance_el)) or None if distance_el else None
        img_el = await card.query_selector(
            "img[data-testid='image'], .sr_item_photo img",
        )
        image_url = await img_el.get_attribute("src") if img_el else None
        free_cancel = await _flag_on_text(card, "text=/free cancellation/i")
        breakfast = await _flag_on_text(card, "text=/breakfast included/i")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error parsing Kayak.com property card: %s", type(exc).__name__)
        return None
    return {
        "name": name,
        "price_per_night": per_night,
        "total_price": total,
        "rating": rating,
        "review_count": review_count,
        "review_label": review_label,
        "location": location,
        "property_type": _classify_property_type(name),
        "listing_url": listing_url,
        "image_url": image_url,
        "free_cancellation": free_cancel,
        "breakfast_included": breakfast,
        "distance_from_center": distance,
    }


# ---------------------------------------------------------------------------
# Property-detail extraction (per-section probes)
# ---------------------------------------------------------------------------

DETAIL_NAME_SELECTORS = (
    "h1", "[data-testid='property-title']", "h2.hp__hotel-title",
)
DETAIL_DESC_SELECTORS = (
    "[data-testid='property-description']",
    "#property_description_content",
    ".hp-description .hp_desc_main_content",
)
DETAIL_ADDRESS_SELECTORS = (
    "[data-testid='address']", ".hp_address_subtitle",
    "[data-node_tt_id='location_score_tooltip']",
)
DETAIL_RATING_SELECTORS = (
    "[data-testid='review-score-component']", ".bui-review-score__badge",
)
DETAIL_REVIEW_SELECTORS = (
    "[data-testid='review-score-text']", ".bui-review-score__text",
)


async def detail_text(page: "Page", selectors: tuple[str, ...]) -> Optional[str]:
    """Read inner text under any selector on the property detail page."""
    try:
        return await text_via_selectors(page, selectors)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Detail probe suppressed: %s", type(exc).__name__)
        return None


async def detail_rating_and_count(
    page: "Page",
) -> tuple[Optional[float], Optional[int]]:
    """Pull (rating, review_count) from the detail page's review block."""
    try:
        rating_text = await text_via_selectors(page, DETAIL_RATING_SELECTORS)
        review_el = await query_first(page, DETAIL_REVIEW_SELECTORS)
        review_text = await safe_inner_text(review_el)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Detail rating probe suppressed: %s", type(exc).__name__)
        return None, None
    return parse_rating(rating_text), parse_review_count(review_text)


from dataclasses import dataclass


@dataclass(frozen=True)
class _LiSpec:
    """Bundle for a list-of-li page section probe."""

    selector: str
    cap: int
    max_len: int
    label: str


async def _collect_li_text(page: Any, spec: _LiSpec) -> list[str]:
    """Read up to ``spec.cap`` ``<li>`` strings under the given selector."""
    out: list[str] = []
    try:
        els = await page.query_selector_all(spec.selector)
        for el in els[:spec.cap]:
            text = (await el.inner_text()).strip()
            if text and len(text) < spec.max_len:
                out.append(text)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("%s probe suppressed: %s", spec.label, type(exc).__name__)
    return out


_FACILITY_SPEC = _LiSpec(
    selector=(
        "[data-testid='facility-list'] li, "
        ".hp_desc_important_facilities li, .facilities li"
    ),
    cap=50, max_len=100, label="Facility",
)
_POLICY_SPEC = _LiSpec(
    selector="[data-testid='property-section-policies'] li, .hp-policies-block li",
    cap=20, max_len=200, label="Policies",
)
_NEARBY_SPEC = _LiSpec(
    selector="[data-testid='property-section-nearby'] li, .hp-nearby-attractions li",
    cap=15, max_len=10_000, label="Nearby",
)


async def detail_facilities(page: "Page") -> list[str]:
    """List of facility labels (de-duplicated, capped at 50)."""
    try:
        show = await page.query_selector(
            "button:has-text('See all'), button:has-text('facilities')",
        )
        if show is not None:
            await show.click()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Facility expand-click suppressed: %s", type(exc).__name__)
    return list(dict.fromkeys(await _collect_li_text(page, _FACILITY_SPEC)))


async def detail_policies(page: "Page") -> list[str]:
    """List of policy strings (capped at 20)."""
    return await _collect_li_text(page, _POLICY_SPEC)


async def detail_nearby(page: "Page") -> list[str]:
    """List of nearby-attraction labels (capped at 15)."""
    return await _collect_li_text(page, _NEARBY_SPEC)


async def detail_photo_count(page: "Page") -> int:
    """Count of photo thumbnails on the detail page."""
    try:
        photo_els = await page.query_selector_all(
            "[data-testid='property-photos'] img, .bh-photo-grid-item img",
        )
        return len(photo_els)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Photo count probe suppressed: %s", type(exc).__name__)
        return 0


# ---------------------------------------------------------------------------
# Room-card + review-card extraction
# ---------------------------------------------------------------------------

async def room_card_data(card: "ElementHandle") -> Optional[dict]:
    """Render one room-card into kwargs for ``BookingRoomType``."""
    try:
        name_el = await card.query_selector(
            "[data-testid='room-title'], .hprt-roomtype-block",
        )
        name = (await safe_inner_text(name_el)) or "Unknown Room"
        price_el = await card.query_selector(
            "[data-testid='price'], .hprt-price-block",
        )
        price = (await safe_inner_text(price_el)) or None if price_el else None
        occ_el = await card.query_selector("[data-testid='room-max-occupancy']")
        occ_text = await safe_inner_text(occ_el)
        bed_el = await card.query_selector("[data-testid='bed-configuration']")
        bed_type = (await safe_inner_text(bed_el)) or None if bed_el else None
        refundable = await _flag_on_text(card, "text=/free cancellation/i")
        breakfast = await _flag_on_text(card, "text=/breakfast/i")
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None
    return {
        "name": name,
        "price_per_night": price,
        "total_price": None,
        "max_occupancy": parse_first_int(occ_text),
        "bed_type": bed_type,
        "refundable": refundable,
        "breakfast_included": breakfast,
    }


async def review_card_data(card: "ElementHandle") -> Optional[dict]:
    """Render one review card into kwargs for ``BookingReview``."""
    try:
        title_el = await card.query_selector("[data-testid='review-title']")
        title = (await safe_inner_text(title_el)) or None if title_el else None
        rating_el = await card.query_selector("[data-testid='review-score']")
        rating = parse_first_int(await safe_inner_text(rating_el))
        text_el = await card.query_selector("[data-testid='review-text']")
        text = (await safe_inner_text(text_el)) or None if text_el else None
        author_el = await card.query_selector("[data-testid='reviewer-name']")
        author = (await safe_inner_text(author_el)) or None if author_el else None
        date_el = await card.query_selector("[data-testid='review-date']")
        date = (await safe_inner_text(date_el)) or None if date_el else None
        helpful_el = await card.query_selector("[data-testid='helpful-button']")
        helpful_count = parse_first_int(await safe_inner_text(helpful_el))
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None
    return {
        "title": title,
        "rating": rating,
        "text": text,
        "author": author,
        "date": date,
        "helpful_count": helpful_count,
    }
