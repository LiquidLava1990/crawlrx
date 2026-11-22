"""YouTube channel-page Playwright scraper (sibling module).

Splits ``_scrape_channel_playwright`` into per-field probes that run via
:func:`_youtube_helpers.safe_text_extract`. The video-list extraction is
isolated in :func:`_extract_videos` so the orchestrator stays small.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from . import _youtube_helpers as _yh
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.youtube.channel")


_NAME_SELECTOR = (
    "#channel-name yt-formatted-string, "
    "ytd-channel-name yt-formatted-string, "
    "#text.ytd-channel-name"
)
_HANDLE_SELECTOR = (
    "#channel-handle, #metadata-container yt-formatted-string#text"
)
_SUBSCRIBERS_SELECTOR = (
    "#subscriber-count, "
    "yt-formatted-string#subscriber-count, "
    "#metadata-container span"
)
_DESC_SELECTOR = (
    "#description-container yt-formatted-string, "
    "#description .ytd-channel-tagline-renderer"
)
_VIDEO_CONTAINER_SELECTORS = (
    "ytd-rich-item-renderer",
    "ytd-grid-video-renderer",
    "ytd-video-renderer",
    "[id='video-title']",
)


async def _goto_videos_tab(page: Any, url: str, timeout: int) -> None:
    """Navigate to the channel /videos tab and settle the page."""
    videos_url = url if "/videos" in url else url.rstrip("/") + "/videos"
    logger.info("Navigating to channel: %s", videos_url)
    await page.goto(videos_url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    except PWTimeout:
        pass
    await page.wait_for_timeout(3000)


async def _read_selector_text(scope: Any, selector: str) -> str:
    """Inner-text for the first matching selector under ``scope`` (``""`` if none)."""
    el = await scope.query_selector(selector)
    return (await el.inner_text()).strip() if el else ""


async def _extract_via_selector(
    page: Any, selector: str, *, field: str,
) -> str:
    """Probe one selector and return its inner-text under safe_text_extract."""
    async def _do() -> str:
        return await _read_selector_text(page, selector)
    return await _yh.safe_text_extract(_do, field=field, log=logger)


async def _extract_name(page: Any) -> str:
    return await _extract_via_selector(page, _NAME_SELECTOR, field="name")


async def _extract_handle(page: Any, channel_url: str) -> str:
    """Read the channel handle, falling back to a ``/@slug`` regex on the URL."""
    direct = await _extract_via_selector(page, _HANDLE_SELECTOR, field="handle")
    if direct:
        return direct
    match = re.search(r"/@([^/]+)", channel_url)
    return f"@{match.group(1)}" if match else ""


async def _extract_subscribers(page: Any) -> str:
    async def _do() -> str:
        text = await _read_selector_text(page, _SUBSCRIBERS_SELECTOR)
        if not text:
            return ""
        return _yh.parse_count_with_suffix(text, label="subscriber|subscribers")
    return await _yh.safe_text_extract(_do, field="subscribers", log=logger)


async def _extract_description(page: Any) -> str:
    return await _extract_via_selector(page, _DESC_SELECTOR, field="description")


async def _scroll_load_videos(page: Any, scrolls: int = 3) -> None:
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(500)


async def _find_video_containers(page: Any) -> List[Any]:
    """Try each candidate selector; return the first non-empty list."""
    for selector in _VIDEO_CONTAINER_SELECTORS:
        elements = await page.query_selector_all(selector)
        if elements:
            return elements
    return []


async def _video_title_url(container: Any) -> Dict[str, str]:
    """Extract title + URL from a single video card; never raises."""
    async def _query() -> Optional[Any]:
        return await container.query_selector(
            "#video-title, a#video-title-link, h3 a"
        )
    title_el = await _yh.safe_query(_query, field="title_el", log=logger)
    if not title_el:
        return {"title": "", "url": ""}

    async def _read_title() -> str:
        text = (await title_el.inner_text()).strip()
        if text:
            return text
        attr = await title_el.get_attribute("title")
        return attr or ""
    title = await _yh.safe_text_extract(_read_title, field="video_title", log=logger)

    async def _read_href() -> str:
        href = await title_el.get_attribute("href")
        if not href:
            return ""
        return ("https://www.youtube.com" + href) if href.startswith("/") else href
    url = await _yh.safe_text_extract(_read_href, field="video_url", log=logger)
    return {"title": title, "url": url}


_META_SPAN_SELECTOR = "#metadata-line span, .ytd-video-meta-block span"


async def _first_meta_text(container: Any) -> str:
    """Inner-text of the first metadata span under a video card."""
    return await _read_selector_text(container, _META_SPAN_SELECTOR)


async def _video_views(container: Any) -> str:
    async def _do() -> str:
        text = await _first_meta_text(container)
        return _yh.parse_views_text(text) if text else ""
    return await _yh.safe_text_extract(_do, field="video_views", log=logger)


async def _video_published(container: Any) -> str:
    """First metadata span that looks like a published-ago marker."""
    async def _do() -> str:
        elements = await container.query_selector_all(_META_SPAN_SELECTOR)
        for meta in elements:
            text = (await meta.inner_text()).strip()
            if _yh.is_published_marker(text):
                return text
        return ""
    return await _yh.safe_text_extract(_do, field="video_published", log=logger)


async def _build_video_record(container: Any) -> Optional[Dict[str, str]]:
    """Build one video card record; return None when title or URL is missing."""
    title_url = await _video_title_url(container)
    if not (title_url["title"] and title_url["url"]):
        return None
    return {
        "title": title_url["title"],
        "url": title_url["url"],
        "views": await _video_views(container),
        "published": await _video_published(container),
    }


async def _extract_videos(page: Any, cap: int = 10) -> List[Dict[str, str]]:
    """Scroll to load video cards and pull up to ``cap`` records."""
    await _scroll_load_videos(page)
    containers = await _find_video_containers(page)
    out: List[Dict[str, str]] = []
    for container in containers[:cap]:
        record = await _build_video_record(container)
        if record is not None:
            out.append(record)
    return out


async def scrape_channel_playwright(url: str, timeout: int) -> Dict[str, Any]:
    """Scrape a YouTube channel page; per-field failures fall through to ``""``."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await _goto_videos_tab(page, url, timeout)
            name = await _extract_name(page)
            handle = await _extract_handle(page, url)
            subscribers = await _extract_subscribers(page)
            description = await _extract_description(page)
            videos = await _extract_videos(page)
            return {
                "name": name or "Unknown Channel",
                "handle": handle,
                "subscribers": subscribers,
                "description": description,
                "videos": videos,
            }
    except _yh.PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "Playwright channel scrape failed (%s): %s", type(exc).__name__, exc,
        )
        raise exc
    finally:
        await close_stealth_context(browser, ctx, page)
