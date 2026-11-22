"""Etsy scraper helpers extracted from ``etsy.py``.

Module split: per-field BeautifulSoup probes and the HTTP fetch
helper live here so the public ``etsy.py`` orchestrators stay below the
fn_length / cyclomatic / cognitive thresholds.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.etsy.helpers")

ETSY_BASE_URL = "https://www.etsy.com"

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

PARSE_EXC: Tuple[type[BaseException], ...] = ELEMENT_PROBE_EXCEPTIONS


# ─────────────────────────────────────────────
# Generic field extraction
# ─────────────────────────────────────────────

def extract_rating(text: Optional[str]) -> Optional[float]:
    """Pull the first decimal out of a rating-like string."""
    if not text:
        return None
    match = re.search(r"([\d.]+)", text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def extract_int(text: Optional[str]) -> Optional[int]:
    """Pull the first integer (commas tolerated) out of free text."""
    if not text:
        return None
    match = re.search(r"([\d,]+)", text)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


@dataclass(frozen=True)
class HttpFetchResult:
    """Outcome of an HTTP GET against an Etsy URL."""

    ok: bool
    text: str = ""
    error: Optional[str] = None


def fetch_html(url: str, *, timeout: float = 30.0) -> HttpFetchResult:
    """GET ``url`` -> body text or a typed error result."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=DEFAULT_HEADERS)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("HTTP error fetching Etsy URL: %s", exc)
        return HttpFetchResult(ok=False, error=str(exc))
    return HttpFetchResult(ok=True, text=resp.text)


def select_first_text(tag: Tag, selectors: Sequence[str]) -> Optional[str]:
    """Stripped text from the first matching selector."""
    for selector in selectors:
        el = tag.select_one(selector)
        if el is not None:
            return el.get_text(strip=True)
    return None


def select_text(tag: Tag, selector: str) -> Optional[str]:
    """``select_one(selector).get_text(strip=True)`` without the None dance."""
    el = tag.select_one(selector)
    return el.get_text(strip=True) if el is not None else None


# ─────────────────────────────────────────────
# Search URL builder + sort map
# ─────────────────────────────────────────────

_SORT_MAP: Dict[str, str] = {
    "relevance": "",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "newest": "created_desc",
}


def build_search_url(
    query: str,
    min_price: Optional[float],
    max_price: Optional[float],
    sort: str,
) -> str:
    """Build an Etsy /search URL with optional price + sort filters."""
    base_url = f"{ETSY_BASE_URL}/search?q={quote(query)}"
    parts: List[str] = []
    if min_price is not None:
        parts.append(f"min_price={min_price}")
    if max_price is not None:
        parts.append(f"max_price={max_price}")
    sort_value = _SORT_MAP.get(sort, "")
    if sort_value:
        parts.append(f"sort_order={sort_value}")
    if not parts:
        return base_url
    return f"{base_url}&{'&'.join(parts)}"


# ─────────────────────────────────────────────
# Search card extractors
# ─────────────────────────────────────────────

CARD_SELECTORS: Tuple[str, ...] = (
    ".listing-card",
    "[data-listing-id]",
    ".v2-listing-card",
    ".wt-list-unstyled .wt-display-block",
)


def select_cards(soup: BeautifulSoup) -> List[Tag]:
    """First non-empty selector list of search cards on the page."""
    for selector in CARD_SELECTORS:
        items = soup.select(selector)
        if items:
            return items
    return []


@dataclass
class SearchCardFields:
    """Flat field record for a single Etsy search card."""

    title: str
    price: str
    original_price: Optional[str]
    shop_name: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    listing_url: str
    image_url: Optional[str]
    free_shipping: bool
    made_to_order: bool


def _card_image_url(item: Tag) -> Optional[str]:
    """Return the first image URL from listing/card image tags."""
    img = item.select_one("img.listing-image, img.v2-listing-card__img")
    if img is None:
        return None
    return img.get("data-src") or img.get("src")


def _card_listing_url(item: Tag) -> str:
    """Build the absolute listing URL from the card link."""
    link = item.select_one(
        'a.listing-link, a.v2-listing-card__overlay, a[href*="/listing/"]',
    )
    if link is None:
        return ""
    return urljoin(ETSY_BASE_URL, link.get("href", ""))


def _card_rating(item: Tag) -> Optional[float]:
    """Extract a numeric rating from a search card."""
    rating_el = item.select_one(".stars-smaller, .rating, [data-rating]")
    if rating_el is None:
        return None
    rating_text = rating_el.get("data-rating") or rating_el.get_text(strip=True)
    return extract_rating(rating_text)


