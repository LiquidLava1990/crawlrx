"""Per-field DOM extractors for Facebook Marketplace.

Operates on Playwright handles via ``safe_extract_async``; never lets a
selector probe propagate as a bare ``Exception``. Used by both
``_facebook_marketplace`` (search results) and ``_facebook_listing``
(single-listing pages).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import parse_qs, urlencode, urlparse

from ._scraper_helpers import safe_extract_async

logger = logging.getLogger("crawlrx.facebook.extractors")

FB_BASE_URL = "https://www.facebook.com"
FB_MARKETPLACE_URL = f"{FB_BASE_URL}/marketplace"


# ---------------------------------------------------------------------------
# URL helpers.
# ---------------------------------------------------------------------------

def extract_listing_id_from_url(url: str) -> Optional[str]:
    """Pull a listing id out of `/marketplace/item/<id>` or `?listing_id=<id>`."""
    match = re.search(r"/marketplace/item/(\d+)", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    listing_id = params.get("listing_id")
    if listing_id:
        return listing_id[0]
    return None


def build_marketplace_search_url(
    query: str,
    location: Optional[str] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
) -> str:
    """Build a Facebook Marketplace search URL."""
    # Location-specific path is intentionally omitted: FB's URL scheme is
    # opaque and changes frequently; we rely on the search query instead.
    del location  # currently unused; kept in signature for parity.
    params: dict[str, str | int] = {"query": query}
    if min_price is not None:
        params["minPrice"] = min_price
    if max_price is not None:
        params["maxPrice"] = max_price
    return f"{FB_MARKETPLACE_URL}/search?{urlencode(params)}"


def normalise_listing_url(href: str) -> Optional[str]:
    """Promote a relative `/marketplace/item/...` href to absolute."""
    if not href or "/marketplace/item/" not in href:
        return None
    if href.startswith("http"):
        return href
    return f"{FB_BASE_URL}{href}"


# ---------------------------------------------------------------------------
# Generic probe helpers.
# ---------------------------------------------------------------------------

async def _query_all(page: Any, selector: str) -> list[Any]:
    """Return all matches as a list (defaulting to empty on failure)."""
    elements = await safe_extract_async(
        lambda: page.query_selector_all(selector),
        field=f"query_all:{selector}",
        default=[],
    )
    return list(elements or [])


async def _text_content(element: Any) -> Optional[str]:
    """Try ``text_content`` then ``inner_text``; return None on failure."""
    if element is None:
        return None
    text = await safe_extract_async(element.text_content, field="text_content")
    if isinstance(text, str) and text:
        return text
    inner = await safe_extract_async(element.inner_text, field="inner_text")
    if isinstance(inner, str):
        return inner
    return None


async def _attr(element: Any, name: str) -> Optional[str]:
    """``element.get_attribute(name)`` swallowing probe errors."""
    if element is None:
        return None
    value = await safe_extract_async(
        lambda: element.get_attribute(name),
        field=f"attr:{name}",
    )
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Marketplace card extraction (search results).
# ---------------------------------------------------------------------------

LOCATION_KEYWORDS = ("mi", "km", "miles")
DATE_KEYWORDS = ("ago", "hour", "day", "week", "minute")


def _is_price_line(line: str) -> bool:
    return line.startswith("$")


def _line_is_numeric(line: str) -> bool:
    return line.replace(",", "").replace(".", "").isdigit()


def _first_match(lines: list[str], predicate) -> Optional[str]:
    """Return the first line where ``predicate(line)`` is True."""
    for line in lines:
        if predicate(line):
            return line
    return None


def parse_card_lines(card_text: str) -> dict[str, Optional[str]]:
    """Pull title/price/location/posted_date from a card's flattened text."""
    lines = [ln.strip() for ln in card_text.split("\n") if ln.strip()]
    title = _first_match(
        lines,
        lambda line: not _is_price_line(line) and not _line_is_numeric(line),
    )
    price = _first_match(lines, lambda line: "$" in line)
    location = _first_match(
        lines,
        lambda line: "," in line or any(
            keyword in line.lower() for keyword in LOCATION_KEYWORDS
        ),
    )
    posted = _first_match(
        lines,
        lambda line: any(
            keyword in line.lower() for keyword in DATE_KEYWORDS
        ),
    )
    return {
        "title": (title or "")[:200] or None,
        "price": (price or "")[:50] or None,
        "location": (location or "")[:100] or None,
        "posted_date": (posted or "")[:50] or None,
    }


async def extract_card_text(card: "ElementHandle") -> str:
    """Best-effort flattened text for a card element."""
    text = await safe_extract_async(
        lambda: card.inner_text() if hasattr(card, "inner_text") else None,
        field="card_inner_text",
    )
    if isinstance(text, str) and text:
        return text
    fallback = await safe_extract_async(
        card.text_content,
        field="card_text_content",
    )
    return fallback if isinstance(fallback, str) else ""


async def extract_card_image(card: "ElementHandle") -> Optional[str]:
    """First non-data image src on a card."""
    img = await safe_extract_async(
        lambda: card.query_selector("img"),
        field="card_img",
    )
    src = await _attr(img, "src")
    if not src or src.startswith("data:"):
        return None
    return src


