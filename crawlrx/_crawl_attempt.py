"""Single Playwright crawl attempt + retry loop.

Owns:
- ``CrawlAttemptParams`` dataclass (one bundle per attempt, no 25-arg signature)
- Page-flow helpers (waits, scroll, JS-inject, screenshot, PDF detection)
- Per-attempt orchestrator ``_do_crawl_attempt``
- Retry loop ``_crawl_with_retries`` and the legacy positional-arg shim ``_do_crawl``

The thin orchestrator in ``crawl.py`` re-exports the load-bearing names so
``crawlrx.batch._do_crawl`` and direct callers keep working.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from playwright.async_api import TimeoutError as PWTimeout

from .browser_pool import get_browser, release_browser
from .captcha_handoff import clear_or_flag
from .config import USER_AGENTS
from .utils import (
    check_content_size,
    create_stealth_context,
    detect_bot_block,
    extract_images,
    extract_links,
    extract_readable_content,
    extract_structured_data,
    extract_title,
    html_to_markdown,
    human_mouse_movements,
    infinite_scroll,
    random_scroll_pause,
    save_failure_screenshot,
    stealth_viewport,
    timer_ms,
)
from ._scraper_helpers import PARSE_EXCEPTIONS, PLAYWRIGHT_EXCEPTIONS

logger = logging.getLogger("crawlrx.crawl")

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.5, 3.0]  # seconds between retry 1->2, 2->3

_PDF_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, ValueError, RuntimeError, TimeoutError, ConnectionError,
)
_CRAWL_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, ValueError, TimeoutError, ConnectionError,
    PWTimeout,
)


# ---------------------------------------------------------------------------
# HTML / PDF helpers
# ---------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    """Extract plain text from HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except (ImportError, *PARSE_EXCEPTIONS):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _extract_pdf(url: str, content: bytes) -> Optional[str]:
    """Extract text from PDF bytes via pdfplumber."""
    try:
        import pdfplumber
        import io
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        return None


def _fetch_pdf_sync(url: str) -> dict:
    """Fetch + extract a PDF URL (sync; called via asyncio.to_thread)."""
    import requests as req_lib
    try:
        resp = req_lib.get(url, timeout=30)
        text = _extract_pdf(url, resp.content)
        if text is None:
            return {"error": "PDF detected but pdfplumber not installed. Run: pip install pdfplumber"}
        return {"success": True, "title": url.split("/")[-1], "text": text, "final_url": url}
    except _PDF_FETCH_EXCEPTIONS as e:
        return {"error": "PDF extraction failed: %s" % e}


async def _fetch_pdf(url: str) -> dict:
    """Fetch + extract PDF off the event loop (30s blocking guard)."""
    return await asyncio.to_thread(_fetch_pdf_sync, url)


# ---------------------------------------------------------------------------
# Page-wait helpers
# ---------------------------------------------------------------------------


