"""ghost_scraper.py -- Ghost CMS detection + content scraping for CrawlRX

Standalone functions:
    detect_ghost(url, timeout) -> bool
    scrape_ghost_content(url, query, limit) -> List[Dict]
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin

import requests
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_HEADERS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    normalize_base_url,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.ghost")

_HEADERS = CMS_HEADERS

_GHOST_TEXT_MARKERS: tuple[str, ...] = (
    "ghost.io",
    "content-api.ghost.org",
    "ghost.init",
)

_KEY_PATTERNS: tuple[str, ...] = (
    r'ghost\.init\(\{[^}]*key:["\']([a-f0-9]{24,})["\']',
    r'content_key["\']?\s*:\s*["\']([a-f0-9]{24,})["\']',
    r'ghost-content-api-key["\']?\s*[:=]\s*["\']([a-f0-9]{24,})["\']',
)

_ARTICLE_SELECTORS: tuple[str, ...] = (
    "article.post-card",
    ".post-card",
    "article",
    ".post",
    '[class*="post-feed"] article',
)


def _detect_via_express(text: str, headers: Any) -> bool:
    """Express-based Ghost site detection (separate cognitive branch)."""
    powered_by = headers.get("X-Powered-By", "")
    if "Express" not in powered_by:
        return False
    return "ghost" in text.lower() or "ghost-theme" in text


def detect_ghost(url: str, timeout: int = 8) -> bool:
    """Check if a URL is a Ghost site. Returns False on any error."""
    base = normalize_base_url(url)
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("Ghost detection error for %s: %s", base, exc)
        return False
    if resp.status_code != 200:
        return False
    text = resp.text
    if any(marker in text for marker in _GHOST_TEXT_MARKERS):
        return True
    return _detect_via_express(text, resp.headers)


def _extract_ghost_key(text: str) -> Optional[str]:
    """Extract Ghost Content API key from page source."""
    for pattern in _KEY_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _resolve_ghost_url(post: dict, base_url: str) -> str:
    """Make a Ghost post url absolute relative to `base_url`."""
    url_path = post.get("url", "")
    if not url_path:
        return base_url
    if url_path.startswith("http"):
        return url_path
    return urljoin(base_url, url_path)


def _resolve_ghost_author(post: dict) -> str:
    """Pull author name from `authors` list or `primary_author` block."""
    authors = post.get("authors", [])
    if authors:
        return authors[0].get("name", "")
    return post.get("primary_author", {}).get("name", "")


def _resolve_ghost_excerpt(post: dict) -> str:
    """Excerpt > custom_excerpt > first 200 chars of plaintext."""
    excerpt = post.get("excerpt", "") or post.get("custom_excerpt", "")
    if excerpt:
        return excerpt
    plain = post.get("plaintext", "")
    return plain[:200] if isinstance(plain, str) else ""


def _parse_ghost_post(post: dict, base_url: str) -> Dict[str, Any]:
    """Parse a Ghost Content API post into standard format."""
    return {
        "title": post.get("title", ""),
        "excerpt": _resolve_ghost_excerpt(post),
        "url": _resolve_ghost_url(post, base_url),
        "published_at": post.get("published_at", ""),
        "author": _resolve_ghost_author(post),
    }


async def _wait_for_idle(page: Any) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        return


async def _query_first_match(page: Any, selectors: tuple[str, ...]) -> list[Any]:
    """Try selectors in order; return first non-empty list."""
    for selector in selectors:
        items = await page.query_selector_all(selector)
        if items:
            return items
    return []


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a browser, swallowing narrow cleanup failures."""
    if not browser:
        return
    try:
        await browser.close()
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _article_title(article: Any) -> str:
    """Pull article title or empty string."""
    el = await article.query_selector('h2, h3, .post-card-title, [class*="title"]')
    return (await el.inner_text()).strip() if el else ""


