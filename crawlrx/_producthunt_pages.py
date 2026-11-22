"""_producthunt_pages.py -- Playwright-driven page scrapers for Product Hunt.

Sibling helpers for ``producthunt.py``: the three navigation-and-extract
flows (search / today / product detail) plus shared open / close ceremony.
Each extractor JS payload lives in :mod:`_producthunt_js`.
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._ecommerce_product_helpers import BROWSER_OR_HTTP_EXCEPTIONS
from ._producthunt_js import (
    JS_EXTRACT_PRODUCT_DETAIL,
    JS_EXTRACT_SEARCH_RESULTS,
    JS_EXTRACT_TODAY_RESULTS,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx._producthunt_pages")


async def random_delay(min_ms: int = 500, max_ms: int = 1500) -> None:
    """Random delay for stealth (500-1500ms)."""
    import asyncio
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def _wait_for_idle(page: Any, timeout_ms: int) -> None:
    """Wait for ``networkidle``, swallowing the expected timeout."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def _open_page_with_url(
    pw: Any, url: str, networkidle_timeout: int = 10000
) -> tuple[Any, Any, Any]:
    """Open a stealth page, navigate to ``url`` and wait for the network idle."""
    browser, ctx, page = await make_stealth_context(pw)
    await page.set_viewport_size({"width": 1280, "height": 800})
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await random_delay()
    await _wait_for_idle(page, networkidle_timeout)
    await random_delay()
    return browser, ctx, page


async def close_browser_safely(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing narrow shutdown failures."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "producthunt browser close", exc)


async def _evaluate_extractor(
    page: Any, js_payload: str, *args: Any
) -> Any:
    """Run a JS extractor in the page context, returning ``[]`` / ``{}`` on failure."""
    try:
        return await page.evaluate(js_payload, *args)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "producthunt evaluate", exc)
        return None


async def fetch_search_results(query: str, limit: int) -> List[dict]:
    """Search Product Hunt and return raw card dicts."""
    search_url = f"https://www.producthunt.com/search?q={query.replace(' ', '+')}"
    logger.info("[ProductHunt] Searching for: '%s'", query)
    return await _drive_listing_page(search_url, JS_EXTRACT_SEARCH_RESULTS, limit)


async def fetch_today_results(limit: int) -> List[dict]:
    """Fetch today's Product Hunt cards."""
    logger.info("[ProductHunt] Fetching today's products")
    return await _drive_listing_page(
        "https://www.producthunt.com/", JS_EXTRACT_TODAY_RESULTS, limit
    )


async def fetch_product_detail(url: str) -> Optional[dict]:
    """Fetch the full product-detail payload from a Product Hunt URL."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await _open_page_with_url(pw, url, networkidle_timeout=15000)
            return await _evaluate_extractor(page, JS_EXTRACT_PRODUCT_DETAIL)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[ProductHunt] Error scraping product page %s: %s", url, exc)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_safely(browser)


async def _drive_listing_page(url: str, js_payload: str, limit: int) -> List[dict]:
    """Open a listing URL, run the extractor JS, return raw card dicts."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await _open_page_with_url(pw, url)
            raw = await _evaluate_extractor(page, js_payload, limit)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[ProductHunt] Listing error for %s: %s", url, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_safely(browser)
    if isinstance(raw, list):
        logger.info("[ProductHunt] Found %d products", len(raw))
        return raw
    return []
