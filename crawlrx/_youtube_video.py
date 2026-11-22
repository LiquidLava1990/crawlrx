"""YouTube video-page Playwright scraper (sibling module).

Splits :func:`scrape_video_playwright` into per-field probes that run via
:func:`_youtube_helpers.safe_text_extract` -- replacing the ten-deep
nested try/except cascades the legacy implementation carried.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from . import _youtube_helpers as _yh
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.youtube.video")


_TITLE_SELECTOR = (
    "h1.title yt-formatted-string, "
    "h1 .ytd-video-primary-info-renderer, "
    "#title h1"
)
_CHANNEL_SELECTOR = (
    "#channel-name yt-formatted-string a, "
    "ytd-channel-name a, "
    "#owner-name a"
)
_VIEWS_SELECTOR = (
    ".ytd-video-view-count-renderer, "
    "#info .ytd-video-primary-info-renderer, "
    "span.ytd-video-info-renderer"
)
_LIKES_SELECTOR = (
    "#top-level-buttons-computed button[aria-label*='like'], "
    "#segmented-like-button button"
)
_DATE_SELECTOR = (
    "#info-strings yt-formatted-string, "
    "#date .ytd-video-primary-info-renderer"
)
_DESCRIPTION_SELECTOR = (
    "#description-inline-expander, "
    "#description .ytd-video-secondary-info-renderer, "
    "#meta #description"
)
_DESC_EXPAND_SELECTOR = "#expand, tp-yt-paper-button#expand"


async def _goto_video(page: Any, url: str, timeout: int) -> None:
    """Navigate to ``url`` and wait for network idle (best effort)."""
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    except PWTimeout:
        pass
    await page.wait_for_timeout(2000)


async def _extract_title(page: Any) -> str:
    async def _do() -> str:
        el = await page.query_selector(_TITLE_SELECTOR)
        return (await el.inner_text()).strip() if el else ""
    return await _yh.safe_text_extract(_do, field="title", log=logger)


async def _extract_channel(page: Any) -> str:
    async def _do() -> str:
        el = await page.query_selector(_CHANNEL_SELECTOR)
        return (await el.inner_text()).strip() if el else ""
    return await _yh.safe_text_extract(_do, field="channel", log=logger)


async def _extract_views(page: Any) -> str:
    async def _do() -> str:
        el = await page.query_selector(_VIEWS_SELECTOR)
        if not el:
            return ""
        return _yh.parse_views_text((await el.inner_text()).strip())
    return await _yh.safe_text_extract(_do, field="views", log=logger)


async def _extract_likes(page: Any) -> str:
    async def _do() -> str:
        el = await page.query_selector(_LIKES_SELECTOR)
        if not el:
            return ""
        aria = await el.get_attribute("aria-label") or ""
        return _yh.parse_count_with_suffix(aria, label="like|likes")
    return await _yh.safe_text_extract(_do, field="likes", log=logger)


async def _extract_published(page: Any) -> str:
    async def _do() -> str:
        el = await page.query_selector(_DATE_SELECTOR)
        return (await el.inner_text()).strip() if el else ""
    return await _yh.safe_text_extract(_do, field="published", log=logger)


async def _click_show_more(page: Any) -> None:
    """Click the description "Show more" button if it's visible."""
    try:
        button = await page.query_selector(_DESC_EXPAND_SELECTOR)
        if button:
            await button.click()
            await page.wait_for_timeout(500)
    except _yh.ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("show-more click suppressed %s: %s", type(exc).__name__, exc)


async def _extract_description(page: Any) -> str:
    await _click_show_more(page)

    async def _do() -> str:
        el = await page.query_selector(_DESCRIPTION_SELECTOR)
        return (await el.inner_text()).strip() if el else ""
    return await _yh.safe_text_extract(_do, field="description", log=logger)


async def scrape_video_playwright(url: str, timeout: int) -> Dict[str, str]:
    """Scrape a YouTube video page; per-field failures fall through to ``""``."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("Navigating to video: %s", url)
            await _goto_video(page, url, timeout)

            title = await _extract_title(page)
            channel = await _extract_channel(page)
            views = await _extract_views(page)
            likes = await _extract_likes(page)
            published = await _extract_published(page)
            description = await _extract_description(page)

            return {
                "title": title or "Unknown",
                "channel": channel or "Unknown",
                "views": views,
                "likes": likes,
                "published": published,
                "description": description,
            }
    except _yh.PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Playwright video scrape failed (%s): %s", type(exc).__name__, exc)
        raise exc
    finally:
        await close_stealth_context(browser, ctx, page)