def card_to_fields(item: Tag) -> Optional[SearchCardFields]:
    """Pull every fielded value off a search card; None to skip."""
    title = select_first_text(
        item,
        (".v2-listing-card__title", ".listing-title", "h2 a", ".title"),
    ) or ""
    if not title:
        return None
    price = select_first_text(
        item,
        (".v2-listing-card__price", ".price", ".currency-value"),
    ) or ""
    return SearchCardFields(
        title=title,
        price=price,
        original_price=select_first_text(
            item, (".v2-listing-card__original-price", ".original-price"),
        ),
        shop_name=select_first_text(
            item, (".v2-listing-card__shop", ".shop-name", ".seller-name"),
        ),
        rating=_card_rating(item),
        review_count=extract_int(select_text(item, ".review-count, .reviews") or ""),
        listing_url=_card_listing_url(item),
        image_url=_card_image_url(item),
        free_shipping=item.select_one('.free-shipping, [data-shipping="free"]') is not None,
        made_to_order=item.select_one('.made-to-order, [data-made-to-order]') is not None,
    )


# ─────────────────────────────────────────────
# Listing detail extractors
# ─────────────────────────────────────────────

def parse_listing_title(soup: BeautifulSoup) -> str:
    """Title for a single Etsy listing page."""
    return select_first_text(
        soup,
        (
            "h1[data-buy-box-listing-title]",
            "h1.show-transcript",
            "#listing-title",
            "h1",
        ),
    ) or ""


def parse_listing_images(soup: BeautifulSoup) -> List[str]:
    """Distinct full-size image URLs on a listing page."""
    images: List[str] = []
    for img in soup.select(
        "img[data-src-zoom-image], img[data-carousel-image], .listing-page-image img",
    ):
        src = (
            img.get("data-src-zoom-image")
            or img.get("data-carousel-image")
            or img.get("src")
        )
        if src and "etsy" in src and src not in images:
            images.append(src)
    return images


def parse_listing_materials(soup: BeautifulSoup) -> List[str]:
    """Materials list off a listing page."""
    materials_block = soup.select_one("#materials-section, .materials-list")
    if materials_block is None:
        return []
    return [li.get_text(strip=True) for li in materials_block.select("li")]


def parse_listing_tags(soup: BeautifulSoup) -> List[str]:
    """Tag chips off a listing page."""
    tags: List[str] = []
    for tag in soup.select(".listing-tag, .tag, [data-tag]"):
        text = tag.get_text(strip=True)
        if text:
            tags.append(text)
    return tags


def _parse_review_block(rev: Tag) -> Dict[str, Any]:
    """Pull author/rating/text/date from a single review block."""
    rating: Optional[int] = None
    rating_text = select_first_text(rev, (".review-rating", ".stars"))
    if rating_text is not None:
        rating_value = extract_rating(rating_text)
        if rating_value is not None:
            rating = int(rating_value)
    return {
        "author": select_first_text(rev, (".reviewer-name", ".author")),
        "rating": rating,
        "text": select_first_text(rev, (".review-text", ".content")),
        "date": select_first_text(rev, (".review-date", ".date")),
    }


def parse_listing_reviews(soup: BeautifulSoup, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Collect up to ``limit`` review dicts off a listing page."""
    out: List[Dict[str, Any]] = []
    for rev in soup.select(".review, .listing-page-review")[:limit]:
        try:
            out.append(_parse_review_block(rev))
        except PARSE_EXC as exc:
            logger.debug("Suppressed review parse error (%s): %s", type(exc).__name__, exc)
    return out


# ─────────────────────────────────────────────
# Shop detail extractors
# ─────────────────────────────────────────────

def _parse_shop_listing(listing: Tag) -> Optional[Dict[str, Any]]:
    """Pull title/price/url/image off one shop listing card."""
    try:
        link = listing.select_one('a[href*="/listing/"]')
        img = listing.select_one("img")
        return {
            "title": select_first_text(listing, (".title", ".listing-title")),
            "price": select_first_text(listing, (".price", ".listing-price")),
            "listing_url": (
                urljoin(ETSY_BASE_URL, link.get("href", "")) if link is not None else None
            ),
            "image_url": img.get("src") if img is not None else None,
        }
    except PARSE_EXC as exc:
        logger.debug("Suppressed shop listing parse error (%s): %s", type(exc).__name__, exc)
        return None


def parse_shop_listings(soup: BeautifulSoup, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Top-N listing dicts from a shop page."""
    out: List[Dict[str, Any]] = []
    for listing in soup.select(".listing-card, .shop-listing")[:limit]:
        parsed = _parse_shop_listing(listing)
        if parsed is not None:
            out.append(parsed)
    return out
