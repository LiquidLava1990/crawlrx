"""webflow.py -- POST /scrape/webflow/page + POST /scrape/webflow/cms

Webflow scraper -- page content and CMS collection extraction.

Strategy (page):
  - Playwright DOM scrape -- verify data-wf-page attribute, extract .w-richtext

Strategy (cms):
  - Playwright DOM scrape -- extract .w-dyn-item collection items
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, TypeVar

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin

from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

_T = TypeVar("_T")

logger = logging.getLogger("crawlrx.webflow")
router = APIRouter(prefix="", tags=["webflow"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class WebflowCMSItem(BaseModel):
    """Single Webflow CMS collection item"""
    title: str = Field(..., description="Item title")
    url: str = Field(..., description="Item URL")
    excerpt: Optional[str] = Field(default=None, description="Item excerpt/summary")
    date: Optional[str] = Field(default=None, description="Published date")
    thumbnail: Optional[str] = Field(default=None, description="Thumbnail image URL")


class WebflowPageRequest(BaseModel):
    """Request model for POST /scrape/webflow/page"""
    url: str = Field(..., description="Webflow site URL (e.g., https://example.webflow.io)")
    timeout: int = Field(default=30000, ge=5000, le=90000, description="Timeout in milliseconds")


class WebflowPageResponse(BaseModel):
    """Response model for POST /scrape/webflow/page"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Page URL")
    title: Optional[str] = Field(default=None, description="Page title")
    content: Optional[str] = Field(default=None, description="Page content")
    cms_items: List[Dict[str, Any]] = Field(default_factory=list, description="CMS items found on page")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


class WebflowCMSRequest(BaseModel):
    """Request model for POST /scrape/webflow/cms"""
    url: str = Field(..., description="Webflow CMS collection URL (e.g., https://example.webflow.io/blog)")
    timeout: int = Field(default=30000, ge=5000, le=90000, description="Timeout in milliseconds")


class WebflowCMSResponse(BaseModel):
    """Response model for POST /scrape/webflow/cms"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Collection URL")
    items: List[WebflowCMSItem] = Field(default_factory=list, description="CMS items")
    total: int = Field(default=0, description="Total items found")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

async def _is_webflow_page(page: Any) -> bool:
    """Check if page has Webflow data-wf-page attribute."""
    try:
        has_wf = await page.evaluate("() => document.querySelector('[data-wf-page]') !== null")
        return bool(has_wf)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("[Webflow] data-wf-page check suppressed: %s", type(exc).__name__)
        return False


async def _extract_richtext_content(page: Any) -> str:
    """Extract content from .w-richtext blocks."""
    content_blocks = await page.query_selector_all(".w-richtext")
    texts: list[str] = []
    for block in content_blocks:
        try:
            text = (await block.inner_text()).strip()
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("[Webflow] richtext block suppressed: %s", type(exc).__name__)
            continue
        if text:
            texts.append(text)
    return "\n\n".join(texts)


async def _wait_for_idle(page: Any, timeout_ms: int) -> None:
    """Best-effort networkidle wait, capped at 15s."""
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
    except PlaywrightTimeoutError:
        return


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a browser, swallowing narrow cleanup failures."""
    if not browser:
        return
    try:
        await browser.close()
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _collect_inline_cms(page: Any, max_items: int = 10) -> list[Dict[str, Any]]:
    """Pull a tiny preview of .w-dyn-item rows on a page."""
    out: list[Dict[str, Any]] = []
    dyn_items = await page.query_selector_all(".w-dyn-item")
    for item in dyn_items[:max_items]:
        try:
            text = (await item.inner_text()).strip()[:200]
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("[Webflow] inline cms item suppressed: %s", type(exc).__name__)
            continue
        out.append({"text": text})
    return out


async def _fallback_body_text(page: Any) -> str:
    """Return body inner_text or empty string."""
    body = await page.query_selector("body")
    if not body:
        return ""
    return (await body.inner_text()).strip()


async def _navigate_and_idle(page: Any, url: str, timeout_ms: int) -> None:
    """goto + networkidle wait."""
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _wait_for_idle(page, timeout_ms)


