"""clickfunnels_scraper.py -- ClickFunnels funnel/page scraper for CrawlRX.

Detection and content extraction for ClickFunnels-hosted pages.
Strategy:
  1. Detection: Check page source for ClickFunnels markers
  2. Scraping: Playwright CDP (port 9236) -- ClickFunnels is JS-heavy
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._cms_helpers import (
    CMS_PLAYWRIGHT_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    text_contains_any,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.clickfunnels_scraper")

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DETECTION_MARKERS: tuple[str, ...] = ("clickfunnels.com", "cf-page", "cfnl")


_CONTENT_EXTRACT_JS = """() => {
    const results = [];
    const sections = document.querySelectorAll('section, .cf-section, .cf-page-section, .row');
    sections.forEach(section => {
        const h1 = section.querySelector('h1');
        const h2 = section.querySelector('h2');
        const h3 = section.querySelector('h3');
        const headline = h1 ? h1.innerText.trim() : '';
        const subheadline = h2 ? h2.innerText.trim() : (h3 ? h3.innerText.trim() : '');
        const buttons = section.querySelectorAll('a.button, button, .cta, .cf-btn, [class*="button"]');
        let ctaText = '';
        let ctaUrl = '';
        buttons.forEach(btn => {
            if (!ctaText && btn.innerText.trim()) {
                ctaText = btn.innerText.trim();
                ctaUrl = btn.getAttribute('href') || '';
            }
        });
        let price = '';
        const priceSelectors = [
            '.price', '.pricing', '[class*="price"]', '[class*="pricing"]',
            '.amount', '.cost', '[class*="cost"]'
        ];
        for (const sel of priceSelectors) {
            const el = section.querySelector(sel);
            if (el && el.innerText.match(/\\$|\\d+\\.\\d{2}/)) {
                price = el.innerText.trim();
                break;
            }
        }
        const paragraphs = section.querySelectorAll('p');
        let description = '';
        paragraphs.forEach(p => {
            const text = p.innerText.trim();
            if (text.length > 20 && description.length < 500) {
                description += text + ' ';
            }
        });
        description = description.trim();
        if (headline || description) {
            results.push({
                title: headline || subheadline,
                description: description,
                price: price,
                cta_url: ctaUrl,
                cta_text: ctaText
            });
        }
    });
    return results;
}"""


def detect_clickfunnels(url: str, timeout: int = _TIMEOUT) -> bool:
    """Return True if URL is a ClickFunnels site (page-source markers)."""
    if not url:
        return False
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("ClickFunnels detection failed for %s: %s: %s", url, type(exc).__name__, exc)
        return False
    return text_contains_any(resp.text.lower(), _DETECTION_MARKERS)


def _matches_query(title: str, description: str, query: Optional[str]) -> bool:
    """Case-insensitive match against title or description."""
    if not query:
        return True
    q_lower = query.lower()
    return q_lower in title.lower() or q_lower in description.lower()


def _build_section_dict(item: dict, url: str) -> Dict[str, Any]:
    """Map one DOM-extracted section to standard dict."""
    return {
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "price": item.get("price", ""),
        "cta_url": item.get("cta_url", ""),
        "url": url,
    }


async def _wait_for_idle(page: Any, timeout_ms: int = 15000) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a browser, swallowing narrow cleanup failures."""
    if not browser:
        return
    try:
        await browser.close()
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _do_clickfunnels_scrape(page: Any, url: str) -> list[dict]:
    """Run the page interaction inside an open page; returns raw items."""
    logger.info("Scraping ClickFunnels page: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_for_idle(page)
    await page.wait_for_timeout(2000)
    return await page.evaluate(_CONTENT_EXTRACT_JS)


async def scrape_clickfunnels_content(
    url: str, query: Optional[str] = None, limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape ClickFunnels page content via Playwright (JS-heavy site)."""
    if not url:
        return []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            data = await _do_clickfunnels_scrape(page, url)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("ClickFunnels scrape failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)
    results: List[Dict[str, Any]] = []
    for item in data[:limit]:
        built = _build_section_dict(item, url)
        if not _matches_query(built["title"], built["description"], query):
            continue
        results.append(built)
    logger.info("ClickFunnels scrape: %d items from %s", len(results), url)
    return results[:limit]
