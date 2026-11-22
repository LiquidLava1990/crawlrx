"""Redfin property-detail fragment helpers (split from ``redfin.py``).

Each ``_fill_*`` coroutine pulls one logical chunk of a Redfin property
page into the shared ``data`` dict. Keeping them in a sibling module
keeps ``redfin.py`` under the 500 LOC ceiling without losing single-
responsibility per helper.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from . import _redfin_helpers as rfh
from ._realestate_helpers import (
    extract_float,
    extract_price_int,
    first_inner_text,
    parse_year,
)
from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)
from .models import RedfinPriceHistory, RedfinSchool, RedfinTaxHistory

logger = logging.getLogger("crawlrx.redfin.property")


# ---------------------------------------------------------------------------
# Empty-record factory
# ---------------------------------------------------------------------------

def empty_property(url: str) -> Dict[str, Any]:
    """Initial detail payload with all fields defaulted."""
    return {
        "address": None,
        "price": None,
        "beds": None,
        "baths": None,
        "sqft": None,
        "lot_size": None,
        "year_built": None,
        "description": None,
        "price_history": [],
        "tax_history": [],
        "schools": [],
        "walk_score": None,
        "transit_score": None,
        "images": [],
        "url": url,
    }


# ---------------------------------------------------------------------------
# Element-level read primitives (extraction-error tolerant)
# ---------------------------------------------------------------------------

async def _safe_inner_text(element: Any) -> Optional[str]:
    """``inner_text`` with the standard extraction-error swallow set."""
    try:
        text = await element.inner_text()
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return None
    return text.strip() if text else None


async def _row_cells_text(row: Any, cell_selector: str) -> List[str]:
    """Return inner-text strings for every cell under ``row``."""
    try:
        cells = await row.query_selector_all(cell_selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return []
    out: List[str] = []
    for cell in cells:
        text = await _safe_inner_text(cell)
        out.append(text or "")
    return out


async def _section_rows(
    page: Any, holder_selector: str, row_selector: str,
) -> List[Any]:
    """Locate ``holder_selector`` and return its child rows (skipping head)."""
    try:
        holder = await page.query_selector(holder_selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return []
    if holder is None:
        return []
    try:
        rows = await holder.query_selector_all(row_selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return []
    return list(rows[1:])


# ---------------------------------------------------------------------------
# Per-fragment fillers (mutate ``data`` in place)
# ---------------------------------------------------------------------------

async def fill_address(page: "Page", data: Dict[str, Any]) -> None:
    """Populate ``address`` from any of the address selectors."""
    address = await first_inner_text(page, rfh.detail_addr_selectors())
    if address:
        data["address"] = address


async def fill_price(page: "Page", data: Dict[str, Any]) -> None:
    """Populate ``price`` (int) from any of the price selectors."""
    price_text = await first_inner_text(page, rfh.detail_price_selectors())
    if price_text:
        data["price"] = extract_price_int(price_text)


async def fill_description(page: "Page", data: Dict[str, Any]) -> None:
    """Populate ``description`` from any of the description selectors."""
    description = await first_inner_text(page, rfh.detail_desc_selectors())
    if description:
        data["description"] = description


_KEY_BED_RE = re.compile(r"\b(?:bd|bed)\b", re.IGNORECASE)
_KEY_BATH_RE = re.compile(r"\b(?:ba|bath)\b", re.IGNORECASE)
_KEY_SQFT_RE = re.compile(r"sq\s*ft|sqft", re.IGNORECASE)
_KEY_BUILT_RE = re.compile(r"built", re.IGNORECASE)
_KEY_LOT_RE = re.compile(r"lot|acre", re.IGNORECASE)


def _classify_key_detail(text: str, data: Dict[str, Any]) -> None:
    """Mutate ``data`` based on which header (beds/baths/sqft/...) hit."""
    if _KEY_BED_RE.search(text):
        value = extract_float(text)
        if value is not None:
            data["beds"] = value
        return
    if _KEY_BATH_RE.search(text):
        value = extract_float(text)
        if value is not None:
            data["baths"] = value
        return
    if _KEY_SQFT_RE.search(text):
        value = extract_float(text)
        if value is not None:
            data["sqft"] = int(value)
        return
    if _KEY_BUILT_RE.search(text):
        year = parse_year(text)
        if year is not None:
            data["year_built"] = year
        return
    if _KEY_LOT_RE.search(text):
        data["lot_size"] = text


async def fill_key_details(page: "Page", data: Dict[str, Any]) -> None:
    """Walk every ``.keyDetail`` element and dispatch to classifier."""
    try:
        elements = await page.query_selector_all(rfh.key_detail_selector())
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return
    for element in elements:
        text = await _safe_inner_text(element)
        if text:
            _classify_key_detail(text, data)


async def fill_price_history(page: "Page", data: Dict[str, Any]) -> None:
    """Append each parsed row to ``price_history`` -- model-validated."""
    rows = await _section_rows(
        page, rfh.price_history_holder(), rfh.price_history_row_selector(),
    )
    for row in rows:
        cells = await _row_cells_text(row, rfh.price_history_cell_selector())
        if len(cells) < 2:
            continue
        try:
            entry = RedfinPriceHistory(
                date=cells[0],
                event=cells[1] if len(cells) > 1 else "",
                price=extract_price_int(cells[2]) if len(cells) > 2 else None,
            )
        except PARSE_EXCEPTIONS:
            continue
        data["price_history"].append(entry)


async def fill_tax_history(page: "Page", data: Dict[str, Any]) -> None:
    """Append each parsed row to ``tax_history`` -- model-validated."""
    rows = await _section_rows(
        page, rfh.tax_history_holder(), rfh.tax_history_row_selector(),
    )
    for row in rows:
        cells = await _row_cells_text(row, rfh.tax_history_cell_selector())
        if len(cells) < 2:
            continue
        year = parse_year(cells[0])
        if year is None:
            continue
        try:
            entry = RedfinTaxHistory(
                year=year,
                taxes=extract_price_int(cells[1]) if len(cells) > 1 else None,
                assessment=(
                    extract_price_int(cells[2]) if len(cells) > 2 else None
                ),
            )
        except PARSE_EXCEPTIONS:
            continue
        data["tax_history"].append(entry)


async def _school_record(card: Any) -> Optional[RedfinSchool]:
    """Build a :class:`RedfinSchool` from one school-card element."""
    name = await first_inner_text(card, (".school-name", "td:first-child"))
    if not name:
        return None
    rating_text = await first_inner_text(
        card, (".school-rating", ".rating", "td:nth-child(2)"),
    )
    distance = await first_inner_text(
        card, (".school-distance", ".distance", "td:nth-child(3)"),
    )
    grades = await first_inner_text(card, (".school-grades", ".grades"))
    try:
        return RedfinSchool(
            name=name,
            rating=extract_float(rating_text) if rating_text else None,
            distance=distance,
            grades=grades,
        )
    except PARSE_EXCEPTIONS:
        return None


async def fill_schools(page: "Page", data: Dict[str, Any]) -> None:
    """Append validated :class:`RedfinSchool` records to ``schools``."""
    try:
        holder = await page.query_selector(rfh.schools_holder())
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return
    if holder is None:
        return
    try:
        cards = await holder.query_selector_all(rfh.school_card_selector())
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return
    for card in cards:
        record = await _school_record(card)
        if record is not None:
            data["schools"].append(record)


_WALK_RE = re.compile(r"walk\s*score\s*[:\s]*(\d+)", re.IGNORECASE)
_TRANSIT_RE = re.compile(r"transit\s*score\s*[:\s]*(\d+)", re.IGNORECASE)


def _set_int_match(
    pattern: re.Pattern[str], text: str, data: Dict[str, Any], key: str,
) -> None:
    """Set ``data[key]`` to the int-cast first capture group, if any."""
    match = pattern.search(text)
    if not match:
        return
    try:
        data[key] = int(match.group(1))
    except (ValueError, OverflowError):
        return


async def fill_walk_score(page: "Page", data: Dict[str, Any]) -> None:
    """Populate ``walk_score`` / ``transit_score`` from the walk-score block."""
    try:
        section = await page.query_selector(rfh.walk_score_holder())
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return
    if section is None:
        return
    text = await _safe_inner_text(section)
    if not text:
        return
    _set_int_match(_WALK_RE, text, data, "walk_score")
    _set_int_match(_TRANSIT_RE, text, data, "transit_score")


async def _img_src(img: Any) -> Optional[str]:
    """First non-empty ``src`` / ``data-src`` for an ``img`` element."""
    for attr in ("src", "data-src"):
        try:
            value = await img.get_attribute(attr)
        except SELECTOR_EXTRACTION_EXCEPTIONS:
            continue
        if value:
            return value
    return None


async def fill_images(page: "Page", data: Dict[str, Any]) -> None:
    """Append up to 20 unique image URLs sourced from any gallery selector."""
    seen: List[str] = data["images"]
    for selector in rfh.detail_img_selectors():
        try:
            elements = await page.query_selector_all(selector)
        except SELECTOR_EXTRACTION_EXCEPTIONS:
            continue
        for img in elements[:20]:
            src = await _img_src(img)
            if src and src not in seen:
                seen.append(src)
        if seen:
            return


PROPERTY_FRAGMENTS = (
    fill_address,
    fill_price,
    fill_key_details,
    fill_description,
    fill_price_history,
    fill_tax_history,
    fill_schools,
    fill_walk_score,
    fill_images,
)