async def _wait_state_quiet(page: object, state: str, timeout_ms: int) -> bool:
    """Wait for a Playwright load state, swallow PWTimeout. True on success."""
    try:
        await page.wait_for_load_state(state, timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


async def _wait_default(page: object, timeout: int) -> None:
    """Default wait: try networkidle, then domcontentloaded + settle."""
    if await _wait_state_quiet(page, "networkidle", min(timeout, 10000)):
        return
    await _wait_state_quiet(page, "domcontentloaded", min(timeout, 8000))
    await page.wait_for_timeout(1500)


async def _wait_for_selector_quiet(page: object, selector: str, timeout_ms: int) -> None:
    """Wait for a CSS selector; warn-and-continue on timeout."""
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    except PWTimeout:
        logger.warning("CSS selector wait_for '%s' timed out", selector)
        return None  # explicit recovery: best-effort wait; PWTimeout is non-fatal


async def _wait_for_page(page: object, wait_for: Optional[str], timeout: int) -> None:
    """Smart wait: None|networkidle|load|domcontentloaded|<selector>."""
    if wait_for is None:
        await _wait_default(page, timeout)
        return
    if wait_for == "networkidle":
        await _wait_state_quiet(page, "networkidle", min(timeout, 15000))
        return
    if wait_for == "load":
        await _wait_state_quiet(page, "load", timeout)
        return
    if wait_for == "domcontentloaded":
        return
    await _wait_for_selector_quiet(page, wait_for, min(timeout, 5000))


# ---------------------------------------------------------------------------
# Per-attempt parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrawlAttemptParams:
    """Bundle of options for a single crawl attempt (eliminates 25-param signature)."""

    url: str
    wait_for: Optional[str]
    timeout: int
    javascript: bool = True
    extract_links_flag: bool = True
    extract_text_flag: bool = True
    extract_markdown_flag: bool = True
    extract_images_flag: bool = True
    user_agent: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    screenshot: bool = False
    scroll: bool = False
    agent: Optional[str] = None
    include_structured_data: bool = False
    stealth: bool = True
    include_readable: bool = False
    same_domain_only: bool = False
    max_links: int = 500
    max_images: int = 200
    js_inject: Optional[str] = None
    js_inject_timeout: int = 5000
    wait_for_text: Optional[str] = None
    wait_for_gone: Optional[str] = None
    wait_for_timeout_extra: int = 0


def _empty_crawl_result(url: str) -> dict:
    """Skeleton dict that every code path fills in."""
    return {
        "success": False, "url": url, "status_code": None,
        "title": None, "html": None, "text": None, "markdown": None,
        "links": [], "images": [], "screenshot_b64": None,
        "readable_content": None, "elapsed_ms": 0, "error": None,
        "final_url": None, "redirected": False,
        "structured_data": None, "js_result": None,
    }


async def _make_crawl_context(browser: object, params: CrawlAttemptParams, ua: str, vp: dict) -> object:
    """Create a fresh Playwright BrowserContext for one crawl attempt."""
    if params.stealth:
        return await create_stealth_context(
            browser,
            user_agent=ua,
            viewport=vp,
            extra_http_headers=params.headers or {},
        )
    ctx_kwargs: dict = {
        "user_agent": ua,
        "java_script_enabled": params.javascript,
        "viewport": vp,
    }
    if params.headers:
        ctx_kwargs["extra_http_headers"] = params.headers
    return await browser.new_context(**ctx_kwargs)


# ---------------------------------------------------------------------------
# Page-flow side-effects (waits / mouse / JS / screenshot)
# ---------------------------------------------------------------------------


async def _await_extra_waits(page: object, params: CrawlAttemptParams) -> None:
    """Honour wait_for_text / wait_for_gone / wait_for_timeout_extra."""
    if params.wait_for_text:
        try:
            await page.wait_for_function(
                "(text) => document.body.innerText.includes(text)",
                arg=params.wait_for_text,
                timeout=min(params.timeout, 15000),
            )
        except (PWTimeout, RuntimeError):
            logger.warning(
                "wait_for_text '%s' not found on %s",
                params.wait_for_text, params.url,
            )
            return None  # explicit recovery: non-fatal: (PWTimeout, RuntimeError) is recorded but does not abort the caller
    if params.wait_for_gone:
        try:
            await page.wait_for_selector(
                params.wait_for_gone, state="hidden",
                timeout=min(params.timeout, 15000),
            )
        except (PWTimeout, RuntimeError) as exc:
            logger.debug("wait_for_gone suppressed: %s", exc)
    if params.wait_for_timeout_extra > 0:
        await page.wait_for_timeout(params.wait_for_timeout_extra)


async def _run_js_inject(page: object, params: CrawlAttemptParams, result: dict) -> None:
    """Run user-supplied pre-extraction JavaScript (best-effort)."""
    if not params.js_inject:
        return
    try:
        js_val = await asyncio.wait_for(
            page.evaluate(params.js_inject),
            timeout=min(params.js_inject_timeout / 1000, 10),
        )
        if isinstance(js_val, (dict, list)):
            result["js_result"] = js_val
        await page.wait_for_timeout(min(params.js_inject_timeout, 2000))
    except asyncio.TimeoutError:
        logger.warning("js_inject timed out on %s", params.url)
        return None  # explicit recovery: non-fatal: asyncio.TimeoutError is recorded but does not abort the caller
    except (*PLAYWRIGHT_EXCEPTIONS, ValueError, TypeError) as exc:
        logger.warning("js_inject failed on %s: %s", params.url, exc)
        return None  # explicit recovery: non-fatal: (*PLAYWRIGHT_EXCEPTIONS, ValueError, TypeError) is recorded but does not abort the caller


def _populate_result_artifacts(result: dict, html: str, params: CrawlAttemptParams) -> None:
    """Fill the optional extraction fields based on the request flags."""
    url = params.url
    if params.extract_text_flag:
        result["text"] = _html_to_text(html)
    if params.extract_markdown_flag:
        result["markdown"] = html_to_markdown(html, base_url=url)
    if params.extract_links_flag:
        result["links"] = extract_links(
            html, base_url=url, same_domain=params.same_domain_only,
            max_count=params.max_links,
        )
    if params.extract_images_flag:
        result["images"] = extract_images(html, base_url=url, max_count=params.max_images)
    if params.include_structured_data:
        result["structured_data"] = extract_structured_data(html)
    if params.include_readable:
        result["readable_content"] = extract_readable_content(html, url)


async def _maybe_screenshot(page: object, params: CrawlAttemptParams, result: dict) -> None:
    """Capture a base64-encoded PNG when the request asks for one."""
    if not params.screenshot:
        return
    try:
        png = await page.screenshot(type="png", full_page=False)
        result["screenshot_b64"] = base64.b64encode(png).decode("utf-8")
    except (*PLAYWRIGHT_EXCEPTIONS, OSError) as exc:
        logger.warning("Screenshot failed: %s", exc)
        return None  # explicit recovery: non-fatal: (*PLAYWRIGHT_EXCEPTIONS, OSError) is recorded but does not abort the caller


# ---------------------------------------------------------------------------
# Cleanup + crash handling
# ---------------------------------------------------------------------------


async def _safe_close_one(obj: object, name: str, url: str) -> None:
    """Close a single browser object, swallowing recoverable errors."""
    if obj is None:
        return
    try:
        await obj.close()
    except (*PLAYWRIGHT_EXCEPTIONS, OSError) as exc:
        logger.warning("Error closing %s for %s: %s", name, url, exc)
        return  # explicit recovery: cleanup is best-effort; non-fatal


async def close_attempt(page: object, context: object, slot: object, url: str) -> None:
    """Best-effort cleanup of page+context+browser slot."""
    await _safe_close_one(page, "page", url)
    await _safe_close_one(context, "context", url)
    if slot is not None:
        await release_browser(slot)


async def _save_crash_screenshot(page: Optional[object], url: str) -> None:
    """Try to capture a debug screenshot when an exception fires."""
    if page is None:
        return
    try:
        await asyncio.wait_for(save_failure_screenshot(page, url), timeout=5)
    except (*PLAYWRIGHT_EXCEPTIONS, asyncio.TimeoutError, OSError) as exc:
        logger.debug("save_failure_screenshot suppressed: %s", exc)


# ---------------------------------------------------------------------------
# Per-attempt main flow
# ---------------------------------------------------------------------------


async def _do_crawl_pdf_response(page: object, response: object, url: str) -> Optional[dict]:
    """Detect PDF responses and return the PDF result; None otherwise."""
    content_type = (response.headers.get("content-type", "") if response and response.headers else "")
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        return await _fetch_pdf(url)
    return None


async def _do_crawl_attempt_body(page: object, params: CrawlAttemptParams, result: dict) -> Optional[dict]:
    """Inner crawl flow once the page is open (returns a result OR None to continue)."""
    response = await page.goto(
        params.url, timeout=min(params.timeout, 30000),
        wait_until="domcontentloaded",
    )
    result["status_code"] = response.status if response else None
    final_url = page.url
    result["final_url"] = final_url
    result["redirected"] = (final_url.rstrip("/") != params.url.rstrip("/"))
    pdf_result = await _do_crawl_pdf_response(page, response, params.url)
    if pdf_result is not None:
        return pdf_result
    await _wait_for_page(page, params.wait_for, params.timeout)
    await _await_extra_waits(page, params)
    await human_mouse_movements(page, stealth_viewport())
    await random_scroll_pause(page)
    await _run_js_inject(page, params, result)
    if not await check_content_size(page):
        result["error"] = "Page too large (>10MB)"
        return result
    if params.scroll:
        await infinite_scroll(page, passes=3, wait_ms=1200)
    html = await page.content()
    title = await page.title() or extract_title(html)
    result["title"] = title
    if detect_bot_block(html, title or ""):
        refreshed = await clear_or_flag(page, params.url, html, title or "", result)
        if refreshed is None:
            return result
        html, title = refreshed[0], refreshed[1] or extract_title(refreshed[0])
        result["title"] = title
    result["html"] = html
    _populate_result_artifacts(result, html, params)
    await _maybe_screenshot(page, params, result)
    result["success"] = True
    return None


async def _do_crawl_attempt(params: CrawlAttemptParams, start: float) -> dict:
    """One crawl attempt -- fresh context+page, closed in finally."""
    result = _empty_crawl_result(params.url)
    ua = params.user_agent or USER_AGENTS[0]
    vp = stealth_viewport()
    slot = None
    context = None
    page = None
    try:
        browser, slot = await get_browser()
        context = await _make_crawl_context(browser, params, ua, vp)
        page = await context.new_page()
        page.set_default_timeout(min(params.timeout, 30000))
        early = await _do_crawl_attempt_body(page, params, result)
        if early is not None:
            return early
    except PWTimeout as exc:
        result["error"] = "Timeout: %s" % exc
        logger.error("Crawl timeout for %s: %s", params.url, exc)
        await _save_crash_screenshot(page, params.url)
        raise exc
    except _CRAWL_RETRY_EXCEPTIONS as exc:
        result["error"] = str(exc)
        logger.error("Crawl error for %s: %s", params.url, exc, exc_info=True)
        await _save_crash_screenshot(page, params.url)
        raise exc
    finally:
        await close_attempt(page, context, slot, params.url)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def _empty_failure_result(url: str, last_err: Optional[BaseException], elapsed_ms: int) -> dict:
    """Result dict for a crawl that exhausted all retries."""
    out = _empty_crawl_result(url)
    out["error"] = str(last_err) if last_err else "unknown error"
    out["elapsed_ms"] = elapsed_ms
    return out


async def _handle_retry_failure(
    exc: BaseException,
    attempt: int,
    url: str,
) -> None:
    """Log retry context and sleep before the next attempt."""
    if attempt >= _MAX_RETRIES:
        logger.error(
            "All %d crawl attempts failed for %s: %s",
            _MAX_RETRIES, url, exc,
        )
        return
    wait_s = _RETRY_BACKOFF[attempt - 1]
    logger.warning(
        "Crawl attempt %d/%d failed for %s: %s -- retrying in %.1fs",
        attempt, _MAX_RETRIES, url, exc, wait_s,
    )
    await asyncio.sleep(wait_s)


async def _try_one_crawl_attempt(
    params: CrawlAttemptParams,
    start: float,
    attempt: int,
) -> tuple[Optional[dict], Optional[BaseException]]:
    """Run one crawl attempt: returns (result, error)."""
    try:
        result = await _do_crawl_attempt(params, start)
        result["elapsed_ms"] = int(timer_ms() - start)
        return result, None
    except _CRAWL_RETRY_EXCEPTIONS as exc:
        await _handle_retry_failure(exc, attempt, params.url)
        return None, exc


async def _crawl_with_retries(params: CrawlAttemptParams, start: float) -> dict:
    """Run up to _MAX_RETRIES crawl attempts; first success or final failure."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        result, err = await _try_one_crawl_attempt(params, start, attempt)
        if result is not None:
            return result
        last_err = err
    return _empty_failure_result(params.url, last_err, int(timer_ms() - start))


_DO_CRAWL_FIELD_NAMES: tuple[str, ...] = (
    "url", "wait_for", "timeout", "javascript",
    "extract_links_flag", "extract_text_flag", "extract_markdown_flag",
    "extract_images_flag", "user_agent", "headers",
    "screenshot", "scroll", "agent",
    "include_structured_data", "stealth", "include_readable",
    "same_domain_only", "max_links", "max_images",
    "js_inject", "js_inject_timeout",
    "wait_for_text", "wait_for_gone", "wait_for_timeout_extra",
)


async def _do_crawl(*args: Any, **kwargs: Any) -> dict:
    """Legacy crawl shim -- accepts positional args for backwards compat."""
    bag: Dict[str, Any] = dict(zip(_DO_CRAWL_FIELD_NAMES, args))
    bag.update(kwargs)
    url = bag["url"]
    if url.lower().endswith(".pdf"):
        return await _fetch_pdf(url)
    params = CrawlAttemptParams(**bag)
    return await _crawl_with_retries(params, timer_ms())
