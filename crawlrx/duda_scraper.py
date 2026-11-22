"""duda_scraper.py -- Duda CMS detection + content scraping for CrawlRX

Standalone functions:
    detect_duda(url, timeout) -> bool
    scrape_duda_content(url, query, limit) -> List[Dict]
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_HEADERS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    normalize_base_url,
    text_contains_any,
    text_contains_any_ci,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.duda")

_HEADERS = {
    **CMS_HEADERS,
    "Accept": "text/html",
}

_DUDA_TEXT_MARKERS: tuple[str, ...] = (
    "irp-cdn.multiscreensite.com",
    "dmstatic.com",
    "data-dm-event",
    "dm-module",
    "duda.co",
)
_DUDA_CI_MARKERS: tuple[str, ...] = ("duda-one",)

_CONTENT_SELECTORS: tuple[str, ...] = (
    "main",
    "section.dmHomeSection",
    ".dmHomeSection",
    ".dmSection",
    "section",
    '[class*="dmSection"]',
    ".dmContent",
)


def detect_duda(url: str, timeout: int = 8) -> bool:
    """Check if a URL is a Duda site. Returns False on any error."""
    base = normalize_base_url(url)
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("Duda detection error for %s: %s", base, exc)
        return False
    if resp.status_code != 200:
        return False
    text = resp.text
    if text_contains_any(text, _DUDA_TEXT_MARKERS):
        return True
    return text_contains_any_ci(text, _DUDA_CI_MARKERS)


def _first_heading_text(section: Any) -> str:
    """Return the first heading text inside a section."""
    h1 = section.find("h1")
    if h1:
        return h1.get_text(strip=True)
    h2 = section.find("h2")
    if h2:
        return h2.get_text(strip=True)
    headings = section.find_all(["h1", "h2", "h3"])
    return headings[0].get_text(strip=True) if headings else ""


def _section_text(section: Any) -> str:
    """Return joined paragraphs (first 5) or fallback section text."""
    paragraphs = section.find_all("p")
    text_parts = [p.get_text(strip=True) for p in paragraphs[:5]]
    text = " ".join(text_parts)
    if text:
        return text
    return section.get_text(strip=True, separator=" ")[:500]


def _build_section_dict(section: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Build the {title, text, url} dict for one section, or None if empty."""
    title = _first_heading_text(section)
    text = _section_text(section)
    if not title and not text:
        return None
    return {
        "title": title or "Untitled",
        "text": text[:500],
        "url": base_url,
    }


def _resolve_sections(soup: BeautifulSoup) -> list[Any]:
    """Pick first matching content selector; fall back to body / soup."""
    for selector in _CONTENT_SELECTORS:
        sections = soup.select(selector)
        if sections:
            return list(sections)
    if soup.body:
        return [soup.body]
    return [soup]


def _extract_duda_content(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    """Extract content from Duda HTML using BeautifulSoup."""
    results: List[Dict[str, Any]] = []
    for section in _resolve_sections(soup):
        try:
            entry = _build_section_dict(section, base_url)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Duda content extraction error: %s", exc)
            continue
        if entry is not None:
            results.append(entry)
    return results


def _filter_and_cap(
    results: List[Dict[str, Any]], query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Apply query filter (title + text) then cap at `limit`."""
    if query:
        q_lower = query.lower()
        results = [
            r for r in results
            if q_lower in r["title"].lower() or q_lower in r["text"].lower()
        ]
    return results[:limit]


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a browser, swallowing narrow cleanup failures."""
    if not browser:
        return
    try:
        await browser.close()
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _wait_for_idle(page: Any) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        return


async def _playwright_scrape_duda(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright CDP fallback -- port 9234 (knox)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await _wait_for_idle(page)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            return _filter_and_cap(_extract_duda_content(soup, url), query, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("Duda Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
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


def _try_http_scrape(base: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """HTTP + BeautifulSoup happy path; None on any failure."""
    try:
        resp = requests.get(base, headers=_HEADERS, timeout=8, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("Duda requests error for %s: %s: %s", base, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Duda HTTP returned %d for %s", resp.status_code, base)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    results = _filter_and_cap(_extract_duda_content(soup, base), query, limit)
    logger.info("Duda BeautifulSoup: %d content blocks from %s", len(results), base)
    return results


def scrape_duda_content(
    url: str,
    query: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape content from a Duda site (HTTP + BS4, Playwright fallback)."""
    base = normalize_base_url(url)
    http_results = _try_http_scrape(base, query, limit)
    if http_results is not None:
        return http_results
    logger.warning("Duda falling back to Playwright for %s", base)
    return _run_async(_playwright_scrape_duda(base, query, limit))
