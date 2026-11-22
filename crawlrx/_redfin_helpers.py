"""Redfin card / detail extraction helpers (split from ``redfin.py``).

Every helper here is a small async building block used by either the
search-listing card loop or the single-property detail page parser.
Splitting the god functions into these named primitives drops cognitive
complexity from 170 -> <15 per leaf and keeps nesting depth <= 3.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._realestate_helpers import (
    extract_price_int,
    first_attribute,
    first_inner_text,
)
from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS

logger = logging.getLogger("crawlrx.redfin.helpers")


REDFIN_BASE_URL = "https://www.redfin.com"


# ---------------------------------------------------------------------------
# Selector tables (single source of truth for the family)
# ---------------------------------------------------------------------------

CARD_SELECTORS: tuple[str, ...] = (
    ".HomeCardContainer",
    ".home-card",
    "[data-rf-test-id='home-card']",
    ".MapHomeCard",
)

ADDRESS_SELECTORS: tuple[str, ...] = (
    ".address",
    ".home-address",
    "[data-rf-test-id='address']",
    "h3",
)

PRICE_SELECTORS: tuple[str, ...] = (
    ".price",
    ".home-price",
    "[data-rf-test-id='price']",
    ".bp-Homecard__Price",
)

STATS_SELECTORS: tuple[str, ...] = (
    ".stats",
    ".home-stats",
    "[data-rf-test-id='home-stats']",
    ".bp-Homecard__Stats",
)

DOM_SELECTORS: tuple[str, ...] = (
    ".days-on-market",
    ".time-on-redfin",
    "[data-rf-test-id='days-on-market']",
)

LINK_SELECTORS: tuple[str, ...] = (
    "a[href*='/home/']",
    "a.slide",
    ".home-card a",
)

IMG_SELECTORS: tuple[str, ...] = (
    "img",
    ".home-image img",
    "[data-rf-test-id='home-image'] img",
)

STATUS_SELECTORS: tuple[str, ...] = (
    ".status",
    ".home-status",
    "[data-rf-test-id='home-status']",
    ".bp-Homecard__Status",
)

DETAIL_SELECTORS: tuple[str, ...] = (
    ".home-details",
    ".details",
    ".bp-Homecard__Details",
)

DETAIL_ADDR_SELECTORS: tuple[str, ...] = (
    "h1.address",
    ".home-address",
    "[data-rf-test-id='address-section']",
    "header h1",
)

DETAIL_PRICE_SELECTORS: tuple[str, ...] = (
    ".price",
    ".home-price",
    "[data-rf-test-id='price']",
    ".bp-HomeInfo__Price",
)

DETAIL_DESC_SELECTORS: tuple[str, ...] = (
    ".description",
    ".property-description",
    "[data-rf-test-id='description']",
    ".text-container",
)

DETAIL_IMG_SELECTORS: tuple[str, ...] = (
    ".photo-carousel img",
    ".property-image img",
    ".home-gallery img",
)


# ---------------------------------------------------------------------------
# Stats string -> beds / baths / sqft
# ---------------------------------------------------------------------------

_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bd", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ba", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\s*sqft", re.IGNORECASE)
_BUILT_RE = re.compile(r"built\s+(\d{4})", re.IGNORECASE)
_LOT_RE = re.compile(r"(\d+[\d,]*\.?\d*)\s*(acre|sq ft|sqft)", re.IGNORECASE)
_DOM_RE = re.compile(r"(\d+)")


def parse_stats_text(stats_text: str, listing: Dict[str, Any]) -> None:
    """Mutate ``listing`` with beds / baths / sqft parsed from a stats blob."""
    bed_match = _BED_RE.search(stats_text)
    if bed_match:
        listing["beds"] = float(bed_match.group(1))
    bath_match = _BATH_RE.search(stats_text)
    if bath_match:
        listing["baths"] = float(bath_match.group(1))
    sqft_match = _SQFT_RE.search(stats_text)
    if sqft_match:
        try:
            listing["sqft"] = int(sqft_match.group(1).replace(",", ""))
        except (ValueError, OverflowError):
            pass


def parse_detail_text(detail_text: str, listing: Dict[str, Any]) -> None:
    """Mutate ``listing`` with year / lot parsed from a detail blob."""
    year_match = _BUILT_RE.search(detail_text)
    if year_match:
        try:
            listing["year_built"] = int(year_match.group(1))
        except (ValueError, OverflowError):
            pass
    lot_match = _LOT_RE.search(detail_text)
    if lot_match:
        listing["lot_size"] = lot_match.group(0)


def parse_dom_text(dom_text: str) -> Optional[int]:
    """Extract the days-on-market integer from a Redfin DOM badge."""
    match = _DOM_RE.search(dom_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Card extraction (one card -> populated dict)
# ---------------------------------------------------------------------------

async def fill_card_address(card: "ElementHandle", listing: Dict[str, Any]) -> bool:
    """Set ``address`` on ``listing``; return True iff one was found."""
    address = await first_inner_text(card, ADDRESS_SELECTORS)
    if address:
        listing["address"] = address
        return True
    return False


async def fill_card_price(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Set ``price`` on ``listing`` if any price selector resolved."""
    price_text = await first_inner_text(card, PRICE_SELECTORS)
    if price_text:
        parsed = extract_price_int(price_text)
        if parsed is not None:
            listing["price"] = parsed