# ---------------------------------------------------------------------------
# Single-listing extraction.
# ---------------------------------------------------------------------------

LISTING_TITLE_SELECTORS = (
    "h1",
    "[role='main'] h1",
    "span[role='heading']",
)

LISTING_DESC_SELECTORS = (
    "[role='main'] div[dir='auto']",
    "[data-pagelet='root'] div[dir='auto']",
)

LISTING_PRICE_FALLBACK_SELECTORS = (
    "span:has-text('$')",
    "[role='main'] span",
)

LISTING_SELLER_SELECTORS = (
    "a[href*='/marketplace/profile/']",
    "[role='main'] a[href*='profile']",
)

LISTING_CATEGORY_SELECTORS = (
    "a[href*='/marketplace/category/']",
)

LISTING_IMAGE_SELECTORS = (
    "img",
    "[role='main'] img",
)

CONDITION_KEYWORDS = (
    "new",
    "used - like new",
    "used - good",
    "used - fair",
)

POSTED_DATE_KEYWORDS = ("ago", "listed", "posted")
LOCATION_LINE_KEYWORDS = ("mi", "km", "miles", "away")


async def extract_listing_title(page: "Page") -> Optional[str]:
    """First non-empty title across the candidate selectors."""
    for selector in LISTING_TITLE_SELECTORS:
        element = await safe_extract_async(
            lambda sel=selector: page.query_selector(sel),
            field=f"title:{selector}",
        )
        text = await _text_content(element)
        if text and len(text.strip()) > 2:
            return text.strip()[:300]
    return None


async def _price_from_page_text(page_text: str) -> Optional[str]:
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", page_text or "")
    return match.group(0) if match else None


async def _price_from_selectors(page: Any) -> Optional[str]:
    for selector in LISTING_PRICE_FALLBACK_SELECTORS:
        elements = await _query_all(page, selector)
        for element in elements:
            text = await _text_content(element)
            if text and text.startswith("$") and len(text) < 20:
                return text.strip()
    return None


async def extract_listing_price(page: "Page", page_text: str) -> Optional[str]:
    primary = await _price_from_page_text(page_text)
    if primary:
        return primary
    return await _price_from_selectors(page)


async def extract_listing_description(page: "Page") -> Optional[str]:
    for selector in LISTING_DESC_SELECTORS:
        elements = await _query_all(page, selector)
        for element in elements:
            text = await _text_content(element)
            if text and len(text.strip()) > 50:
                return text.strip()[:2000]
    return None


async def extract_listing_seller(page: "Page") -> tuple[Optional[str], Optional[str]]:
    """Return ``(seller_name, seller_url)`` or both None."""
    for selector in LISTING_SELLER_SELECTORS:
        element = await safe_extract_async(
            lambda sel=selector: page.query_selector(sel),
            field=f"seller:{selector}",
        )
        if not element:
            continue
        text = await _text_content(element)
        href = await _attr(element, "href")
        if not text or not (0 < len(text.strip()) < 100):
            continue
        seller_name = text.strip()[:100]
        seller_url = _normalise_seller_href(href)
        return (seller_name, seller_url)
    return (None, None)


def _normalise_seller_href(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("/"):
        return f"{FB_BASE_URL}{href}"
    if href.startswith("http"):
        return href
    return None


def find_location_line(page_text: str) -> Optional[str]:
    """Return the first line that looks like ``City, ST · 5 mi away``."""
    for raw_line in (page_text or "").split("\n"):
        line = raw_line.strip()
        if "," not in line:
            continue
        if not any(keyword in line.lower() for keyword in LOCATION_LINE_KEYWORDS):
            continue
        return line[:100]
    return None


def find_condition(page_text: str) -> Optional[str]:
    lowered = (page_text or "").lower()
    for keyword in CONDITION_KEYWORDS:
        if keyword in lowered:
            return keyword.title()
    return None


def find_posted_date(page_text: str) -> Optional[str]:
    for raw_line in (page_text or "").split("\n"):
        line = raw_line.strip()
        if any(keyword in line.lower() for keyword in POSTED_DATE_KEYWORDS):
            return line[:100]
    return None


async def extract_listing_category(page: "Page") -> Optional[str]:
    for selector in LISTING_CATEGORY_SELECTORS:
        element = await safe_extract_async(
            lambda sel=selector: page.query_selector(sel),
            field=f"category:{selector}",
        )
        text = await _text_content(element)
        if text and text.strip():
            return text.strip()[:50]
    return None


async def extract_listing_images(page: "Page", max_count: int = 10) -> list[str]:
    """Collect up to ``max_count`` distinct ``fbcdn.net`` image urls."""
    seen: set[str] = set()
    images: list[str] = []
    for selector in LISTING_IMAGE_SELECTORS:
        elements = await _query_all(page, selector)
        for element in elements:
            src = await _attr(element, "src")
            if not src or src.startswith("data:") or "fbcdn.net" not in src:
                continue
            if src in seen:
                continue
            seen.add(src)
            images.append(src)
            if len(images) >= max_count:
                return images
    return images


async def get_page_text(page: "Page") -> str:
    """Return ``page.text_content()`` as a string (empty on failure)."""
    text = await safe_extract_async(page.text_content, field="page_text")
    return text if isinstance(text, str) else ""
