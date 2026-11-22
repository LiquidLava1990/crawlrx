"""Keepa price history scraper for CrawlRX webforge. Uses Playwright CDP."""

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger(__name__)

CDP_PORT = 9240
REQUEST_TIMEOUT = 15000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter(prefix="/scrape", tags=["scrapers"])


class KeepaRequest(BaseModel):
    asin: str


def _extract_price(text: str) -> float | None:
    """Extract numeric price from text string."""
    if not text:
        return None
    match = re.search(r"\$?([\d,]+\.?\d*)", text.replace(",", ""))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


_CURRENT_PRICE_SELECTORS = (
    "[data-testid='current-price']",
    ".price-current",
    ".current-price",
    "[class*='price']",
)
_STATS_FALLBACK_SELECTORS = (
    "[class*='statistics']",
    "[class*='stats']",
    "[class*='history']",
)
_HIST_MIN_RE = re.compile(r"(?:low|min|minimum)[^\d]*(\$?[\d,]+\.?\d*)", re.I)
_HIST_MAX_RE = re.compile(r"(?:high|max|maximum)[^\d]*(\$?[\d,]+\.?\d*)", re.I)
_DEAL_SCORE_RE = re.compile(r"(\d+)/10")
_PRICE_TOKEN_RE = re.compile(r"\$[\d,]+\.?\d*")


def _empty_keepa_record(asin: str) -> dict[str, Any]:
    """Skeleton dict every Keepa probe starts with."""
    return {
        "asin": asin,
        "current": None,
        "price_drop": None,
        "deal_score": None,
        "history_min": None,
        "history_max": None,
        "source": "keepa",
    }


async def _populate_current_price(page: Any, result: dict[str, Any]) -> None:
    """Walk the current-price selector list until one yields a $-prefixed value."""
    for selector in _CURRENT_PRICE_SELECTORS:
        try:
            elem = await page.locator(selector).first
            if not elem.is_visible(timeout=1000):
                continue
            text = elem.text_content()
            if text and "$" in text:
                result["current"] = _extract_price(text)
                return
        except BROWSER_SCRAPE_EXCEPTIONS:
            continue


async def _populate_price_drop(page: Any, result: dict[str, Any]) -> None:
    """Probe the 'price drop' badge."""
    try:
        drop_elem = await page.locator("text=/price drop/i").first
        if drop_elem.is_visible(timeout=1000):
            result["price_drop"] = _extract_price(drop_elem.text_content() or "")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("price_drop probe suppressed (%s): %s", type(exc).__name__, exc)


async def _populate_deal_score(page: Any, result: dict[str, Any]) -> None:
    """Probe the deal-score badge for the ``N/10`` pattern."""
    try:
        deal_elem = await page.locator("[class*='deal'], [class*='score']").first
        if not deal_elem.is_visible(timeout=1000):
            return
        match = _DEAL_SCORE_RE.search(deal_elem.text_content() or "")
        if match:
            result["deal_score"] = int(match.group(1))
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("deal_score probe suppressed (%s): %s", type(exc).__name__, exc)


async def _populate_history_from_text(page: Any, result: dict[str, Any]) -> None:
    """Parse the post-scroll page text for low/high keyword-anchored values."""
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)
        content = await page.content()
        min_m = _HIST_MIN_RE.search(content)
        max_m = _HIST_MAX_RE.search(content)
        if min_m:
            result["history_min"] = _extract_price(min_m.group(1))
        if max_m:
            result["history_max"] = _extract_price(max_m.group(1))
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Failed to extract historical range: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


async def _populate_history_from_stats(page: Any, result: dict[str, Any]) -> None:
    """Fallback: pull min/max from a stats panel when keyword search misses."""
    if result["history_min"] or result["history_max"]:
        return
    for sel in _STATS_FALLBACK_SELECTORS:
        try:
            stats_elem = await page.locator(sel).first
            if not stats_elem.is_visible(timeout=1000):
                continue
            tokens = _PRICE_TOKEN_RE.findall(stats_elem.text_content() or "")
            prices = [p for p in (_extract_price(t) for t in tokens) if p is not None]
            if len(prices) >= 2:
                result["history_min"] = min(prices)
                result["history_max"] = max(prices)
            return
        except BROWSER_SCRAPE_EXCEPTIONS:
            continue


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Best-effort browser teardown."""
    if not browser:
        return
    try:
        await browser.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Browser close error: %s", exc)


async def scrape_keepa(asin: str) -> dict[str, Any]:
    """Scrape price data from Keepa using Playwright.

    Returns dict with current, price_drop, deal_score, history_min, history_max.
    """
    if not asin or not isinstance(asin, str):
        logger.error("Invalid ASIN provided: %s", asin)
        return {}

    asin = asin.strip().upper()
    url = f"https://keepa.com/#!product/1-{asin}"
    browser = ctx = page = None
    result = _empty_keepa_record(asin)
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            page.set_default_timeout(REQUEST_TIMEOUT)
            logger.info("Navigating to Keepa for ASIN %s", asin)
            await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=REQUEST_TIMEOUT)
            await _populate_current_price(page, result)
            await _populate_price_drop(page, result)
            await _populate_deal_score(page, result)
            await _populate_history_from_text(page, result)
            await _populate_history_from_stats(page, result)
            logger.info(
                "Keepa ASIN %s: current=%s, deal_score=%s",
                asin, result["current"], result["deal_score"],
            )
            return result
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Keepa scrape failed for ASIN %s: %s", asin, exc)
        return {}
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


@router.post("/keepa")
def api_scrape_keepa(request: KeepaRequest) -> dict:
    """POST /scrape/keepa - price history by ASIN."""
    import time as _t
    t0 = _t.time()
    if not request.asin or not request.asin.strip():
        return {"success": True, "asin": "", "data": {}, "elapsed_ms": 0}
    try:
        result = scrape_keepa(request.asin)
        elapsed = int((_t.time() - t0) * 1000)
        return {"success": bool(result), "asin": request.asin, "data": result or {}, "elapsed_ms": elapsed}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        elapsed = int((_t.time() - t0) * 1000)
        logger.error("Keepa endpoint error: %s", e)
        return {"success": False, "asin": request.asin, "data": {}, "elapsed_ms": elapsed, "error": str(e)}
