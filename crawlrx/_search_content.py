"""Per-result content fetcher (split from search.py)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from ._search_engines import random_ua
from ._search_helpers import SERP_BROWSER_EXCEPTIONS, safe_close_resources
from .browser_pool import get_browser, release_browser
from .utils import is_safe_url

logger = logging.getLogger("crawlrx.search.content")

Page = Any

_CONTENT_FETCH_SEMAPHORE: Optional[asyncio.Semaphore] = None


def get_content_fetch_semaphore() -> asyncio.Semaphore:
    """Lazy semaphore -- bound to running loop on first use."""
    global _CONTENT_FETCH_SEMAPHORE
    if _CONTENT_FETCH_SEMAPHORE is None:
        _CONTENT_FETCH_SEMAPHORE = asyncio.Semaphore(3)
    return _CONTENT_FETCH_SEMAPHORE


def _strip_html_with_bs4(html: str) -> Optional[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except (ValueError, TypeError) as exc:
        logger.debug("bs4 parse error: %s", type(exc).__name__)
        return None
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def _strip_html_regex(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    """Extract plain text from HTML (bs4 preferred, regex fallback)."""
    out = _strip_html_with_bs4(html)
    if out is not None:
        return out
    return _strip_html_regex(html)


async def _attempt_networkidle(page: Page, timeout_ms: int, pw_timeout) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
    except pw_timeout:
        return


async def fetch_url_content(url: str, timeout_ms: int = 15000, agent: Optional[str] = None) -> Optional[str]:
    """Fetch and extract plain text from a URL using the browser pool."""
    from playwright.async_api import TimeoutError as PWTimeout

    if not is_safe_url(url):
        logger.warning("Blocked unsafe search content fetch URL: %s", url)
        return None

    browser_obj = None
    slot = None
    context = None
    page: Optional[Page] = None
    try:
        browser_obj, slot = await get_browser()
        context = await browser_obj.new_context(user_agent=random_ua())
        page = await context.new_page()
        content_timeout = min(timeout_ms, 15000)
        page.set_default_timeout(content_timeout)
        await page.goto(url, timeout=content_timeout, wait_until="domcontentloaded")
        await _attempt_networkidle(page, timeout_ms, PWTimeout)
        html = await page.content()
        return html_to_text(html)
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.warning("Content fetch failed for %s: %s", url, type(exc).__name__)
        return None
    finally:
        await safe_close_resources(logger, (page, "page"), (context, "context"))
        if slot is not None:
            await release_browser(slot)


def _truncate_content(content: str, limit: int = 2000) -> str:
    if len(content) <= limit:
        return content
    return content[:limit].rsplit(" ", 1)[0] + "..."


async def fetch_content_for_result(
    result: dict, timeout_ms: int, agent: Optional[str] = None,
) -> dict:
    """Fetch content for a single search result with concurrency control."""
    async with get_content_fetch_semaphore():
        content = await fetch_url_content(result["url"], timeout_ms, agent)
        if content:
            content = _truncate_content(content)
        result["content"] = content
        return result
