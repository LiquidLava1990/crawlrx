"""DOM-scrape helpers for the WordPress scraper (Playwright fallback).

Split out of ``wordpress.py`` for readability: the
``_scrape_posts_dom`` / ``_scrape_single_dom`` god functions used to
embed >40 selectors and seven ``try/except Exception`` blocks each.
Selector tuples + per-field resolvers live here so the routing module
keeps just orchestration logic.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS
from ._wordpress_helpers import (
    WordPressPost,
    WordPressPostDetail,
    collect_inner_texts,
    probe_first_match,
    resolve_datetime_or_text,
    resolve_image_src,
    resolve_inner_text,
    resolve_meta_or_text,
    resolve_url,
)

logger = logging.getLogger("crawlrx.wordpress._dom")


# ---------------------------------------------------------------------------
# Selector catalogue (kept as tuples to make ordering explicit)
# ---------------------------------------------------------------------------

POST_LIST_SELECTORS: Tuple[str, ...] = (
    "article.post",
    "article",
    ".post",
    ".blog-post",
    ".entry",
    ".wp-block-post",
)

TITLE_SELECTORS: Tuple[str, ...] = (
    "h1 a", "h2 a", "h1.entry-title a", "h2.entry-title a",
    "h1", "h2", ".entry-title",
)

SINGLE_TITLE_SELECTORS: Tuple[str, ...] = (
    "h1.entry-title", "h1.post-title", "h1", "article h1",
)

CONTENT_SELECTORS: Tuple[str, ...] = (
    ".entry-content",
    ".post-content",
    ".content",
    "article .content",
    ".wp-block-post-content",
)

EXCERPT_SELECTORS: Tuple[str, ...] = (
    ".entry-summary", ".excerpt", ".post-excerpt", "p",
)

DATE_SELECTORS: Tuple[str, ...] = (
    "time", ".entry-date", ".published", ".post-date",
)

SINGLE_DATE_SELECTORS: Tuple[str, ...] = (
    "time", ".entry-date", ".published",
    "meta[property='article:published_time']",
)

AUTHOR_SELECTORS: Tuple[str, ...] = (
    ".author a", ".entry-author", ".byline a",
)

SINGLE_AUTHOR_SELECTORS: Tuple[str, ...] = (
    ".author a", ".entry-author a", ".byline a", "meta[name='author']",
)

FEATURED_SELECTORS: Tuple[str, ...] = (
    ".featured-image img", ".post-thumbnail img",
    "meta[property='og:image']",
)

CATEGORY_SELECTORS: str = ".cat-links a, .categories a, a[rel='category tag']"
TAG_SELECTORS: str = ".tags-links a, .tags a, a[rel='tag']"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def find_post_elements(page: "Page") -> List[Any]:
    """Walk ``POST_LIST_SELECTORS`` until one returns a non-empty list."""
    for selector in POST_LIST_SELECTORS:
        try:
            els = await page.query_selector_all(selector)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("wordpress post-list(%s) failed (%s): %s",
                         selector, type(exc).__name__, exc)
            continue
        if els:
            return els
    return []


async def _post_url_from(scope: Any, base_url: str) -> str:
    """Resolve the first ``<a>`` href under ``scope`` to absolute."""
    try:
        link_el = await scope.query_selector("a")
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("wordpress link probe failed (%s): %s",
                     type(exc).__name__, exc)
        return ""
    if not link_el:
        return ""
    try:
        href = await link_el.get_attribute("href")
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("wordpress link href failed (%s): %s",
                     type(exc).__name__, exc)
        return ""
    return resolve_url(base_url, href or "")


async def _featured_image_dom(scope: Any, base_url: str) -> Optional[str]:
    """Resolve the first inline ``<img>`` against the post element."""
    try:
        img_el = await scope.query_selector("img")
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("wordpress img probe failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    if not img_el:
        return None
    try:
        src = await resolve_image_src(img_el)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("wordpress img resolve failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    if not src:
        return None
    return resolve_url(base_url, src)


async def _resolve_inner_html(el: Any) -> Optional[str]:
    """``await el.inner_html()`` (no strip)."""
    return await el.inner_html()


async def _resolve_date_or_meta(el: Any) -> Optional[str]:
    """Date resolver: ``datetime`` attr, then ``content`` attr, then text."""
    dt = await el.get_attribute("datetime")
    if dt:
        return dt
    return await resolve_meta_or_text(el)


async def _resolve_featured_image(el: Any) -> Optional[str]:
    """Featured-image resolver: ``content`` for ``<meta>`` else image src."""
    tag = (getattr(el, "tag_name", None) or "").lower()
    if tag == "meta":
        return await el.get_attribute("content")
    return await resolve_image_src(el)


async def build_dom_post(
    el: "ElementHandle | None", idx: int, base_url: str,
) -> WordPressPost:
    """Pull a ``WordPressPost`` out of a Playwright post element handle."""
    title = await probe_first_match(el, TITLE_SELECTORS, resolve_inner_text)
    excerpt_full = await probe_first_match(
        el, EXCERPT_SELECTORS, resolve_inner_text,
    )
    return WordPressPost(
        id=idx,
        title=title,
        url=await _post_url_from(el, base_url),
        excerpt=excerpt_full[:300],
        date=await probe_first_match(el, DATE_SELECTORS, resolve_datetime_or_text),
        author=await probe_first_match(el, AUTHOR_SELECTORS, resolve_inner_text),
        categories=[],
        tags=[],
        featured_image=await _featured_image_dom(el, base_url),
    )


async def build_dom_post_detail(
    page: "Page", post_url: str,
) -> WordPressPostDetail:
    """Pull a ``WordPressPostDetail`` out of a fully-loaded post page."""
    title = await probe_first_match(
        page, SINGLE_TITLE_SELECTORS, resolve_inner_text,
    )
    content = await probe_first_match(
        page, CONTENT_SELECTORS, _resolve_inner_html,
    )
    date = await probe_first_match(
        page, SINGLE_DATE_SELECTORS, _resolve_date_or_meta,
    )
    author = await probe_first_match(
        page, SINGLE_AUTHOR_SELECTORS, resolve_meta_or_text,
    )
    featured = await probe_first_match(
        page, FEATURED_SELECTORS, _resolve_featured_image,
    )
    categories = await collect_inner_texts(page, CATEGORY_SELECTORS)
    tags = await collect_inner_texts(page, TAG_SELECTORS)
    return WordPressPostDetail(
        title=title,
        url=post_url,
        content=content,
        date=date,
        author=author,
        categories=categories,
        tags=tags,
        featured_image=featured or None,
    )
