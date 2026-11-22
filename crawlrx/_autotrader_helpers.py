"""Helpers shared by autotrader.py / cars_com.py (module split).

Bundles param-bag dataclasses + a Playwright "fresh browser, single page"
context manager so the two scrapers don't replicate try/finally cleanup.
"""

from __future__ import annotations

import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from ._scraper_helpers import playwright_exception_types

logger = logging.getLogger("crawlrx._autotrader_helpers")

LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}

# Cleanup failures are best-effort -- anything Playwright + async + OS may
# raise. Anything else is a real bug and propagates.
CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError,)
)


@dataclass(frozen=True)
class AutoTraderSearchArgs:
    """Bundle of /search query parameters; keeps URL-build + scrape ≤5 args."""

    make: Optional[str] = None
    model: Optional[str] = None
    zip_code: str = "40202"
    max_price: Optional[int] = None
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    max_mileage: Optional[int] = None
    listing_type: str = "any"
    limit: int = 20


@dataclass
class _BrowserSession:
    """Mutable bag passed by :func:`fresh_browser_session`."""

    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    new_context: bool = False


async def close_quietly(target: "Page | BrowserContext | Browser | None", label: str) -> None:
    """Best-effort async ``.close()`` that logs and swallows known failures."""
    if target is None:
        return
    try:
        await target.close()
    except CLEANUP_EXCEPTIONS as exc:
        logger.debug(
            "Cleanup %s failed (%s): %s", label, type(exc).__name__, exc
        )


async def _attach_context(
    browser: Browser,
    user_agents: list[str],
) -> tuple[BrowserContext, bool]:
    """Reuse the first existing context, or build a fresh one with stealth UA."""
    contexts = browser.contexts
    if contexts:
        return contexts[0], False
    context = await browser.new_context(
        user_agent=random.choice(user_agents),
        viewport=dict(DEFAULT_VIEWPORT),
    )
    return context, True


@asynccontextmanager
async def fresh_browser_session(
    user_agents: list[str],
) -> AsyncIterator[_BrowserSession]:
    """Launch a fresh headless browser and yield a session with one open page.

    On exit closes the page + (if newly-created) the context. The browser is
    closed by ``async_playwright()``'s own ``__aexit__``.
    """
    session = _BrowserSession()
    async with async_playwright() as p:
        session.browser = await p.chromium.launch(
            headless=True,
            args=list(LAUNCH_ARGS),
        )
        try:
            session.context, session.new_context = await _attach_context(
                session.browser, user_agents
            )
            session.page = await session.context.new_page()
            yield session
        finally:
            await close_quietly(session.page, "page")
            if session.new_context:
                await close_quietly(session.context, "context")
