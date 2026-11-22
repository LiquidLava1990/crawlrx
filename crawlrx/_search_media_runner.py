"""Generic Bing media-search runner (split from search_media.py).

Shared infrastructure for ``/search/images``, ``/search/news``, ``/search/videos``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional
from urllib.parse import quote

from ._search_helpers import (
    SERP_BROWSER_EXCEPTIONS,
    SERP_PARSE_EXCEPTIONS,
    safe_close_resources,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.search_media.runner")

Page = Any
ExtractorFn = Callable[[Page, int], Awaitable[List[dict]]]

_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MEDIA_VIEWPORT = {"width": 1920, "height": 1080}


@dataclass(frozen=True)
class MediaSearchSpec:
    """Bundled args for one media-search call."""

    query: str
    count: int
    timeout: int
    section: str  # "images" / "news" / "videos"
    extractor: ExtractorFn
    not_found_label: str  # "image" / "news article" / "video"


def _empty_media_result(query: str) -> dict:
    return {
        "success": False,
        "query": query,
        "engine": "bing",
        "results": [],
        "count": 0,
        "error": None,
    }


def _build_section_url(section: str, query: str) -> str:
    return "https://www.bing.com/%s/search?q=%s" % (section, quote(query))


async def _attempt_networkidle(
    page: Page, timeout_ms: int, pw_timeout,
) -> bool:
    """Wait for ``networkidle``; True iff achieved before deadline."""
    deadline = min(timeout_ms, 20000)
    try:
        await page.wait_for_load_state("networkidle", timeout=deadline)
    except pw_timeout as exc:
        logger.debug("Networkidle wait timed out: %s", type(exc).__name__)
        return False
    return True


async def _open_bing_page(p: Any, timeout_ms: int) -> tuple[Any, Any, Page]:
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=_MEDIA_USER_AGENT,
        viewport=_MEDIA_VIEWPORT,
    )
    page = await context.new_page()
    page.set_default_timeout(timeout_ms)
    return browser, context, page


def _record_success(result: dict, items: List[dict], section: str) -> None:
    result["success"] = True
    result["results"] = items
    result["count"] = len(items)
    logger.info("Extracted %d %s from Bing", len(items), section)


def _apply_extraction(
    result: dict, items: List[dict], section: str, query: str, label: str,
) -> None:
    if items:
        _record_success(result, items, section)
        return
    result["error"] = "No %s results found" % label
    logger.warning("No %s found for query: %s", label, query)


async def run_bing_media_search(spec: MediaSearchSpec) -> dict:
    """Synchronous Playwright search for a Bing media section."""
    from playwright.async_api import (
        TimeoutError as PWTimeout,
        async_playwright,
    )

    start = timer_ms()
    result = _empty_media_result(spec.query)
    browser = None
    context = None
    page: Optional[Page] = None

    try:
        async with async_playwright() as p:
            browser, context, page = await _open_bing_page(p, spec.timeout)
            url = _build_section_url(spec.section, spec.query)
            logger.info("Navigating to Bing %s: %s", spec.section, url)
            await page.goto(url, timeout=spec.timeout, wait_until="domcontentloaded")
            await _attempt_networkidle(page, spec.timeout, PWTimeout)
            items = await spec.extractor(page, spec.count)
            _apply_extraction(
                result, items, spec.section, spec.query, spec.not_found_label,
            )
    except PWTimeout as exc:
        result["error"] = "Timeout waiting for Bing %s: %s" % (spec.section, exc)
        logger.error("Timeout for %s query '%s': %s",
                     spec.not_found_label, spec.query, type(exc).__name__)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except SERP_BROWSER_EXCEPTIONS as exc:
        result["error"] = "Search failed: %s" % type(exc).__name__
        logger.error(
            "Error searching Bing %s for '%s': %s",
            spec.section, spec.query, type(exc).__name__,
        )
        return None  # explicit recovery: non-fatal: SERP_BROWSER_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await safe_close_resources(
            logger, (page, "page"), (context, "context"), (browser, "browser"),
        )
        result["elapsed_ms"] = int(timer_ms() - start)
    return result


async def run_with_wall_timeout(
    spec: MediaSearchSpec, wall_timeout_s: int,
) -> dict:
    """Wrap a search in a wall-clock timeout."""
    try:
        return await asyncio.wait_for(
            run_bing_media_search(spec), timeout=wall_timeout_s,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for %s search", spec.section)
        return {
            "success": False,
            "query": spec.query,
            "engine": "bing",
            "results": [],
            "count": 0,
            "error": "Global timeout exceeded (%ds wall-clock)" % wall_timeout_s,
            "elapsed_ms": wall_timeout_s * 1000,
        }


# ─────────────────────────────────────────────
# Generic card-iteration helper (extractors share this)
# ─────────────────────────────────────────────

async def _query_first_match(page_or_card, selectors: tuple) -> list:
    for selector in selectors:
        cards = await page_or_card.query_selector_all(selector)
        if cards:
            return cards
    return []


def _playwright_wait_exceptions() -> tuple[type[BaseException], ...]:
    """Return Playwright + asyncio failure classes raised by ``wait_for_selector``."""
    base: list[type[BaseException]] = [asyncio.TimeoutError, OSError, RuntimeError]
    try:
        from playwright.async_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
        )
        base.extend([PlaywrightError, PlaywrightTimeoutError])
    except ImportError:
        pass
    return tuple(base)


_WAIT_SELECTOR_EXCEPTIONS: tuple[type[BaseException], ...] = (
    SERP_PARSE_EXCEPTIONS + _playwright_wait_exceptions()
)


async def _wait_any_selector(
    page: Page, selectors: list[str], timeout_ms: int,
) -> bool:
    """Wait for the first selector that resolves; True iff any matched."""
    waiter = ", ".join(selectors)
    try:
        await page.wait_for_selector(waiter, timeout=timeout_ms)
        return True
    except _WAIT_SELECTOR_EXCEPTIONS as exc:
        logger.debug("wait_for_selector failed: %s", type(exc).__name__)
        return False


async def collect_cards_via_selectors(
    page: Page, selectors: list[str], timeout_ms: int = 10000,
) -> list:
    """Wait for any selector then return the first non-empty card list."""
    await _wait_any_selector(page, selectors, timeout_ms)
    await page.wait_for_timeout(2000)
    return await _query_first_match(page, tuple(selectors))
