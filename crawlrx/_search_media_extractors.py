"""Per-card extractors for Bing Images / News / Videos (split from search_media)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional, Tuple

from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS
from ._search_helpers import SERP_BROWSER_EXCEPTIONS
from ._search_media_runner import collect_cards_via_selectors

logger = logging.getLogger("crawlrx.search_media.extractors")

Page = Any
Card = Any

_BING_HOST = "https://www.bing.com"


# ─────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────

def _absolute(url: Optional[str]) -> str:
    if not url:
        return ""
    return _BING_HOST + url if url.startswith("/") else url


async def _read_inner_text(card: Card, selectors: Tuple[str, ...]) -> Optional[str]:
    for selector in selectors:
        elem = await card.query_selector(selector)
        if not elem:
            continue
        try:
            return (await elem.inner_text()).strip()
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("inner_text failed: %s", type(exc).__name__)
            continue
    return None


async def _safe_get_attribute(elem: Card, attr: str) -> Optional[str]:
    """Read ``attr`` from ``elem`` swallowing selector-extraction errors."""
    try:
        return await elem.get_attribute(attr)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("get_attribute failed: %s", type(exc).__name__)
        return None


async def _first_present_attr(elem: Card, attrs: Tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty attribute on ``elem`` from ``attrs``."""
    for attr in attrs:
        value = await _safe_get_attribute(elem, attr)
        if value:
            return value
    return None


async def _first_attr(
    card: Card, selectors: Tuple[str, ...], attrs: Tuple[str, ...],
) -> Optional[str]:
    for selector in selectors:
        elem = await card.query_selector(selector)
        if not elem:
            continue
        value = await _first_present_attr(elem, attrs)
        if value:
            return value
    return None


async def _try_extract(card: Card, fn) -> Optional[dict]:
    try:
        return await fn(card)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Card extraction failed: %s", type(exc).__name__)
        return None


# ─────────────────────────────────────────────
# Images
# ─────────────────────────────────────────────

_IMAGE_CARD_SELECTORS: Tuple[str, ...] = (
    ".iusc", ".img_cont", "[data-idx] .mimg", ".dg_b img",
)
_IMAGE_WAIT_SELECTORS = [".iusc", ".img_cont", "[data-idx]"]


_IMAGE_THUMB_ATTRS: Tuple[str, ...] = ("src", "data-src", "data-thumbnail-url", "href")


async def _image_thumbnail(card: Card) -> str:
    # The "card" itself is the image element in some selector paths -- read its
    # own attributes first, then fall back to a child via ``.`` selector.
    direct = await _first_present_attr(card, _IMAGE_THUMB_ATTRS)
    if direct:
        return direct
    fallback = await _first_attr(card, (".",), _IMAGE_THUMB_ATTRS)
    return fallback or ""


async def _image_title(card: Card) -> str:
    own = await _first_present_attr(card, ("alt", "title"))
    if own:
        return own
    parent = await card.query_selector("..")
    if not parent:
        return ""
    parent_value = await _first_present_attr(parent, ("title", "aria-label"))
    return parent_value or ""


async def _image_url_from_anchor(card: Card) -> Optional[str]:
    """Look up an ``<a>`` child of ``card`` and return its ``href``."""
    anchor = await card.query_selector("a")
    if not anchor:
        return None
    return await _safe_get_attribute(anchor, "href")


async def _image_url(card: Card) -> str:
    href = await _safe_get_attribute(card, "href")
    if not href:
        href = await _image_url_from_anchor(card)
    return _absolute(href)


async def _image_record(card: Card) -> Optional[dict]:
    thumb = await _image_thumbnail(card)
    if not thumb:
        return None
    url = await _image_url(card)
    if not url:
        return None
    return {
        "thumbnail": thumb,
        "title": await _image_title(card),
        "url": url,
    }


_IMAGE_JSON_SCRIPT = """
() => {
    const scripts = document.querySelectorAll('script');
    for (const script of scripts) {
        const text = script.textContent || '';
        if (text.includes('murl') || text.includes('mediaurl')) {
            return text;
        }
    }
    return null;
}
"""


def _image_records_from_json(json_data: str, max_results: int) -> List[dict]:
    matches = re.findall(r'murl":"([^"]+)"', json_data)
    titles = re.findall(r'title":"([^"]+)"', json_data)
    out: List[dict] = []
    for i, murl in enumerate(matches[:max_results]):
        out.append({
            "title": titles[i] if i < len(titles) else "",
            "url": murl,
            "thumbnail": murl,
        })
    return out


