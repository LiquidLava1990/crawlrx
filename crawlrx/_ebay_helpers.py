"""eBay scraper helpers extracted from ``ebay.py``.

Module split: card-level parsing primitives + Playwright field
extractors live here so the public ``ebay.py`` orchestrators stay below
the the function-length limit / cyclomatic / cognitive thresholds.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
from bs4 import BeautifulSoup, Tag

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.ebay.helpers")

EBAY_BASE_URL = "https://www.ebay.com"

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.0.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

PARSE_EXC: Tuple[type[BaseException], ...] = ELEMENT_PROBE_EXCEPTIONS
SELECTOR_EXC: Tuple[type[BaseException], ...] = SELECTOR_EXTRACTION_EXCEPTIONS


# ─────────────────────────────────────────────
# URL + simple field helpers
# ─────────────────────────────────────────────

_ID_PATTERNS: Tuple[str, ...] = (
    r"/itm/(\d+)",
    r"itemId=(\d+)",
    r"itm/(\d+)",
)


def extract_item_id(url: str) -> Optional[str]:
    """Return eBay item id from a listing/itm URL or ``None``."""
    for pattern in _ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def normalize_price(price_text: str) -> str:
    """Trim + collapse whitespace inside an eBay price snippet."""
    if not price_text:
        return ""
    cleaned = price_text.strip().replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", cleaned)


# ─────────────────────────────────────────────
# Search URL builder
# ─────────────────────────────────────────────

_SORT_MAP: Dict[str, str] = {
    "best_match": "12",
    "price_asc": "2",
    "price_desc": "3",
    "ending_soon": "1",
}


def _condition_token(condition: Optional[str]) -> Optional[str]:
    """Map a `condition` request value to the eBay search query token."""
    if condition == "new":
        return "new"
    if condition == "used":
        return "used"
    return None


def _listing_type_token(listing_type: Optional[str]) -> Optional[str]:
    """Map a `listing_type` request value to the eBay search query token."""
    if listing_type == "auction":
        return "auction"
    if listing_type == "buy_it_now":
        return "buy it now"
    return None


def build_search_url(params: Dict[str, Any]) -> str:
    """Build eBay search URL from request param dict."""
    from urllib.parse import urlencode

    query_parts: List[str] = [params["query"]]
    for token in (
        _condition_token(params.get("condition")),
        _listing_type_token(params.get("listing_type")),
    ):
        if token:
            query_parts.append(token)

    ebay_params: Dict[str, Any] = {
        "_nkw": " ".join(query_parts),
        "_ipg": min(params.get("limit", 20), 50),
    }
    if params.get("min_price"):
        ebay_params["_udlo"] = params["min_price"]
    if params.get("max_price"):
        ebay_params["_udhi"] = params["max_price"]
    if params.get("sort"):
        ebay_params["_sop"] = _SORT_MAP.get(params["sort"], "12")

    return f"https://www.ebay.com/sch/i.html?{urlencode(ebay_params)}"


# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class HttpFetchResult:
    """HTTP fetch result: ``ok=False`` carries the exception message."""

    ok: bool
    text: str = ""
    error: Optional[str] = None


def http_get_text(url: str, *, timeout: float = 30.0) -> HttpFetchResult:
    """GET ``url`` and return body text or a typed error result."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=DEFAULT_HEADERS)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("HTTP error fetching eBay URL: %s", exc)
        return HttpFetchResult(ok=False, error=str(exc))
    return HttpFetchResult(ok=True, text=resp.text)


# ─────────────────────────────────────────────
# Search-card parsers
# ─────────────────────────────────────────────

def _select_first_text(item: Tag, selectors: Sequence[str]) -> Optional[str]:
    """Return ``.get_text(strip=True)`` for the first match, else None."""
    for selector in selectors:
        el = item.select_one(selector)
        if el is not None:
            return el.get_text(strip=True)
    return None


