"""wordpress.py -- WordPress scraper.

Endpoints:
  POST /scrape/wordpress/posts -- list posts with pagination
  POST /scrape/wordpress/post  -- single post detail

3-tier fallback strategy:
  1. REST API   -- ``{base}/wp-json/wp/v2/posts`` (fast, structured)
  2. RSS feed   -- ``{base}/feed/`` or ``{base}/rss.xml`` (fallback)
  3. DOM scrape -- Playwright (last resort)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)
from ._wordpress_dom import (
    build_dom_post,
    build_dom_post_detail,
    find_post_elements,
)
from ._wordpress_helpers import (
    WordPressPost,
    WordPressPostDetail,
    extract_slug_from_url,
    normalize_url,
    parse_api_post,
    parse_api_post_detail,
    parse_rss_feed,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.wordpress")
router = APIRouter(prefix="", tags=["wordpress"])

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/json",
}
_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ─────────────────────────────────────────────
# TIER 1: REST API
# ─────────────────────────────────────────────

async def _api_get_json(
    endpoint: str, params: Dict[str, Any], where: str,
) -> Optional[Any]:
    """GET ``endpoint`` and return ``(resp, json)`` (or None on any failure)."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers=_API_HEADERS, params=params)
    except httpx.HTTPError as exc:
        logger.warning("[WordPress] API fetch failed for %s: %s", where, exc)
        return None
    if resp.status_code != 200:
        logger.info("[WordPress] API returned %d for %s", resp.status_code, where)
        return None
    try:
        return resp, resp.json()
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.warning(
            "[WordPress] API JSON decode failed for %s (%s): %s",
            where, type(exc).__name__, exc,
        )
        return None


async def _try_api_posts(
    base_url: str, count: int, page: int,
) -> Optional[Dict[str, Any]]:
    """Try the WordPress REST API for the posts list."""
    endpoint = f"{base_url}/wp-json/wp/v2/posts"
    result = await _api_get_json(
        endpoint,
        {"per_page": count, "page": page, "_embed": 1},
        f"posts list {endpoint}",
    )
    if result is None:
        return None
    resp, data = result
    total = resp.headers.get("X-WP-Total", "0")
    return {"posts": data, "total": int(total) if total.isdigit() else len(data)}


async def _try_api_single(
    base_url: str, slug: str,
) -> Optional[Dict[str, Any]]:
    """Try the WordPress REST API for a single post by slug."""
    endpoint = f"{base_url}/wp-json/wp/v2/posts"
    result = await _api_get_json(
        endpoint,
        {"slug": slug, "_embed": 1},
        f"single slug={slug}",
    )
    if result is None:
        return None
    _, data = result
    if data and len(data) > 0:
        return data[0]
    return None


# ─────────────────────────────────────────────
# TIER 2: RSS Feed
# ─────────────────────────────────────────────

async def _try_rss_feed(
    client: httpx.AsyncClient,
    feed_url: str,
    base_url: str,
    count: int,
) -> List[WordPressPost]:
    """Fetch one feed URL and return its parsed posts (or [])."""
    try:
        resp = await client.get(feed_url, headers=_RSS_HEADERS)
    except httpx.HTTPError as exc:
        logger.debug("[WordPress] RSS fetch failed for %s: %s", feed_url, exc)
        return []
    if resp.status_code != 200:
        return []
    return parse_rss_feed(resp.text, base_url, count)


async def _try_rss_posts(
    base_url: str, count: int,
) -> Optional[Dict[str, Any]]:
    """Walk WordPress RSS feed candidates for the posts list."""
    feed_urls = (f"{base_url}/feed/", f"{base_url}/rss.xml", f"{base_url}/feed")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for feed_url in feed_urls:
            posts = await _try_rss_feed(client, feed_url, base_url, count)
            if posts:
                return {"posts": posts, "total": len(posts)}
    return None


async def _try_rss_single(
    base_url: str, slug: str,
) -> Optional[WordPressPostDetail]:
    """Search recent RSS posts for a matching slug, returning detail-shape."""
    result = await _try_rss_posts(base_url, 50)
    if not (result and result.get("posts")):
        return None
    for post in result["posts"]:
        post_slug = extract_slug_from_url(post.url)
        if post_slug and post_slug.lower() == slug.lower():
            return WordPressPostDetail(
                title=post.title,
                url=post.url,
                content=post.excerpt,
                date=post.date,
                author=post.author,
                categories=post.categories,
                tags=post.tags,
                featured_image=post.featured_image,
            )
    return None