async def _images_from_embedded_json(page: Page, max_results: int) -> List[dict]:
    try:
        json_data = await page.evaluate(_IMAGE_JSON_SCRIPT)
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.debug("JSON fallback evaluate failed: %s", type(exc).__name__)
        return []
    if not json_data:
        return []
    try:
        return _image_records_from_json(json_data, max_results)
    except (re.error, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.debug("JSON regex/decode failed: %s", type(exc).__name__)
        return []


async def extract_images(page: Page, max_results: int) -> List[dict]:
    """Extract image cards from a Bing Images page."""
    cards = await collect_cards_via_selectors(page, _IMAGE_WAIT_SELECTORS)
    images: List[dict] = []
    for card in cards[:max_results]:
        record = await _try_extract(card, _image_record)
        if record:
            images.append(record)
    if images:
        return images
    return await _images_from_embedded_json(page, max_results)


# ─────────────────────────────────────────────
# News
# ─────────────────────────────────────────────

_NEWS_WAIT_SELECTORS = [".news-card", ".nc_cn", "[data-tag='news']"]
_NEWS_CARD_SELECTORS: Tuple[str, ...] = (
    ".news-card", ".nc_cn", "[data-tag='news']",
    ".b_algo", ".newsitm", ".newscard",
)
_NEWS_TITLE_SELECTORS: Tuple[str, ...] = (
    "a.title", ".title", "h2 a", "a[href]:not([class])",
)
_NEWS_SNIPPET_SELECTORS: Tuple[str, ...] = (
    ".snippet", ".desc", ".content", "p",
)
_NEWS_DATE_SELECTORS: Tuple[str, ...] = (
    ".source .time", ".time", ".date", "[aria-label*='ago']", "span[title]",
)


async def _news_title_and_url(card: Card) -> Tuple[str, str]:
    title: str = ""
    url: str = ""
    for selector in _NEWS_TITLE_SELECTORS:
        elem = await card.query_selector(selector)
        if not elem:
            continue
        try:
            title = (await elem.inner_text()).strip()
            href = await elem.get_attribute("href")
        except SELECTOR_EXTRACTION_EXCEPTIONS:
            continue
        url = _absolute(href)
        if title and url:
            break
    return title, url


async def _news_record(card: Card) -> Optional[dict]:
    title, url = await _news_title_and_url(card)
    if not title or not url:
        return None
    snippet = await _read_inner_text(card, _NEWS_SNIPPET_SELECTORS) or ""
    date = await _read_inner_text(card, _NEWS_DATE_SELECTORS) or ""
    return {"title": title, "url": url, "snippet": snippet, "published": date}


async def extract_news(page: Page, max_results: int) -> List[dict]:
    """Extract news cards from a Bing News page."""
    cards = await collect_cards_via_selectors(page, _NEWS_WAIT_SELECTORS)
    out: List[dict] = []
    for card in cards[:max_results]:
        record = await _try_extract(card, _news_record)
        if record:
            out.append(record)
    return out


# ─────────────────────────────────────────────
# Videos
# ─────────────────────────────────────────────

_VIDEO_WAIT_SELECTORS = [".mc_vtvc", ".b_algo", "[data-idx]"]
_VIDEO_TITLE_SELECTORS: Tuple[str, ...] = (
    ".title", ".mc_vtvc_title", "a[href]", "h3",
)
_VIDEO_DURATION_SELECTORS: Tuple[str, ...] = (
    ".mc_bc_ctp", ".duration", ".time", "span[title*=':']",
)


async def _video_url(card: Card) -> str:
    elem = await card.query_selector("a[href]")
    if not elem:
        return ""
    href = await _safe_get_attribute(elem, "href")
    return _absolute(href)


async def _video_thumbnail(card: Card) -> str:
    elem = await card.query_selector("img[src], img[data-src]")
    if not elem:
        return ""
    value = await _first_present_attr(elem, ("src", "data-src"))
    return value or ""


async def _video_record(card: Card) -> Optional[dict]:
    title = await _read_inner_text(card, _VIDEO_TITLE_SELECTORS) or ""
    if not title:
        return None
    url = await _video_url(card)
    if not url:
        return None
    duration = await _read_inner_text(card, _VIDEO_DURATION_SELECTORS) or ""
    return {
        "title": title,
        "url": url,
        "thumbnail": await _video_thumbnail(card),
        "duration": duration,
    }


async def extract_videos(page: Page, max_results: int) -> List[dict]:
    """Extract video cards from a Bing Videos page."""
    cards = await collect_cards_via_selectors(page, _VIDEO_WAIT_SELECTORS)
    out: List[dict] = []
    for card in cards[:max_results]:
        record = await _try_extract(card, _video_record)
        if record:
            out.append(record)
    return out