def _select_attr(item: Tag, selectors: Sequence[str], *attrs: str) -> Optional[str]:
    """First match's attribute value, trying ``attrs`` in order."""
    for selector in selectors:
        el = item.select_one(selector)
        if el is None:
            continue
        for attr in attrs:
            value = el.get(attr)
            if value:
                return str(value)
    return None


def parse_card_title(item: Tag) -> str:
    """Extract a search card title, dropping placeholders."""
    title_el = item.select_one(".s-item__title, .s-item__title--has-tags, h3")
    if title_el is None:
        raw = item.get_text(separator="\n", strip=True)
        title = next(
            (line for line in raw.split("\n") if line and "Opens in" not in line),
            "",
        )
    else:
        title = title_el.get_text(strip=True)
    return title.replace("NEW LISTING", "").strip()


def parse_card_link(item: Tag) -> Tuple[str, Optional[str]]:
    """Return ``(listing_url, item_id)`` for a search card."""
    link_el = item.select_one(".s-item__link")
    if link_el is None:
        return "", None
    href = link_el.get("href", "") or ""
    return href, extract_item_id(href)


def parse_seller(item: Tag) -> Tuple[Optional[str], Optional[str]]:
    """Parse ``(seller_name, seller_rating)`` from an eBay search card."""
    seller_el = item.select_one(".s-item__seller-info")
    if seller_el is None:
        return None, None
    text = seller_el.get_text(strip=True)
    match = re.search(r"(.+?)\s*\((\d+\.?\d*)%\)", text)
    if match is None:
        return None, None
    return match.group(1).strip(), match.group(2) + "%"


def parse_bids(item: Tag) -> Optional[int]:
    """Parse the bid count out of an auction card."""
    bids_el = item.select_one(".s-item__bids, .s-item__bidCount")
    if bids_el is None:
        return None
    match = re.search(r"(\d+)", bids_el.get_text(strip=True))
    return int(match.group(1)) if match else None


@dataclass
class CardFields:
    """Flat record extracted from one eBay search card."""

    title: str
    price: str
    listing_url: str
    item_id: Optional[str]
    image_url: Optional[str]
    condition: Optional[str]
    shipping: Optional[str]
    location: Optional[str]
    seller_name: Optional[str]
    seller_rating: Optional[str]
    bids: Optional[int]
    time_left: Optional[str]


def card_to_fields(item: Tag) -> Optional[CardFields]:
    """Pull every fielded value out of one search card; None to skip."""
    title = parse_card_title(item)
    if not title or "Shop on eBay" in title:
        return None

    price_text = _select_first_text(
        item, (".s-item__price", ".s-item__price--bold"),
    )
    listing_url, item_id = parse_card_link(item)
    image_url = _select_attr(
        item, (".s-item__image-img",), "src", "data-src",
    )

    location = _select_first_text(item, (".s-item__location",))
    if location:
        location = location.replace("from ", "")

    seller_name, seller_rating = parse_seller(item)

    return CardFields(
        title=title,
        price=normalize_price(price_text or ""),
        listing_url=listing_url,
        item_id=item_id,
        image_url=image_url,
        condition=_select_first_text(item, (".s-item__subtitle", ".SECONDARY_INFO")),
        shipping=_select_first_text(item, (".s-item__shipping", ".s-item__logisticsCost")),
        location=location,
        seller_name=seller_name,
        seller_rating=seller_rating,
        bids=parse_bids(item),
        time_left=_select_first_text(item, (".s-item__time-left", ".s-item__timeEnd")),
    )


# ─────────────────────────────────────────────
# Item-detail parsers
# ─────────────────────────────────────────────

_PRICE_DETAIL_SELECTORS: Tuple[str, ...] = (
    '[data-testid="x-price-primary"]',
    "#prcIsum",
    "#mm-saleDscPrc",
    ".notranslate",
    ".u-flL.condText",
)


def parse_detail_title(soup: BeautifulSoup) -> Optional[str]:
    """Pull the eBay item title off the detail page."""
    el = soup.select_one(
        'h1[data-testid="x-item-title-label"], #itemTitle, .it-ttl'
    )
    if el is None:
        return None
    return el.get_text(strip=True).replace("Details about  \xa0", "")


