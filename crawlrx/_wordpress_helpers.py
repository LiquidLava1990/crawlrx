"""Helpers for the WordPress scraper.

Split out of ``wordpress.py`` for readability:
- The 9 / 8-positional-arg ``__init__`` constructors are replaced by
  ``@dataclass`` definitions here (the parameter-count limit gone).
- The deeply-nested ``_parse_api_post*`` helpers move here so the
  routing module stays light.
- The DOM scrape paths receive a shared ``probe_first_match`` helper
  that compresses the duplicated selector loops.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin, urlparse

from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx.wordpress._helpers")


# ---------------------------------------------------------------------------
# Models -- dataclasses replace the previous 9-/8-positional `__init__`
# ---------------------------------------------------------------------------

_COMMON_POST_FIELDS = (
    "title", "url", "date", "author", "featured_image",
)


def _common_post_dict(obj: Any) -> Dict[str, Any]:
    """Shared mapping for the fields ``WordPressPost`` and ...Detail share."""
    out: Dict[str, Any] = {name: getattr(obj, name) for name in _COMMON_POST_FIELDS}
    out["categories"] = list(obj.categories)
    out["tags"] = list(obj.tags)
    return out


@dataclass
class WordPressPost:
    """Row in a WordPress posts list."""

    id: Any
    title: str
    url: str
    excerpt: str = ""
    date: str = ""
    author: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    featured_image: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-shaped dict for the FastAPI response."""
        out = _common_post_dict(self)
        out["id"] = self.id
        out["excerpt"] = self.excerpt
        return out