async def fill_card_stats(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Apply :func:`parse_stats_text` against the first stats element."""
    stats_text = await first_inner_text(card, STATS_SELECTORS)
    if stats_text:
        parse_stats_text(stats_text, listing)


async def fill_card_dom(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Set ``days_on_market`` from a DOM badge if present."""
    dom_text = await first_inner_text(card, DOM_SELECTORS)
    if not dom_text:
        return
    dom_value = parse_dom_text(dom_text)
    if dom_value is not None:
        listing["days_on_market"] = dom_value


async def fill_card_link(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Set ``listing_url`` resolving relative paths against Redfin base."""
    href = await first_attribute(card, LINK_SELECTORS, "href")
    if not href:
        return
    if href.startswith("http"):
        listing["listing_url"] = href
    else:
        listing["listing_url"] = REDFIN_BASE_URL + href


async def fill_card_image(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Set ``image_url`` from the first ``src`` / ``data-src``."""
    src = await first_attribute(card, IMG_SELECTORS, "src", "data-src")
    if src:
        listing["image_url"] = src


async def fill_card_status(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Set ``status`` (``Active`` / ``Pending`` / etc.) if present."""
    status = await first_inner_text(card, STATUS_SELECTORS)
    if status:
        listing["status"] = status


async def fill_card_detail(card: "ElementHandle", listing: Dict[str, Any]) -> None:
    """Apply :func:`parse_detail_text` against the first detail element."""
    detail_text = await first_inner_text(card, DETAIL_SELECTORS)
    if detail_text:
        parse_detail_text(detail_text, listing)


def compute_price_per_sqft(listing: Dict[str, Any]) -> None:
    """Mutate ``listing`` with computed ``price_per_sqft`` when feasible."""
    price = listing.get("price")
    sqft = listing.get("sqft")
    if not price or not sqft or sqft <= 0:
        return
    try:
        listing["price_per_sqft"] = int(price / sqft)
    except (ValueError, ZeroDivisionError):
        return


async def extract_listing_card(card: "ElementHandle") -> Optional[Dict[str, Any]]:
    """Return a populated listing dict for one card, ``None`` if no address."""
    listing: Dict[str, Any] = {}
    if not await fill_card_address(card, listing):
        return None
    await fill_card_price(card, listing)
    await fill_card_stats(card, listing)
    compute_price_per_sqft(listing)
    await fill_card_dom(card, listing)
    await fill_card_link(card, listing)
    await fill_card_image(card, listing)
    await fill_card_status(card, listing)
    await fill_card_detail(card, listing)
    return listing


async def collect_card_elements(page: "Page") -> List[Any]:
    """Try each Redfin card selector until one returns a non-empty list."""
    for selector in CARD_SELECTORS:
        try:
            cards = await page.query_selector_all(selector)
        except SELECTOR_EXTRACTION_EXCEPTIONS:
            continue
        if cards:
            return list(cards)
    return []


# ---------------------------------------------------------------------------
# Search URL builder (single-arg via SearchUrlSpec)
# ---------------------------------------------------------------------------

def location_slug(location: str) -> str:
    """Slugify ``location`` the way Redfin expects (lowercase + dashes)."""
    return location.lower().replace(" ", "-").replace(",", "")


def search_path(listing_type: str, slug: str) -> str:
    """Map ``listing_type`` to the Redfin URL path segment."""
    if listing_type == "sold":
        return f"/zip/{slug}/sold"
    if listing_type == "for_rent":
        return f"/zip/{slug}/apartments"
    return f"/zip/{slug}"


def search_query(
    min_price: Optional[int],
    max_price: Optional[int],
    min_beds: Optional[int],
    min_baths: Optional[float],
) -> str:
    """Build a ``?key=value&...`` query string from the filter bag."""
    params: List[str] = []
    if min_price:
        params.append(f"minPrice={min_price}")
    if max_price:
        params.append(f"maxPrice={max_price}")
    if min_beds:
        params.append(f"minBeds={min_beds}")
    if min_baths:
        params.append(f"minBaths={min_baths}")
    if not params:
        return ""
    return "?" + "&".join(params)


# ---------------------------------------------------------------------------
# Property-detail fragment parsers (mutate ``property_data`` in place)
# ---------------------------------------------------------------------------

_KEY_DETAIL_SELECTOR = ".keyDetailsList .keyDetail, .home-stats .stat"
_PRICE_HISTORY_HOLDER = "[data-rf-test-id='price-history'], .price-history"
_PRICE_HISTORY_ROWS = "tr, .price-history-row"
_PRICE_HISTORY_CELLS = "td, .price-history-cell"
_TAX_HISTORY_HOLDER = "[data-rf-test-id='tax-history'], .tax-history"
_TAX_HISTORY_ROWS = "tr, .tax-history-row"
_TAX_HISTORY_CELLS = "td, .tax-history-cell"
_SCHOOLS_HOLDER = "[data-rf-test-id='schools'], .schools-section"
_SCHOOL_CARDS = ".school-card, .school-item, tr"
_WALK_HOLDER = "[data-rf-test-id='walk-score'], .walk-score"
_WAIT_DETAIL_SELECTOR = (
    ".home-details, .property-details, [data-rf-test-id='property-details']"
)


def detail_wait_selector() -> str:
    """Selector waited on after navigating to a property page."""
    return _WAIT_DETAIL_SELECTOR


def key_detail_selector() -> str:
    """Selector for the page-level key-details list."""
    return _KEY_DETAIL_SELECTOR


def price_history_holder() -> str:
    """Selector for the price-history table holder."""
    return _PRICE_HISTORY_HOLDER


def price_history_row_selector() -> str:
    """Selector for one price-history row."""
    return _PRICE_HISTORY_ROWS


def price_history_cell_selector() -> str:
    """Selector for one price-history cell."""
    return _PRICE_HISTORY_CELLS


def tax_history_holder() -> str:
    """Selector for the tax-history table holder."""
    return _TAX_HISTORY_HOLDER


def tax_history_row_selector() -> str:
    """Selector for one tax-history row."""
    return _TAX_HISTORY_ROWS


def tax_history_cell_selector() -> str:
    """Selector for one tax-history cell."""
    return _TAX_HISTORY_CELLS


def schools_holder() -> str:
    """Selector for the schools section holder."""
    return _SCHOOLS_HOLDER


def school_card_selector() -> str:
    """Selector for one school card / row."""
    return _SCHOOL_CARDS


def walk_score_holder() -> str:
    """Selector for the walk-score block."""
    return _WALK_HOLDER


def detail_addr_selectors() -> Sequence[str]:
    """Return the property-page address selector list."""
    return DETAIL_ADDR_SELECTORS


def detail_price_selectors() -> Sequence[str]:
    """Return the property-page price selector list."""
    return DETAIL_PRICE_SELECTORS


def detail_desc_selectors() -> Sequence[str]:
    """Return the property-page description selector list."""
    return DETAIL_DESC_SELECTORS


def detail_img_selectors() -> Sequence[str]:
    """Return the property-page image gallery selector list."""
    return DETAIL_IMG_SELECTORS