async def _with_stealth_page(
    runner: Callable[[Any], Awaitable[_T]],
) -> _T:
    """Run `runner(page)` inside a stealth-context lifecycle."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await runner(page)
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


async def _do_page_runner(page: Any, url: str, timeout_ms: int, start_time: float) -> WebflowPageResponse:
    """Body of a Webflow page scrape; runs once a stealth page is open."""
    logger.info("[Webflow] Scraping page: %s", url)
    await _navigate_and_idle(page, url, timeout_ms)
    if not await _is_webflow_page(page):
        logger.warning("[Webflow] Page may not be Webflow (no data-wf-page found): %s", url)
    title = await page.title()
    content = await _extract_richtext_content(page)
    cms_items = await _collect_inline_cms(page)
    if not content:
        content = await _fallback_body_text(page)
    elapsed = int((time.time() - start_time) * 1000)
    return WebflowPageResponse(
        success=True, url=url, title=title, content=content,
        cms_items=cms_items, elapsed_ms=elapsed,
    )


async def _scrape_webflow_page(url: str, timeout_ms: int) -> WebflowPageResponse:
    """Scrape a Webflow page via Playwright."""
    start_time = time.time()
    try:
        return await _with_stealth_page(
            lambda page: _do_page_runner(page, url, timeout_ms, start_time)
        )
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("[Webflow] Page scrape error for %s: %s: %s", url, type(exc).__name__, exc)
        return WebflowPageResponse(
            success=False, url=url, error=str(exc), elapsed_ms=elapsed,
        )


async def _extract_title(item: Any) -> str:
    """Pull title from heading selectors inside an item."""
    el = await item.query_selector("h1, h2, h3, h4, .heading, .title, .post-title")
    return (await el.inner_text()).strip() if el else ""


async def _extract_link(item: Any, base_url: str) -> tuple[str, str]:
    """Return (resolved_url, fallback_link_text)."""
    link_el = await item.query_selector("a")
    if not link_el:
        return base_url, ""
    href = await link_el.get_attribute("href")
    item_url = urljoin(base_url, href) if href else base_url
    link_text = (await link_el.inner_text()).strip()
    return item_url, link_text


async def _extract_excerpt(item: Any) -> Optional[str]:
    """First .excerpt/.summary/.description/p inner_text, capped at 300."""
    el = await item.query_selector(".excerpt, .summary, .description, p")
    if not el:
        return None
    return (await el.inner_text()).strip()[:300]


async def _extract_date(item: Any) -> Optional[str]:
    """Datetime attribute or time element text, or None."""
    el = await item.query_selector(".date, .published, time")
    if not el:
        return None
    attr = await el.get_attribute("datetime")
    if attr:
        return attr
    return (await el.inner_text()).strip()


async def _extract_thumbnail(item: Any) -> Optional[str]:
    """First img src/data-src, or None."""
    img_el = await item.query_selector("img")
    if not img_el:
        return None
    return await img_el.get_attribute("src") or await img_el.get_attribute("data-src")


async def _build_cms_item(item: Any, base_url: str) -> Optional[WebflowCMSItem]:
    """Build one CMS item; return None if no usable title."""
    title = await _extract_title(item)
    item_url, link_text = await _extract_link(item, base_url)
    if not title:
        title = link_text
    if not title:
        return None
    return WebflowCMSItem(
        title=title,
        url=item_url,
        excerpt=await _extract_excerpt(item),
        date=await _extract_date(item),
        thumbnail=await _extract_thumbnail(item),
    )


async def _extract_cms_items_from_dom(page: Any, base_url: str) -> List[WebflowCMSItem]:
    """Extract CMS collection items from DOM using Playwright."""
    items: List[WebflowCMSItem] = []
    dyn_items = await page.query_selector_all(".w-dyn-item")
    for item in dyn_items:
        try:
            built = await _build_cms_item(item, base_url)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("[Webflow] Error extracting CMS item: %s", exc)
            continue
        if built is not None:
            items.append(built)
    return items


async def _scroll_for_more(page: Any, max_attempts: int = 3) -> None:
    """Trigger infinite-scroll up to `max_attempts` rounds."""
    prev_count = 0
    for _ in range(max_attempts):
        items = await page.query_selector_all(".w-dyn-item")
        if len(items) == prev_count:
            break
        prev_count = len(items)
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)


async def _do_cms_runner(page: Any, url: str, timeout_ms: int, start_time: float) -> WebflowCMSResponse:
    """Body of a Webflow CMS scrape; runs once a stealth page is open."""
    logger.info("[Webflow] Scraping CMS collection: %s", url)
    await _navigate_and_idle(page, url, timeout_ms)
    await _scroll_for_more(page)
    items = await _extract_cms_items_from_dom(page, url)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("[Webflow] CMS scrape: %d items from %s", len(items), url)
    return WebflowCMSResponse(
        success=True, url=url, items=items, total=len(items), elapsed_ms=elapsed,
    )


async def _scrape_webflow_cms(url: str, timeout_ms: int) -> WebflowCMSResponse:
    """Scrape Webflow CMS collection via Playwright."""
    start_time = time.time()
    try:
        return await _with_stealth_page(
            lambda page: _do_cms_runner(page, url, timeout_ms, start_time)
        )
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("[Webflow] CMS scrape error for %s: %s: %s", url, type(exc).__name__, exc)
        return WebflowCMSResponse(
            success=False, url=url, error=str(exc), elapsed_ms=elapsed,
        )


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

_REQUEST_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    *CMS_PLAYWRIGHT_EXCEPTIONS,
)


@router.post("/scrape/webflow/page", response_model=WebflowPageResponse)
async def scrape_webflow_page(request: WebflowPageRequest) -> WebflowPageResponse:
    """Scrape a Webflow page (verifies data-wf-page, pulls .w-richtext blocks)."""
    timeout_seconds = max(request.timeout // 1000, 10) + 10
    try:
        return await asyncio.wait_for(
            _scrape_webflow_page(request.url, request.timeout),
            timeout=timeout_seconds,
        )
    except _REQUEST_EXCEPTIONS as exc:
        logger.error("[Webflow] Page request failed: %s: %s", type(exc).__name__, exc)
        return WebflowPageResponse(
            success=False,
            url=request.url,
            error=f"Request failed: {exc}",
            elapsed_ms=request.timeout,
        )


@router.post("/scrape/webflow/cms", response_model=WebflowCMSResponse)
async def scrape_webflow_cms(request: WebflowCMSRequest) -> WebflowCMSResponse:
    """Scrape a Webflow CMS collection (extracts .w-dyn-item rows)."""
    timeout_seconds = max(request.timeout // 1000, 10) + 10
    try:
        return await asyncio.wait_for(
            _scrape_webflow_cms(request.url, request.timeout),
            timeout=timeout_seconds,
        )
    except _REQUEST_EXCEPTIONS as exc:
        logger.error("[Webflow] CMS request failed: %s: %s", type(exc).__name__, exc)
        return WebflowCMSResponse(
            success=False,
            url=request.url,
            error=f"Request failed: {exc}",
            elapsed_ms=request.timeout,
        )
