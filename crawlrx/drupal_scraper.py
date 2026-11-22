"""drupal_scraper.py -- Drupal CMS detection + content scraping for CrawlRX

Standalone functions:
    detect_drupal(url, timeout) -> bool
    scrape_drupal_content(url, query, limit) -> List[Dict]
"""

import asyncio
import logging
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

logger = logging.getLogger("crawlrx.drupal")

_HEADERS = CMS_HEADERS

_DETECTION_MARKERS: tuple[str, ...] = (
    "Drupal.settings",
    "/sites/default/files/",
)

_ARTICLE_SELECTORS: tuple[str, ...] = (
    "article",
    ".node--type-article",
    ".view-content .views-row",
    '[class*="article"]',
    ".content .field--name-body",
)


def detect_drupal(url: str, timeout: int = 8) -> bool:
    """Check if a URL is a Drupal site. Returns False on any error."""
    base = normalize_base_url(url)
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("Drupal detection error for %s: %s", base, exc)
        return False
    if resp.status_code != 200:
        return False
    if any(marker in resp.text for marker in _DETECTION_MARKERS):
        return True
    return resp.headers.get("X-Generator", "").startswith("Drupal")


def _parse_drupal_article(node: dict, base_url: str) -> Dict[str, Any]:
    """Parse a Drupal JSON:API article node into standard format."""
    attrs = node.get("attributes", {})
    body = attrs.get("body", {})
    body_summary = body.get("summary", "") or body.get("value", "")[:200]
    url_path = attrs.get("path", {}).get("alias", "") or f"/node/{node.get('id', '')}"
    author_ref = attrs.get("uid", {})
    author = author_ref.get("display_name", "") if isinstance(author_ref, dict) else ""
    return {
        "title": attrs.get("title", ""),
        "body_summary": body_summary,
        "url": urljoin(base_url, url_path),
        "published": attrs.get("created", ""),
        "author": author,
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
    el = await article.query_selector('h1, h2, h3, .node__title, [class*="title"]')
    return (await el.inner_text()).strip() if el else ""


async def _article_body(article: Any) -> str:
    """Pull article body text (first 200 chars)."""
    el = await article.query_selector(".field--name-body, .content, p")
    return (await el.inner_text()).strip()[:200] if el else ""


async def _article_url(article: Any, base_url: str) -> str:
    """Resolve article link or fall back to base."""
    link_el = await article.query_selector("a")
    if not link_el:
        return base_url
    href = await link_el.get_attribute("href")
    return urljoin(base_url, href) if href else base_url


async def _article_published(article: Any) -> str:
    """Resolve published-at from text or datetime attribute."""
    el = await article.query_selector('.date, time, [class*="date"]')
    if not el:
        return ""
    text = (await el.inner_text()).strip()
    if text:
        return text
    return await el.get_attribute("datetime") or ""


async def _article_author(article: Any) -> str:
    """Resolve author name or empty string."""
    el = await article.query_selector('.author, [class*="author"]')
    return (await el.inner_text()).strip() if el else ""


async def _parse_dom_article(article: Any, url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse one DOM article element; None on miss / query mismatch."""
    title = await _article_title(article)
    if not title:
        return None
    if query and query.lower() not in title.lower():
        return None
    return {
        "title": title,
        "body_summary": await _article_body(article),
        "url": await _article_url(article, url),
        "published": await _article_published(article),
        "author": await _article_author(article),
    }


async def _walk_articles(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Walk article elements, return parsed list capped at `limit`."""
    results: List[Dict[str, Any]] = []
    articles = await _query_first_match(page, _ARTICLE_SELECTORS)
    for article in articles:
        if len(results) >= limit:
            break
        try:
            parsed = await _parse_dom_article(article, url, query)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Drupal PW article parse error: %s", exc)
            continue
        if parsed is not None:
            results.append(parsed)
    return results


async def _playwright_scrape_drupal(
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
        logger.warning("Drupal Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
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


def _drupal_jsonapi_articles(base: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """Hit the Drupal JSON:API endpoint; None on any failure."""
    endpoint = f"{base}/jsonapi/node/article?page[limit]={limit}"
    try:
        resp = requests.get(endpoint, headers=_HEADERS, timeout=8, allow_redirects=True)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Drupal JSON:API error for %s: %s: %s", base, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Drupal JSON:API returned %d for %s", resp.status_code, base)
        return None
    try:
        raw = resp.json().get("data", [])
    except (ValueError, TypeError) as exc:
        logger.warning("Drupal JSON parse failed for %s: %s", base, exc)
        return None
    results: List[Dict[str, Any]] = []
    for node in raw:
        article = _parse_drupal_article(node, base)
        if query and query.lower() not in article["title"].lower():
            continue
        results.append(article)
        if len(results) >= limit:
            break
    logger.info("Drupal JSON:API: %d articles from %s", len(results), base)
    return results


def scrape_drupal_content(
    url: str,
    query: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape Drupal articles (JSON:API + Playwright fallback)."""
    base = normalize_base_url(url)
    rest = _drupal_jsonapi_articles(base, query, limit)
    if rest is not None:
        return rest
    logger.warning("Drupal falling back to Playwright for %s", base)
    return _run_async(_playwright_scrape_drupal(base, query, limit))