async def _article_excerpt(article: Any) -> str:
    """Pull article excerpt (first 200 chars) or empty string."""
    el = await article.query_selector('.post-card-excerpt, p, [class*="excerpt"]')
    return (await el.inner_text()).strip()[:200] if el else ""


async def _article_url(article: Any, base_url: str) -> str:
    """Resolve article link or fall back to base_url."""
    link_el = await article.query_selector("a")
    if not link_el:
        return base_url
    href = await link_el.get_attribute("href")
    return urljoin(base_url, href) if href else base_url


async def _article_published(article: Any) -> str:
    """Resolve published-at from datetime attr or inner text."""
    el = await article.query_selector('time, .post-card-meta-date, [class*="date"]')
    if not el:
        return ""
    attr = await el.get_attribute("datetime")
    if attr:
        return attr
    return (await el.inner_text()).strip()


async def _article_author(article: Any) -> str:
    """Resolve author name or empty string."""
    el = await article.query_selector('.post-card-author, [class*="author"]')
    return (await el.inner_text()).strip() if el else ""


async def _parse_article(article: Any, url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse one article card; None on miss / query mismatch."""
    title = await _article_title(article)
    if not title:
        return None
    if query and query.lower() not in title.lower():
        return None
    return {
        "title": title,
        "excerpt": await _article_excerpt(article),
        "url": await _article_url(article, url),
        "published_at": await _article_published(article),
        "author": await _article_author(article),
    }


async def _walk_articles(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Walk article cards, return parsed list capped at `limit`."""
    results: List[Dict[str, Any]] = []
    articles = await _query_first_match(page, _ARTICLE_SELECTORS)
    for article in articles:
        if len(results) >= limit:
            break
        try:
            parsed = await _parse_article(article, url, query)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Ghost PW article parse error: %s", exc)
            continue
        if parsed is not None:
            results.append(parsed)
    return results


async def _playwright_scrape_ghost(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Playwright CDP fallback -- port 9234 (knox)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await _wait_for_idle(page)
            return await _walk_articles(page, url, query, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("Ghost Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously, returning [] if a loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(coro)


def _fetch_ghost_api_key(base: str) -> Optional[str]:
    """GET base + parse Ghost Content API key from page source."""
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=8,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("Ghost key extraction error for %s: %s", base, exc)
        return None
    if resp.status_code != 200:
        return None
    return _extract_ghost_key(resp.text)


def _ghost_api_posts(base: str, api_key: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """Hit the Ghost Content API and parse posts."""
    endpoint = f"{base}/ghost/api/content/posts/?key={api_key}&limit={limit}"
    try:
        resp = requests.get(endpoint, headers=_HEADERS, timeout=8, allow_redirects=True)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Ghost Content API error for %s: %s: %s", base, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Ghost Content API returned %d for %s", resp.status_code, base)
        return None
    try:
        raw_posts = resp.json().get("posts", [])
    except (ValueError, TypeError) as exc:
        logger.warning("Ghost API JSON parse failed for %s: %s", base, exc)
        return None
    results: List[Dict[str, Any]] = []
    for post in raw_posts:
        parsed = _parse_ghost_post(post, base)
        if query and query.lower() not in parsed["title"].lower():
            continue
        results.append(parsed)
        if len(results) >= limit:
            break
    logger.info("Ghost Content API: %d posts from %s", len(results), base)
    return results


def scrape_ghost_content(
    url: str,
    query: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape posts from a Ghost site (Content API + Playwright fallback)."""
    base = normalize_base_url(url)
    api_key = _fetch_ghost_api_key(base)
    if not api_key:
        logger.warning("Ghost API key not found for %s, falling back to Playwright", base)
        return _run_async(_playwright_scrape_ghost(base, query, limit))
    posts = _ghost_api_posts(base, api_key, query, limit)
    if posts is not None:
        return posts
    logger.warning("Ghost falling back to Playwright for %s", base)
    return _run_async(_playwright_scrape_ghost(base, query, limit))
