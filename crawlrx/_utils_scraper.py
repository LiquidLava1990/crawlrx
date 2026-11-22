"""Scraper hardening: human-like mouse / scroll, infinite-scroll, screenshots."""
from __future__ import annotations

import logging
import os
import random
import re as _re
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS
from .paths import env_dir

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


SCREENSHOT_DIR = env_dir("CRAWLRX_SCREENSHOT_DIR", "screenshots")


async def human_mouse_movements(page: Page, viewport: dict[str, Any]) -> None:
    """Move the mouse to 3 random positions to mimic human pre-scrape behavior."""
    w = viewport.get("width", 1366)
    h = viewport.get("height", 768)
    try:
        for _ in range(3):
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(random.randint(80, 220))
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Mouse movement skipped: %s", exc)


async def random_scroll_pause(page: Page) -> None:
    """Scroll a random delta down then partway back to mimic page scanning."""
    try:
        delta = random.randint(200, 600)
        await page.evaluate("(d) => window.scrollBy(0, d)", delta)
        await page.wait_for_timeout(random.randint(200, 500))
        await page.evaluate("(d) => window.scrollBy(0, -d)", delta // 2)
        await page.wait_for_timeout(random.randint(100, 300))
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Random scroll skipped: %s", exc)


async def infinite_scroll(page: Page, passes: int = 3, wait_ms: int = 1200) -> None:
    """Scroll-to-bottom up to ``passes`` times; stop early on stall."""
    try:
        prev_height = await page.evaluate("() => document.body.scrollHeight")
        for i in range(passes):
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(wait_ms)
            new_height = await page.evaluate("() => document.body.scrollHeight")
            logger.debug(
                "Infinite scroll pass %d/%d: height %d -> %d",
                i + 1, passes, prev_height, new_height,
            )
            if new_height == prev_height:
                logger.debug("Infinite scroll stalled at pass %d - stopping early", i + 1)
                break
            prev_height = new_height
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Infinite scroll error: %s", exc)


async def save_failure_screenshot(page: Page, url: str) -> Optional[str]:
    """Save a PNG of the page for debugging; returns the path or None on failure."""
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        safe = _re.sub(r"[^\w\-]", "_", url)[:80]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, "%s_%s.png" % (ts, safe))
        await page.screenshot(path=path, full_page=False)
        logger.info("Failure screenshot saved: %s", path)
        return path
    except (OSError, *PLAYWRIGHT_EXCEPTIONS) as exc:
        logger.warning("Could not save failure screenshot: %s", exc)
        return None
