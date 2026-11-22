"""joomla_scraper.py -- Joomla CMS detection + content scraping for CrawlRX

Standalone functions:
    detect_joomla(url, timeout) -> bool
    scrape_joomla_content(url, query, limit) -> List[Dict]
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

logger = logging.getLogger("crawlrx.joomla")

_HEADERS = CMS_HEADERS

_DETECTION_MARKERS: tuple[str, ...] = (
    "/media/jui/js/",
    "com_content",
    "/components/com_",
)
_META_GEN_RE = re.compile(
    r'<meta[^>]*name=["\']generator["\'][^>]*content=["\']([^"\']+)',
    re.IGNORECASE,
)

_ARTICLE_SELECTORS: tuple[str, ...] = (
    "article.item",
    ".item",
    ".blog-item",
    ".contentpaneopen",
    "table.contentpaneopen",
)


def _has_joomla_meta(text: str) -> bool:
    """Check if a `<meta name=generator content=...>` includes 'joomla'."""
    if "Joomla!" not in text and "joomla" not in text.lower():
        return False
    meta = _META_GEN_RE.search(text)
    return bool(meta and "joomla" in meta.group(1).lower())


def detect_joomla(url: str, timeout: int = 8) -> bool:
    """Check if a URL is a Joomla site. Returns False on any error."""
    base = normalize_base_url(url)
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("Joomla detection error for %s: %s", base, exc)
        return False
    if resp.status_code != 200:
        return False
    text = resp.text
    if any(marker in text for marker in _DETECTION_MARKERS):
        return True
    return _has_joomla_meta(text)


def _build_article_path(attrs: dict, article_id: Any) -> str:
    """Resolve a Joomla article URL path from slug / category / id."""
    slug = attrs.get("alias", "")
    cat_slug = attrs.get("category_alias", "")
    if cat_slug and slug:
        return f"/{cat_slug}/{slug}"
    if slug:
        return f"/{slug}"
    return f"/index.php?option=com_content&view=article&id={article_id or ''}"


def _parse_joomla_article(article: dict, base_url: str) -> Dict[str, Any]:
    """Parse a Joomla REST API article into standard format."""
    attrs = article.get("attributes", {})
    introtext = attrs.get("introtext", "") or attrs.get("fulltext", "")[:200]
    introtext = re.sub(r"<[^>]+>", "", introtext)[:200]
    return {
        "title": attrs.get("title", ""),
        "introtext": introtext,
        "url": urljoin(base_url, _build_article_path(attrs, article.get("id", ""))),
        "created": attrs.get("created", ""),
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
    el = await article.query_selector('h2, h3, .contentheading, a[title]')
    return (await el.inner_text()).strip() if el else ""


async def _article_intro(article: Any) -> str:
    """Pull article intro (first 200 chars) or empty string."""
    el = await article.query_selector('p, .introtext, [class*="intro"]')
    return (await el.inner_text()).strip()[:200] if el else ""


async def _article_url(article: Any, base_url: str) -> str:
    """Resolve article link or fall back to base."""
    el = await article.query_selector('a.readon, a[title], h2 a, h3 a')
    if not el:
        return base_url
    href = await el.get_attribute("href")
    return urljoin(base_url, href) if href else base_url


async def _article_created(article: Any) -> str:
    """Resolve created-date text or empty string."""
    el = await article.query_selector('.create, time, .date, [class*="date"]')
    return (await el.inner_text()).strip() if el else ""


async def _parse_dom_article(article: Any, url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse one article element; None on miss / query mismatch."""
    title = await _article_title(article)
    if not title:
        return None
    if query and query.lower() not in title.lower():
        return None
    return {
        "title": title,
        "introtext": await _article_intro(article),
        "url": await _article_url(article, url),
        "created": await _article_created(article),
    }


async def _walk_articles(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Walk article cards, return parsed list capped at `limit`."""
    results: List[Dict[str, Any]] = []
    articles = await _query_first_match(page, _ARTICLE_SELECTORS)
    for article in articles:
        if len(results) >= limit:
            break
        try:
            parsed = await _parse_dom_article(article, url, query)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Joomla PW article parse error: %s", exc)
            continue
        if parsed is not None:
            results.append(parsed)
    return results


async def _playwright_scrape_joomla(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright CDP fallback -- port 9234 (knox)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await _wait_for_idle(page)
            return await _walk_articles(page, url, query, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("Joomla Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously, returning [] when a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(coro)


def _joomla_rest_articles(base: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """Fetch from the Joomla REST endpoint, returning parsed articles or None."""
    endpoint = f"{base}/api/index.php/v1/content/articles?limit={limit}"
    try:
        resp = requests.get(endpoint, headers=_HEADERS, timeout=8, allow_redirects=True)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Joomla REST API error for %s: %s: %s", base, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Joomla REST API returned %d for %s", resp.status_code, base)
        return None
    try:
        raw = resp.json().get("data", [])
    except (ValueError, TypeError) as exc:
        logger.warning("Joomla REST JSON parse failed for %s: %s", base, exc)
        return None
    results: List[Dict[str, Any]] = []
    for article in raw:
        parsed = _parse_joomla_article(article, base)
        if query and query.lower() not in parsed["title"].lower():
            continue
        results.append(parsed)
        if len(results) >= limit:
            break
    logger.info("Joomla REST API: %d articles from %s", len(results), base)
    return results


def scrape_joomla_content(
    url: str,
    query: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape Joomla articles (REST API + Playwright fallback)."""
    base = normalize_base_url(url)
    rest = _joomla_rest_articles(base, query, limit)
    if rest is not None:
        return rest
    logger.warning("Joomla falling back to Playwright for %s", base)
    return _run_async(_playwright_scrape_joomla(base, query, limit))
