"""Metadata extraction: title, description, OG tags, canonical, favicon."""
from __future__ import annotations

import logging
from typing import Any, Dict
from urllib.parse import urljoin

from ._utils_html import BS4_AVAILABLE, BeautifulSoup

_BS4_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, IndexError, LookupError,
)

logger = logging.getLogger(__name__)


METADATA_SELECTORS = {
    "title": 'meta[name="title"]',
    "description": 'meta[name="description"]',
    "og_title": 'meta[property="og:title"]',
    "og_description": 'meta[property="og:description"]',
    "og_image": 'meta[property="og:image"]',
    "og_url": 'meta[property="og:url"]',
    "canonical": 'link[rel="canonical"]',
    "favicon": 'link[rel="icon"], link[rel="shortcut icon"]',
    "author": 'meta[name="author"]',
}


_METADATA_INITIAL: Dict[str, Any] = {
    "title": None, "description": None, "og_title": None,
    "og_description": None, "og_image": None, "og_url": None,
    "canonical": None, "favicon": None, "author": None,
}
_METADATA_ABSOLUTISE_KEYS = ("canonical", "og_url", "og_image")


def _assign_metadata_value(
    metadata: Dict[str, Any],
    key: str,
    elem: Any,
    base_url: str,
) -> None:
    """Assign one metadata field from a parsed BS4 element."""
    if key == "title":
        metadata[key] = elem.get_text(strip=True)
        return
    if key == "canonical":
        metadata[key] = elem.get("href")
        return
    if key == "favicon":
        href = elem.get("href")
        metadata[key] = urljoin(base_url, href) if href else None
        return
    metadata[key] = elem.get("content")


def _populate_metadata_meta_tags(
    metadata: Dict[str, Any],
    soup: "BeautifulSoup",
    base_url: str,
) -> None:
    """Walk :data:`METADATA_SELECTORS`, write each match into ``metadata``."""
    for key, selector in METADATA_SELECTORS.items():
        try:
            elem = soup.select_one(selector)
        except _BS4_PARSE_EXCEPTIONS as exc:
            logger.debug("metadata select_one suppressed: %s", exc)
            continue
        if elem is not None:
            _assign_metadata_value(metadata, key, elem, base_url)


def _backfill_title(metadata: Dict[str, Any], soup: "BeautifulSoup") -> None:
    """If meta tags didn't set title, fall back to ``<title>``."""
    if metadata["title"]:
        return
    title_tag = soup.find("title")
    if title_tag is not None:
        metadata["title"] = title_tag.get_text(strip=True)


def _absolutise_metadata_urls(metadata: Dict[str, Any], base_url: str) -> None:
    """Resolve canonical / og_url / og_image against ``base_url``."""
    for key in _METADATA_ABSOLUTISE_KEYS:
        value = metadata.get(key)
        if value:
            metadata[key] = urljoin(base_url, value)


def extract_metadata(html: str, base_url: str) -> Dict[str, Any]:
    """Extract page metadata (title, description, OG tags, canonical, favicon)."""
    if not BS4_AVAILABLE or not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    metadata: Dict[str, Any] = dict(_METADATA_INITIAL)
    _populate_metadata_meta_tags(metadata, soup, base_url)
    _backfill_title(metadata, soup)
    _absolutise_metadata_urls(metadata, base_url)
    return metadata