# ─────────────────────────────────────────────
# TIER 3: DOM scrape (Playwright)
# ─────────────────────────────────────────────

async def _wait_for_networkidle_dom(page: Any, timeout_ms: int) -> None:
    """Best-effort networkidle wait, swallowing only Playwright timeouts."""
    try:
        await page.wait_for_load_state(
            "networkidle", timeout=min(timeout_ms, 15000),
        )
    except PlaywrightTimeoutError:
        return


async def _build_dom_posts(
    elements: List[Any], count: int, base_url: str,
) -> List[WordPressPost]:
    """Convert each DOM post-element into a typed ``WordPressPost``."""
    posts: List[WordPressPost] = []
    for idx, el in enumerate(elements[:count]):
        try:
            posts.append(await build_dom_post(el, idx, base_url))
        except SCRAPE_FAILURE_EXCEPTIONS as exc:
            logger.debug(
                "[WordPress] DOM post parse failed (%s): %s",
                type(exc).__name__, exc,
            )
    return posts


async def _run_dom_session(
    target_url: str, timeout_ms: int, where: str, on_page: Any,
) -> Optional[Any]:
    """Spin up a stealth context, navigate, then dispatch ``on_page(page)``."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[WordPress] DOM %s scrape for %s", where, target_url)
            await page.goto(
                target_url, wait_until="domcontentloaded", timeout=timeout_ms,
            )
            await _wait_for_networkidle_dom(page, timeout_ms)
            return await on_page(page)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "[WordPress] DOM %s scrape failed (%s): %s",
            where, type(exc).__name__, exc,
        )
        return None
    finally:
        await close_stealth_context(browser, ctx, page)


async def _scrape_posts_dom(
    base_url: str, count: int, timeout_ms: int,
) -> Optional[Dict[str, Any]]:
    """Playwright fallback for the posts list."""
    async def _on_page(page: Any) -> Dict[str, Any]:
        elements = await find_post_elements(page)
        posts = await _build_dom_posts(elements, count, base_url)
        return {"posts": posts, "total": len(posts)}

    return await _run_dom_session(base_url, timeout_ms, "posts", _on_page)


async def _scrape_single_dom(
    post_url: str, timeout_ms: int,
) -> Optional[WordPressPostDetail]:
    """Playwright fallback for a single post page."""
    async def _on_page(page: Any) -> WordPressPostDetail:
        return await build_dom_post_detail(page, post_url)

    return await _run_dom_session(post_url, timeout_ms, "single", _on_page)


# ─────────────────────────────────────────────
# Posts-list orchestration
# ─────────────────────────────────────────────

async def _list_via_api(
    base_url: str, count: int, page_num: int,
) -> Optional[Dict[str, Any]]:
    """Tier-1 (REST API) attempt for the posts list."""
    api_result = await _try_api_posts(base_url, count, page_num)
    if not api_result:
        return None
    posts = [
        parse_api_post(p, base_url).to_dict()
        for p in api_result.get("posts", [])
    ]
    if not posts:
        return None
    return {"posts": posts, "total": api_result.get("total", len(posts)), "method": "api"}


async def _list_via_rss(
    base_url: str, count: int,
) -> Optional[Dict[str, Any]]:
    """Tier-2 (RSS) attempt for the posts list."""
    rss_result = await _try_rss_posts(base_url, count)
    if not rss_result:
        return None
    posts = [p.to_dict() for p in rss_result.get("posts", [])]
    if not posts:
        return None
    return {"posts": posts, "total": rss_result.get("total", len(posts)), "method": "rss"}


async def _list_via_dom(
    base_url: str, count: int,
) -> Optional[Dict[str, Any]]:
    """Tier-3 (DOM) attempt for the posts list, with a 45s wait."""
    try:
        dom_result = await asyncio.wait_for(
            _scrape_posts_dom(base_url, count, 30000), timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[WordPress] DOM posts scrape exceeded 45s timeout")
        return None
    if not dom_result:
        return None
    posts = [p.to_dict() for p in dom_result.get("posts", [])]
    if not posts:
        return None
    return {"posts": posts, "total": dom_result.get("total", len(posts)), "method": "dom"}


def _empty_posts_response(url: str, error_message: str, elapsed_ms: int) -> Dict[str, Any]:
    """Failure-shape for the ``/scrape/wordpress/posts`` endpoint."""
    return {
        "success": False,
        "url": url,
        "posts": [],
        "total": 0,
        "method": None,
        "error": error_message,
        "elapsed_ms": elapsed_ms,
    }


@router.post("/scrape/wordpress/posts")
async def scrape_wordpress_posts(request: Dict[str, Any]) -> Dict[str, Any]:
    """List posts from a WordPress site (API > RSS > DOM)."""
    start_time = time.time()
    url = (request.get("url") or "").strip()
    count = min(request.get("count", 10), 100)
    page_num = request.get("page", 1)
    if not url:
        return _empty_posts_response(url, "URL is required", 0)
    base_url = normalize_url(url)
    for tier in (
        _list_via_api(base_url, count, page_num),
        _list_via_rss(base_url, count),
        _list_via_dom(base_url, count),
    ):
        result = await tier
        if result is not None:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "success": True,
                "url": url,
                "posts": result["posts"],
                "total": result["total"],
                "method": result["method"],
                "error": None,
                "elapsed_ms": elapsed_ms,
            }
    elapsed_ms = int((time.time() - start_time) * 1000)
    return _empty_posts_response(
        url, "Failed to fetch posts via API, RSS, or DOM", elapsed_ms,
    )


# ─────────────────────────────────────────────
# Single-post orchestration
# ─────────────────────────────────────────────

async def _detail_via_api(
    base_url: str, slug: Optional[str],
) -> Optional[WordPressPostDetail]:
    """Tier-1 (REST API) attempt for a single post."""
    if not slug:
        return None
    api_data = await _try_api_single(base_url, slug)
    if not api_data:
        return None
    return parse_api_post_detail(api_data, base_url)


async def _detail_via_rss(
    base_url: str, slug: Optional[str],
) -> Optional[WordPressPostDetail]:
    """Tier-2 (RSS) attempt for a single post."""
    if not slug:
        return None
    return await _try_rss_single(base_url, slug)


async def _detail_via_dom(
    post_url: str,
) -> Optional[WordPressPostDetail]:
    """Tier-3 (DOM) attempt for a single post, with a 45s wait."""
    try:
        return await asyncio.wait_for(
            _scrape_single_dom(post_url, 30000), timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("[WordPress] DOM single scrape exceeded 45s timeout")
        return None


def _detail_failure(url: str, error_message: str, elapsed_ms: int) -> Dict[str, Any]:
    """Failure-shape for the ``/scrape/wordpress/post`` endpoint."""
    return {
        "success": False,
        "url": url,
        "error": error_message,
        "method": None,
        "elapsed_ms": elapsed_ms,
    }


@router.post("/scrape/wordpress/post")
async def scrape_wordpress_post(request: Dict[str, Any]) -> Dict[str, Any]:
    """Scrape a single WordPress post (API > RSS > DOM)."""
    start_time = time.time()
    url = (request.get("url") or "").strip()
    if not url:
        return _detail_failure(url, "URL is required", 0)
    slug = extract_slug_from_url(url)
    base_url = normalize_url(url)
    detail: Optional[WordPressPostDetail] = await _detail_via_api(base_url, slug)
    method = "api" if detail else None
    if detail is None:
        detail = await _detail_via_rss(base_url, slug)
        method = "rss" if detail else None
    if detail is None:
        detail = await _detail_via_dom(url)
        method = "dom" if detail else None
    elapsed_ms = int((time.time() - start_time) * 1000)
    if detail is None:
        return _detail_failure(
            url, "Failed to fetch post via API, RSS, or DOM", elapsed_ms,
        )
    result = detail.to_dict()
    result.update({
        "success": True,
        "url": url,
        "method": method,
        "elapsed_ms": elapsed_ms,
    })
    return result