@dataclass
class WordPressPostDetail:
    """Detail body for a single WordPress post."""

    title: str = ""
    url: str = ""
    content: str = ""
    date: str = ""
    author: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    featured_image: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-shaped dict for the FastAPI response."""
        out = _common_post_dict(self)
        out["content"] = self.content
        return out


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Strip trailing slashes + REST/feed suffixes from a WordPress URL."""
    cleaned = url.strip().rstrip("/")
    for suffix in ("/wp-json", "/feed", "/rss.xml"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
    return cleaned


def extract_slug_from_url(url: str) -> Optional[str]:
    """Best-effort slug = last non-empty path segment, sans extension."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None
    slug = parts[-1]
    if "." in slug:
        slug = slug.split(".")[0]
    return slug or None


def resolve_url(base: str, path: str) -> str:
    """Make ``path`` absolute against ``base`` if needed."""
    if path.startswith(("http://", "https://")):
        return path
    return urljoin(base, path)


# ---------------------------------------------------------------------------
# REST-API JSON helpers
# ---------------------------------------------------------------------------

def _rendered_or_str(value: Any) -> str:
    """``value["rendered"]`` if dict-shaped, else ``str(value)``."""
    if isinstance(value, dict):
        return value.get("rendered", "")
    return str(value or "")


def _api_excerpt_from(post: Dict[str, Any]) -> str:
    """Strip HTML tags from the post excerpt."""
    raw = _rendered_or_str(post.get("excerpt", ""))
    return re.sub(r"<[^>]+>", "", raw)


def _api_link_for(post: Dict[str, Any], base_url: str) -> str:
    """Resolve the post's permalink, falling back to ``?p=ID``."""
    url = post.get("link", "")
    if url:
        return url
    if post.get("id"):
        return f"{base_url}/?p={post['id']}"
    return ""


def _api_featured_image(post: Dict[str, Any]) -> Optional[str]:
    """Resolve ``_embedded.wp:featuredmedia[0].source_url`` (or guid fallback)."""
    embedded = post.get("_embedded") or {}
    media = embedded.get("wp:featuredmedia") or []
    if not media:
        return None
    first = media[0] or {}
    source_url = first.get("source_url")
    if source_url:
        return source_url
    guid = first.get("guid") or {}
    return guid.get("rendered") if isinstance(guid, dict) else None


def _api_author_name(post: Dict[str, Any]) -> str:
    """Resolve ``_embedded.author[0].name``."""
    embedded = post.get("_embedded") or {}
    authors = embedded.get("author") or []
    if not authors:
        return ""
    return (authors[0] or {}).get("name", "")


def _flatten_terms(post: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Walk ``_embedded.wp:term`` lists into one flat term iterator."""
    embedded = post.get("_embedded") or {}
    for term_list in embedded.get("wp:term") or []:
        for term in term_list or []:
            if term:
                yield term


def _api_terms(post: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Split ``_embedded.wp:term`` into (categories, tags)."""
    categories: List[str] = []
    tags: List[str] = []
    for term in _flatten_terms(post):
        taxonomy = term.get("taxonomy")
        name = term.get("name", "")
        if taxonomy == "category":
            categories.append(name)
        elif taxonomy == "post_tag":
            tags.append(name)
    return categories, tags


def parse_api_post(post: Dict[str, Any], base_url: str) -> WordPressPost:
    """Map a REST-API post payload to ``WordPressPost``."""
    categories, tags = _api_terms(post)
    return WordPressPost(
        id=post.get("id", 0),
        title=_rendered_or_str(post.get("title", "")),
        url=_api_link_for(post, base_url),
        excerpt=_api_excerpt_from(post),
        date=post.get("date", ""),
        author=_api_author_name(post),
        categories=categories,
        tags=tags,
        featured_image=_api_featured_image(post),
    )


def parse_api_post_detail(
    post: Dict[str, Any], base_url: str,
) -> WordPressPostDetail:
    """Map a REST-API post payload to ``WordPressPostDetail``."""
    categories, tags = _api_terms(post)
    return WordPressPostDetail(
        title=_rendered_or_str(post.get("title", "")),
        url=_api_link_for(post, base_url),
        content=_rendered_or_str(post.get("content", "")),
        date=post.get("date", ""),
        author=_api_author_name(post),
        categories=categories,
        tags=tags,
        featured_image=_api_featured_image(post),
    )


# ---------------------------------------------------------------------------
# RSS feed parser
# ---------------------------------------------------------------------------

def _rss_text(item: Any, name: str) -> str:
    """``item.find(name).text`` defaulted to ``""``."""
    el = item.find(name)
    return (el.text or "") if el is not None else ""


def _rss_categories(item: Any) -> Tuple[List[str], List[str]]:
    """Split ``<category>`` into categories vs tags by ``domain`` attr."""
    categories: List[str] = []
    tags: List[str] = []
    for cat in item.findall("category"):
        cat_text = cat.text or ""
        if cat.get("domain") == "post_tag" or "tag" in cat_text.lower():
            tags.append(cat_text)
        else:
            categories.append(cat_text)
    return categories, tags


def _rss_featured_image(item: Any) -> Optional[str]:
    """Pull a featured image url from ``<enclosure type="image/...">``."""
    enc = item.find("enclosure")
    if enc is None:
        return None
    if not (enc.get("type", "") or "").startswith("image/"):
        return None
    return enc.get("url")


def _rss_author(item: Any) -> str:
    """Resolve the author from ``author`` or DC ``creator``."""
    author_elem = (
        item.find("author")
        or item.find("{http://purl.org/dc/elements/1.1/}creator")
    )
    if author_elem is None or author_elem.text is None:
        return ""
    return author_elem.text


def _rss_excerpt(item: Any) -> str:
    """Grab ``<description>``, strip tags, cap at 300 chars."""
    desc = _rss_text(item, "description")
    if not desc:
        return ""
    return re.sub(r"<[^>]+>", "", desc)[:300]


def _rss_post_from(item: Any) -> WordPressPost:
    """Map one RSS ``<item>`` to a ``WordPressPost``."""
    title = _rss_text(item, "title")
    url = _rss_text(item, "link")
    post_id = _rss_text(item, "guid") or url
    categories, tags = _rss_categories(item)
    return WordPressPost(
        id=post_id,
        title=title,
        url=url,
        excerpt=_rss_excerpt(item),
        date=_rss_text(item, "pubDate"),
        author=_rss_author(item),
        categories=categories,
        tags=tags,
        featured_image=_rss_featured_image(item),
    )


def _rss_items(root: Any) -> Iterable[Any]:
    """Locate items under ``channel``, falling back to RSS 1.0 / RDF."""
    channel = root.find(".//channel")
    if channel is None:
        channel = root
    items = channel.findall(".//item") if channel is not None else []
    if items:
        return items
    return root.findall(".//{http://purl.org/rss/1.0/}item")


def parse_rss_feed(
    xml_content: str, base_url: str, count: int,
) -> List[WordPressPost]:
    """Parse an RSS / RDF feed body into ``WordPressPost`` rows."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        logger.warning("[WordPress] RSS XML parse error: %s", exc)
        return []
    posts: List[WordPressPost] = []
    for item in list(_rss_items(root))[:count]:
        try:
            posts.append(_rss_post_from(item))
        except PARSE_EXCEPTIONS as exc:
            logger.debug(
                "[WordPress] RSS item parse failed (%s): %s",
                type(exc).__name__, exc,
            )
            continue
    return posts


# ---------------------------------------------------------------------------
# DOM-scrape probe helpers
# ---------------------------------------------------------------------------

ResolveAsync = Callable[[Any], Awaitable[Optional[str]]]


async def probe_first_match(
    scope: "Page | ElementHandle",
    selectors: Iterable[str],
    resolve: ResolveAsync,
) -> str:
    """Try each selector against ``scope``; first non-empty resolution wins."""
    for selector in selectors:
        try:
            el = await scope.query_selector(selector)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("wordpress probe(%s) failed (%s): %s",
                         selector, type(exc).__name__, exc)
            continue
        if not el:
            continue
        try:
            value = await resolve(el)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("wordpress probe(%s) resolve failed (%s): %s",
                         selector, type(exc).__name__, exc)
            continue
        if value:
            return value
    return ""


async def resolve_inner_text(el: "ElementHandle | None") -> Optional[str]:
    """``await el.inner_text()`` + ``.strip()``."""
    text = await el.inner_text()
    return text.strip() if isinstance(text, str) else None


async def resolve_datetime_or_text(el: "ElementHandle | None") -> Optional[str]:
    """Prefer ``datetime`` attr, fall back to text content."""
    dt = await el.get_attribute("datetime")
    if dt:
        return dt
    return await resolve_inner_text(el)


async def resolve_meta_or_text(el: "ElementHandle | None") -> Optional[str]:
    """Prefer the ``content`` attr (meta tags), fall back to text."""
    content = await el.get_attribute("content")
    if content:
        return content
    return await resolve_inner_text(el)


async def resolve_image_src(el: "ElementHandle | None") -> Optional[str]:
    """Prefer ``src``, fall back to ``data-src``; skip ``data:`` URLs."""
    src = await el.get_attribute("src")
    if not src:
        src = await el.get_attribute("data-src")
    if not src or src.startswith("data:"):
        return None
    return src


async def collect_inner_texts(scope: "Page | ElementHandle", selector: str) -> List[str]:
    """``await scope.query_selector_all(selector)`` -> stripped text list."""
    try:
        els = await scope.query_selector_all(selector)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("wordpress collect(%s) failed (%s): %s",
                     selector, type(exc).__name__, exc)
        return []
    out: List[str] = []
    for el in els:
        try:
            text = await resolve_inner_text(el)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("wordpress collect inner_text failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "WordPressPost",
    "WordPressPostDetail",
    "collect_inner_texts",
    "extract_slug_from_url",
    "normalize_url",
    "parse_api_post",
    "parse_api_post_detail",
    "parse_rss_feed",
    "probe_first_match",
    "resolve_datetime_or_text",
    "resolve_image_src",
    "resolve_inner_text",
    "resolve_meta_or_text",
    "resolve_url",
]
