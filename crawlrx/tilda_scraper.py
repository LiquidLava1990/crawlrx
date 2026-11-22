"""tilda_scraper.py -- Tilda site detection + content scraper."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    network_exception_types,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.tilda_scraper")

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    *network_exception_types(),
    requests.RequestException,
    ValueError,
)

_TILDA_MARKERS: tuple[str, ...] = ("tildacdn.com", "tilda.ws")


def detect_tilda(url: str, timeout: int = _TIMEOUT) -> bool:
    """Return True if ``url`` page source contains Tilda markers."""
    if not url:
        return False
    try:
        resp = requests.get(
            url, headers=_HEADERS, timeout=timeout, allow_redirects=True
        )
        src = resp.text.lower()
    except _NETWORK_ERRORS as exc:
        logger.warning("Tilda detection failed for %s: %s: %s", url, type(exc).__name__, exc)
        return False
    for marker in _TILDA_MARKERS:
        if marker in src:
            logger.info("Tilda detected at %s via marker: %s", url, marker)
            return True
    return False


# ─────────────────────────────────────────────
# BS4 path
# ─────────────────────────────────────────────


def _record_title(record: Any) -> str:
    """Pull a title from a Tilda record block."""
    title_el = record.select_one(".t-title, .t-heading, h1, h2, h3")
    return title_el.get_text(strip=True) if title_el else ""


def _record_text(record: Any) -> str:
    """Pull all text out of a Tilda record block."""
    text_els = record.select(".t-text, .t-descr, p")
    parts = [el.get_text(strip=True) for el in text_els]
    parts = [p for p in parts if p]
    return " ".join(parts).strip()


def _record_image(record: Any) -> str:
    """Pull image src from a Tilda record block."""
    img_el = record.select_one("img")
    if not img_el:
        return ""
    return img_el.get("src") or img_el.get("data-src") or ""


def _matches_query(title: str, text: str, query: Optional[str]) -> bool:
    """Return True when ``query`` matches title or text (case-insensitive)."""
    if not query:
        return True
    q = query.lower()
    return q in title.lower() or q in text.lower()


def _build_record(record: Any, url: str) -> Optional[Dict[str, Any]]:
    """Convert a single ``.t-record`` element into our payload dict."""
    try:
        title = _record_title(record)
        text = _record_text(record)
        image = _record_image(record)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Tilda record parse error: %s: %s", type(exc).__name__, exc)
        return None
    if not (title or text):
        return None
    return {
        "title": title,
        "text": text[:1000] if text else "",
        "url": url,
        "image": image,
    }


def _scrape_bs4(
    url: str, query: Optional[str], limit: int
) -> Optional[List[Dict[str, Any]]]:
    """Scrape Tilda content using requests + BeautifulSoup."""
    try:
        resp = requests.get(
            url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
    except _NETWORK_ERRORS as exc:
        logger.info("Tilda BS4 scrape failed: %s: %s", type(exc).__name__, exc)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    records = soup.select(".t-records .t-record, .t-record")[:limit]
    results = _walk_bs4_records(records, url, query)
    if results:
        logger.info("Tilda BS4 scrape: %d items from %s", len(results), url)
        return results
    return None


def _walk_bs4_records(
    records: list, url: str, query: Optional[str]
) -> List[Dict[str, Any]]:
    """Walk BS4 records, applying query filter and skipping malformed nodes."""
    results: List[Dict[str, Any]] = []
    for record in records:
        built = _build_record(record, url)
        if built is None:
            continue
        if not _matches_query(built["title"], built["text"], query):
            continue
        results.append(built)
    return results


# ─────────────────────────────────────────────
# Playwright fallback
# ─────────────────────────────────────────────


_TILDA_EVAL_JS = """() => {
    const results = [];
    const records = document.querySelectorAll(
        '.t-records .t-record, .t-record, [class*="t-rec"]'
    );
    records.forEach(record => {
        const titleSelectors = ['.t-title', '.t-heading', 'h1', 'h2', 'h3', '.title'];
        let title = '';
        for (const sel of titleSelectors) {
            const el = record.querySelector(sel);
            if (el) {
                title = el.innerText.trim();
                if (title) break;
            }
        }
        const textSelectors = ['.t-text', '.t-descr', 'p', '.text', '.description'];
        let text = '';
        for (const sel of textSelectors) {
            const els = record.querySelectorAll(sel);
            els.forEach(el => {
                const t = el.innerText.trim();
                if (t) text += t + ' ';
            });
            if (text) break;
        }
        text = text.trim();
        const imgEl = record.querySelector('img');
        const image = imgEl ? (imgEl.src || imgEl.getAttribute('data-src') || '') : '';
        if (title || text) {
            results.push({ title, text, image });
        }
    });
    return results;
}"""


async def _wait_idle(page: Any) -> None:
    """Wait for networkidle (non-fatal on timeout) plus a 2s settle."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug("Tilda networkidle wait timed out")
    await page.wait_for_timeout(2000)


def _shape_pw_item(
    item: Dict[str, Any], url: str, query: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Shape a Playwright record into our payload dict (or skip on filter)."""
    title = item.get("title", "")
    text = item.get("text", "")
    if not _matches_query(title, text, query):
        return None
    return {
        "title": title,
        "text": text[:1000] if text else "",
        "url": url,
        "image": item.get("image", ""),
    }


def _shape_pw_items(
    data: List[Dict[str, Any]], url: str, query: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Apply :func:`_shape_pw_item` across the truncated record list."""
    out: List[Dict[str, Any]] = []
    for item in (data or [])[:limit]:
        shaped = _shape_pw_item(item, url, query)
        if shaped is not None:
            out.append(shaped)
    return out


async def _run_pw_session(url: str) -> List[Dict[str, Any]]:
    """Drive the Playwright session and return raw records."""
    browser = ctx = page = None
    async with async_playwright() as p:
        try:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("Tilda Playwright fallback for %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _wait_idle(page)
            return await page.evaluate(_TILDA_EVAL_JS) or []
        finally:
            await close_stealth_context(browser, ctx, page)


async def _scrape_playwright(
    url: str, query: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Fallback Playwright scrape via CDP."""
    try:
        raw = await _run_pw_session(url)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "Tilda Playwright scrape failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        return []
    results = _shape_pw_items(raw, url, query, limit)
    logger.info("Tilda Playwright scrape: %d items from %s", len(results), url)
    return results


# ─────────────────────────────────────────────
# Public surface
# ─────────────────────────────────────────────


def scrape_tilda_content(
    url: str, query: Optional[str] = None, limit: int = 20
) -> List[Dict[str, Any]]:
    """Scrape Tilda blocks: BS4 first, Playwright fallback. Never raises."""
    if not url:
        return []
    bs4_results = _scrape_bs4(url, query, limit)
    if bs4_results:
        return bs4_results[:limit]
    pw_results = asyncio.run(_scrape_playwright(url, query, limit))
    return pw_results[:limit]


