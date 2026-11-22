"""Chrome DevTools Protocol (CDP) browser helpers.

``ensure_cdp_browser`` checks that a CDP endpoint answers (CrawlRX never
launches programs for you; start Chrome with ``--remote-debugging-port``).
Also bundles the stealth page context manager (``get_cdp_page``) and
Playwright navigation helpers (``safe_goto``, ``extract_text``).
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Optional

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS
from .config import CDP_HOST
from .stealth_context import close_stealth_context, make_stealth_context

if TYPE_CHECKING:
    from playwright.async_api import Page

_CDP_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    urllib.error.URLError,
)

logger = logging.getLogger(__name__)


def _cdp_is_up(port: int, timeout: float = 2.0) -> bool:
    """Return True if a CDP endpoint is responding on ``port`` (sync/blocking)."""
    try:
        urllib.request.urlopen("http://%s:%d/json/version" % (CDP_HOST, port), timeout=timeout)
        return True
    except _CDP_PROBE_EXCEPTIONS:
        return False


async def ensure_cdp_browser(port: int, wait_seconds: float = 5.0) -> bool:
    """True once a CDP browser answers on ``port`` (polls up to ``wait_seconds``)."""
    deadline = time.time() + wait_seconds
    while True:
        if await asyncio.to_thread(_cdp_is_up, port):
            return True
        if time.time() >= deadline:
            break
        await asyncio.sleep(0.5)
    logger.warning(
        "No browser answering on CDP port %s. Start Chrome with "
        "--remote-debugging-port=%s (and a separate --user-data-dir), or omit the CDP option.",
        port, port,
    )
    return False


# ---------------------------------------------------------------------------
# CDP page context manager + navigation helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_cdp_page(
    agent: Optional[str] = None,
    stealth: bool = True,
    timeout: int = 30000,
) -> "AsyncIterator[Page]":  # noqa: ARG001  reason: agent kept for API compatibility
    """Context manager: launch a fresh headless browser, yield a stealth page.

    Yields the page; the surrounding ``async with`` closes the browser.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if stealth:
            browser, ctx, page = await make_stealth_context(p)
        else:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context()
            page = await ctx.new_page()
        try:
            page.set_default_timeout(timeout)
            yield page
        finally:
            await close_stealth_context(browser, ctx, page)


async def safe_goto(
    page: "Page",
    url: str,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
) -> bool:
    """Navigate to ``url``; True on success, False on timeout/error."""
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except PLAYWRIGHT_EXCEPTIONS:
        return False


async def extract_text(page: "Page", selector: str, default: str = "") -> str:
    """Extract text from ``selector``; ``default`` if not found."""
    try:
        el = await page.query_selector(selector)
        return (await el.inner_text()).strip() if el else default
    except (*PLAYWRIGHT_EXCEPTIONS, AttributeError):
        return default
