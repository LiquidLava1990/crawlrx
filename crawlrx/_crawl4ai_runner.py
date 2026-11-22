"""Alternative ``crawl4ai`` engine: render + LLM-friendly markdown payload.

Owns the branch ``request.engine == "crawl4ai"`` of the /crawl pipeline:
- single-shot stealth render (``_crawl4ai_render_page``)
- optional screenshot (``_crawl4ai_screenshot``)
- public-shape payload + ``CrawlResponse`` mapping (``_crawl4ai_payload`` / ``_crawl4ai_to_response``)
- top-level entry point (``_crawl_via_crawl4ai``)

The thin orchestrator in ``crawl.py`` re-exports the load-bearing names.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Optional

from playwright.async_api import TimeoutError as PWTimeout

from .browser_pool import get_browser
from .config import USER_AGENTS
from .models import CrawlRequest, CrawlResponse
from .utils import (
    create_stealth_context,
    extract_images,
    extract_links,
    extract_title,
    html_to_markdown,
    human_mouse_movements,
    infinite_scroll,
    random_scroll_pause,
    stealth_viewport,
)
from ._crawl_attempt import _wait_state_quiet, close_attempt
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS

logger = logging.getLogger("crawlrx.crawl")

_CRAWL_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, ValueError, TimeoutError, ConnectionError,
    PWTimeout,
)


async def _crawl4ai_screenshot(page: object, url: str) -> Optional[str]:
    """Best-effort base64 screenshot for the crawl4ai engine."""
    try:
        png = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(png).decode("utf-8")
    except (*PLAYWRIGHT_EXCEPTIONS, OSError) as exc:
        logger.warning("crawl4ai-engine screenshot failed: %s", exc)
        return None


def _crawl4ai_payload(
    html: str,
    title: Optional[str],
    status: Optional[int],
    url: str,
    screenshot_b64: Optional[str],
) -> dict:
    """Compose the success payload returned by ``_crawl4ai_extract``."""
    md_text = html_to_markdown(html, base_url=url)
    return {
        "success": bool(md_text or html),
        "markdown": md_text,
        "html": html,
        "links": extract_links(html, base_url=url, same_domain=False, max_count=500),
        "images": extract_images(html, base_url=url, max_count=200),
        "screenshot_b64": screenshot_b64,
        "title": title,
        "status_code": status,
    }


async def _crawl4ai_render_page(
    browser: object, url: str, timeout: int,
) -> tuple[str, Optional[str], Optional[int], object, object]:
    """Open a fresh stealth context, render ``url``, return (html, title, status, ctx, page)."""
    ua = USER_AGENTS[0]
    vp = stealth_viewport()
    context = await create_stealth_context(
        browser, user_agent=ua, viewport=vp, extra_http_headers={},
    )
    page = await context.new_page()
    page.set_default_timeout(min(timeout, 15000))
    response = await page.goto(
        url, timeout=min(timeout, 15000), wait_until="domcontentloaded",
    )
    status = response.status if response else None
    if not await _wait_state_quiet(page, "networkidle", min(timeout, 10000)):
        await page.wait_for_timeout(1500)
    await human_mouse_movements(page, vp)
    await random_scroll_pause(page)
    await infinite_scroll(page, passes=2, wait_ms=800)
    html = await page.content()
    title = await page.title() or extract_title(html)
    return html, title, status, context, page


async def _crawl4ai_extract(url: str, timeout: int = 30000, screenshot: bool = False) -> dict:
    """Render a URL and return CrawlRX-native LLM-friendly markdown."""
    slot = None
    context = None
    page = None
    try:
        browser, slot = await get_browser()
        html, title, status, context, page = await _crawl4ai_render_page(browser, url, timeout)
        screenshot_b64 = await _crawl4ai_screenshot(page, url) if screenshot else None
        return _crawl4ai_payload(html, title, status, url, screenshot_b64)
    except _CRAWL_RETRY_EXCEPTIONS as exc:
        logger.error("crawl4ai-engine error for %s: %s", url, exc, exc_info=True)
        return {"success": False, "error": str(exc), "error_type": "crawl4ai_error"}
    finally:
        await close_attempt(page, context, slot, url)


def _crawl4ai_to_response(c4ai_result: dict, request_url: str, elapsed: int) -> CrawlResponse:
    """Map a crawl4ai engine dict into the public response shape."""
    return CrawlResponse(
        success=c4ai_result.get("success", False),
        url=request_url,
        title=c4ai_result.get("title"),
        markdown=c4ai_result.get("markdown"),
        html=c4ai_result.get("html"),
        text=None,
        links=c4ai_result.get("links", []),
        images=c4ai_result.get("images", []),
        screenshot_b64=c4ai_result.get("screenshot_b64"),
        status_code=200 if c4ai_result.get("success") else None,
        elapsed_ms=elapsed,
        error=c4ai_result.get("error"),
        error_type=c4ai_result.get("error_type"),
        cache_hit=False,
        final_url=request_url,
        redirected=False,
        structured_data=None,
        readable_content=None,
        js_result=None,
    )


async def _crawl_via_crawl4ai(request: CrawlRequest) -> CrawlResponse:
    """Branch handling the alternative crawl4ai engine."""
    start_c4 = time.time()
    c4ai_result = await _crawl4ai_extract(
        str(request.url),
        timeout=request.timeout or 30000,
        screenshot=request.screenshot,
    )
    return _crawl4ai_to_response(
        c4ai_result, str(request.url), int((time.time() - start_c4) * 1000),
    )