def parse_detail_price(soup: BeautifulSoup) -> Optional[str]:
    """Pull the first dollar-bearing price off the detail page."""
    for selector in _PRICE_DETAIL_SELECTORS:
        el = soup.select_one(selector)
        if el is None:
            continue
        price = normalize_price(el.get_text())
        if price and "$" in price:
            return price
    return None


def parse_detail_seller(soup: BeautifulSoup) -> Dict[str, str]:
    """Pull seller name + rating from the detail-page seller block."""
    seller_el = soup.select_one(
        '[data-testid="x-seller-section"], .seller-persona'
    )
    if seller_el is None:
        return {}
    out: Dict[str, str] = {}
    name_el = seller_el.select_one(".x-seller__name, .mbg-nw")
    if name_el is not None:
        out["name"] = name_el.get_text(strip=True)
    rating_el = seller_el.select_one(".x-seller__rating, .mbg-l .mbg-id")
    if rating_el is not None:
        out["rating"] = rating_el.get_text(strip=True)
    return out


def parse_detail_images(soup: BeautifulSoup, *, limit: int = 10) -> List[str]:
    """Collect up to ``limit`` distinct full-size images from the detail page."""
    images: List[str] = []
    selector = (
        '#vi_main_img_fs img, [data-testid="x-photos"] img, .vi-image-gallery__image'
    )
    for img in soup.select(selector):
        src = img.get("src") or img.get("data-src")
        if not src or src in images:
            continue
        full = src.replace("l64.jpg", "l1600.jpg").replace("l500.jpg", "l1600.jpg")
        images.append(full)
        if len(images) >= limit:
            break
    return images


def parse_detail_specs(soup: BeautifulSoup) -> Dict[str, str]:
    """Pull the colon-delimited spec rows from the detail page."""
    specs: Dict[str, str] = {}
    rows = soup.select(
        '.u-flL.condText, .itemAttr td, [data-testid="x-item-detail"] tr'
    )
    for row in rows:
        text = row.get_text(strip=True)
        if ":" not in text:
            continue
        parts = text.split(":", 1)
        if len(parts) == 2:
            specs[parts[0].strip()] = parts[1].strip()
    return specs


def _parse_int_in_element(soup: BeautifulSoup, selector: str) -> Optional[int]:
    """Return the first integer found inside ``selector`` or ``None``."""
    el = soup.select_one(selector)
    if el is None:
        return None
    text = el.get_text().replace(",", "")
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def parse_detail_sold_count(soup: BeautifulSoup) -> Optional[int]:
    """Sold count widget."""
    return _parse_int_in_element(
        soup, '.vi-qtyS-hot-red, [data-testid="x-sold-count"]',
    )


def parse_detail_watchers(soup: BeautifulSoup) -> Optional[int]:
    """Watchers count widget."""
    return _parse_int_in_element(
        soup, '#vi-bybox-watchers, [data-testid="x-watchers-count"]',
    )


def parse_detail_text(soup: BeautifulSoup, selector: str) -> Optional[str]:
    """Generic ``soup.select_one(selector).get_text(strip=True)``."""
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el is not None else None


# ─────────────────────────────────────────────
# Playwright field probes (each tries one selector list)
# ─────────────────────────────────────────────

InnerTextProbe = Callable[[], Awaitable[Optional[str]]]


async def _safe_inner_text(page: Any, selectors: str) -> Optional[str]:
    """Best-effort inner_text for the first matching selector list."""
    try:
        el = await page.query_selector(selectors)
    except SELECTOR_EXC as exc:
        logger.debug("query_selector(%s) failed (%s): %s", selectors, type(exc).__name__, exc)
        return None
    if el is None:
        return None
    try:
        text = await el.inner_text()
    except SELECTOR_EXC as exc:
        logger.debug("inner_text(%s) failed (%s): %s", selectors, type(exc).__name__, exc)
        return None
    return text.strip() if text else None


_PRIVATE_HELPERS = (_safe_inner_text,)
