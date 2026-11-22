"""
browser_pool.py -- Fresh browser per request for CrawlRX.

Every request: launch browser -> do work -> close browser -> stop playwright.
No persistent browsers. Semaphore caps concurrent browser count.

Fully async -- uses async_playwright and asyncio.Semaphore.

await get_browser()      -- acquires semaphore slot, launches fresh headless browser
await release_browser(slot) -- closes browser, stops playwright, releases semaphore slot
"""

import asyncio
import os
import logging
from typing import Any, Optional, Tuple

from ._scraper_helpers import playwright_exception_types
from .captcha_handoff import browser_headless

logger = logging.getLogger("crawlrx.browser_pool")
_POOL_SIZE = int(os.getenv("CRAWLRX_BROWSER_POOL", "2"))
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

BrowserSlot = Optional[Tuple[Any, Any]]

# Narrow exception families for browser launch + cleanup. Anything beyond
# Playwright + asyncio + OS / runtime errors is a real bug and propagates.
_LAUNCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError,)
)
_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = _LAUNCH_EXCEPTIONS

# Lazy semaphore -- created on first use inside a running event loop.
# Stored as a single-slot list so the helpers below can mutate without `global`.
_semaphore_slot: list[asyncio.Semaphore] = []


async def get_browser() -> Tuple[Any, BrowserSlot]:
    """Acquire a fresh headless browser; release slot via ``release_browser``."""
    if not _semaphore_slot:
        _semaphore_slot.append(asyncio.Semaphore(_POOL_SIZE))
    semaphore = _semaphore_slot[0]
    await semaphore.acquire()
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=browser_headless(), args=_LAUNCH_ARGS)
        logger.debug("BrowserPool: launched fresh browser")
        return browser, (pw, browser)
    except _LAUNCH_EXCEPTIONS:
        semaphore.release()
        raise


async def release_browser(slot: BrowserSlot) -> None:
    """Close browser, stop playwright, release semaphore. Call in finally block."""
    if not _semaphore_slot:
        _semaphore_slot.append(asyncio.Semaphore(_POOL_SIZE))
    semaphore = _semaphore_slot[0]
    if slot is None:
        semaphore.release()
        return
    pw, browser = slot
    try:
        await browser.close()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.debug("release_browser close (%s): %s", type(exc).__name__, exc)
    try:
        await pw.stop()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.debug("release_browser pw stop (%s): %s", type(exc).__name__, exc)
    semaphore.release()
    logger.debug("BrowserPool: browser closed + released")
