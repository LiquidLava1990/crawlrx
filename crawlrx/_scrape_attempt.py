"""Single Playwright scrape attempt + retry loop.

Owns:
- ``ScrapeAttemptParams`` dataclass (one bundle per attempt)
- Page-flow helpers (waits, click, named-selector resolution, css matches)
- Per-attempt orchestrator ``_do_scrape_attempt``
- Retry loop ``_scrape_with_retries`` and the legacy positional-arg shim ``_do_scrape``

The thin orchestrator in ``scrape.py`` re-exports the load-bearing names so
``crawlrx.batch._do_scrape`` and direct callers keep working.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional

from playwright.async_api import TimeoutError as PWTimeout

from .browser_pool import get_browser, release_browser
from .captcha_handoff import clear_or_flag
from .config import USER_AGENTS
from .scrape_policy import plan_scrape
from ._utils_safety import classify_error
from .utils import (
    check_content_size,
    create_stealth_context,
    detect_bot_block,
    extract_structured_data,
    extract_title,
    human_mouse_movements,
    infinite_scroll,
    random_scroll_pause,
    save_failure_screenshot,
    stealth_viewport,
    timer_ms,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS
from ._smart_fetch_runner import (
    _extract_from_html,
    _scrape_via_smart_fetch,
    _smart_fetch_eligible,
)
from ._scrape_page_actions import (
    _await_extra_waits,
    _maybe_element_screenshot,
    _maybe_shadow_content,
    _run_js_inject,
)

logger = logging.getLogger("crawlrx.scrape")

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.5, 3.0]

_SCRAPE_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, ValueError, TimeoutError, ConnectionError,
    PWTimeout,
)


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


async def _wait_default_scrape(page: object, timeout: int) -> None:
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
        logger.warning("wait_for selector '%s' timed out", selector)
        return None  # explicit recovery: best-effort wait; PWTimeout is non-fatal


async def _wait_for_page(page: object, wait_for: Optional[str], timeout: int) -> None:
    """Smart wait: None|networkidle|load|domcontentloaded|<selector>."""
    if wait_for is None:
        await _wait_default_scrape(page, timeout)
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
class ScrapeAttemptParams:
    """Bundle for one scrape attempt (eliminates 15-param signature)."""

    url: str
    selector: Optional[str]
    selectors: Optional[Dict[str, str]]
    extract: str
    wait_for: Optional[str]
    timeout: int
    javascript: bool = True
    user_agent: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    scroll_to_bottom: bool = False
    click_selector: Optional[str] = None
    agent: Optional[str] = None
    include_structured_data: bool = False
    stealth: bool = True
    js_inject: Optional[str] = None
    js_inject_timeout: int = 5000
    wait_for_text: Optional[str] = None
    wait_for_gone: Optional[str] = None
    wait_for_timeout_extra: int = 0
    screenshot_selector: Optional[str] = None
    shadow_selector: Optional[str] = None


def _empty_scrape_result(url: str) -> dict:
    """Skeleton dict used by all scrape paths."""
    return {
        "success": False, "url": url, "title": None,
        "results": [], "data": {}, "elapsed_ms": 0,
        "error": None, "structured_data": None,
        "error_type": None,
        "status_code": None, "final_url": None,
        "source": "playwright", "rendered": True,
        "strategy": "browser_first",
        "provenance": {"http_attempted": False, "browser_attempted": True},
        "js_result": None, "screenshot_b64": None,
        "shadow_content": None,
    }


async def _make_scrape_context(browser: object, params: ScrapeAttemptParams, ua: str, vp: dict) -> object:
    """Create a fresh Playwright BrowserContext for one scrape attempt."""
    if params.stealth:
        return await create_stealth_context(
            browser, user_agent=ua, viewport=vp,
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
# Click + named selector + CSS match resolution
# ---------------------------------------------------------------------------


async def _maybe_click(page: object, click_selector: Optional[str]) -> None:
    """Optional pre-extract click + networkidle wait (best-effort)."""
    if not click_selector:
        return
    try:
        await page.click(click_selector, timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=5000)
    except (*PLAYWRIGHT_EXCEPTIONS, RuntimeError) as exc:
        logger.warning("click_selector '%s' failed: %s", click_selector, exc)
        return None  # explicit recovery: non-fatal: (*PLAYWRIGHT_EXCEPTIONS, RuntimeError) is recorded but does not abort the caller


async def _query_named(page: object, key: str, sel: str) -> Optional[object]:
    """Resolve a named selector to one element, returning None on error."""
    try:
        return await page.query_selector(sel)
    except (*PLAYWRIGHT_EXCEPTIONS, AttributeError) as exc:
        logger.warning("Named selector '%s' error: %s", key, exc)
        # explicit recovery: selector lookup non-fatal; caller records None
        return None


async def _read_named_text(key: str, el: object) -> Optional[str]:
    """Read inner_text (falling back to inner_html); None on extraction error."""
    try:
        text = await el.inner_text()
        text = text.strip() if text else ""
        return text if text else await el.inner_html()
    except (*PLAYWRIGHT_EXCEPTIONS, AttributeError) as exc:
        logger.warning("Named selector '%s' inner_text error: %s", key, exc)
        # explicit recovery: text-extract non-fatal; caller records None
        return None


async def _resolve_named_selector(
    page: object, key: str, sel: str, url: str,
) -> Optional[str]:
    """Look up one named selector and return its text, or None."""
    el = await _query_named(page, key, sel)
    if el is None:
        logger.warning("Named selector '%s' ('%s') matched nothing on %s", key, sel, url)
        return None
    return await _read_named_text(key, el)


async def _extract_named_selectors(page: object, selectors: Dict[str, str], url: str) -> Dict[str, Optional[str]]:
    """Run page.query_selector for each named selector, log failures."""
    out: Dict[str, Optional[str]] = {}
    for key, sel in selectors.items():
        out[key] = await _resolve_named_selector(page, key, sel, url)
    return out


async def _extract_css_matches(page: object, selector: str, extract: str, url: str) -> list:
    """Run page.query_selector_all + per-element _extract_from_html."""
    try:
        elements = await asyncio.wait_for(page.query_selector_all(selector), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("query_selector_all('%s') timed out on %s", selector, url)
        return []  # explicit recovery: non-fatal: asyncio.TimeoutError is recorded but does not abort the caller
    if not elements:
        logger.warning("Selector '%s' matched 0 elements on %s", selector, url)
    elements = elements[:10000]
    out = []
    for el in elements:
        try:
            out.append(await _extract_from_html(await el.inner_html(), extract, url, selector))
        except (*PLAYWRIGHT_EXCEPTIONS, AttributeError, RuntimeError) as exc:
            logger.warning("Selector '%s' element extraction failed on %s: %s", selector, url, exc)
    return out


# ---------------------------------------------------------------------------
# Cleanup + crash handling
# ---------------------------------------------------------------------------


async def _save_crash_screenshot(page: Optional[object], url: str) -> None:
    """Best-effort failure screenshot when an exception fires."""
    if page is None:
        return
    try:
        await asyncio.wait_for(save_failure_screenshot(page, url), timeout=5)
    except (*PLAYWRIGHT_EXCEPTIONS, asyncio.TimeoutError, OSError) as exc:
        logger.debug("save_failure_screenshot suppressed: %s", exc)
        return None  # explicit recovery: non-fatal: (*PLAYWRIGHT_EXCEPTIONS, asyncio.TimeoutError, OSError) is recorded but does not abort the caller


async def _safe_close_one(obj: object, name: str, url: str) -> None:
    """Close one browser object, swallowing recoverable shutdown errors."""
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


# ---------------------------------------------------------------------------
# Per-attempt main flow
# ---------------------------------------------------------------------------


async def _do_scrape_dispatch(page: object, params: ScrapeAttemptParams, full_html: str, result: dict) -> None:
    """Pick selectors / selector / whole-page mode and populate ``result``."""
    if params.selectors:
        data = await _extract_named_selectors(page, params.selectors, params.url)
        result["data"] = data
        result["success"] = any(value for value in data.values())
        if not result["success"]:
            result["error"] = "Named selectors matched no content"
            result["error_type"] = "selector_empty"
        return
    if params.selector:
        matches = await _extract_css_matches(page, params.selector, params.extract, params.url)
        result["results"] = matches
        result["success"] = bool(matches)
        if not matches:
            result["error"] = "Selector matched no content: %s" % params.selector
            result["error_type"] = "selector_empty"
        return
    result["results"] = [await _extract_from_html(full_html, params.extract, params.url, None)]
    result["success"] = True


async def _do_scrape_attempt_body(page: object, params: ScrapeAttemptParams, result: dict, start: float) -> Optional[dict]:
    """Inner scrape body (returns a result OR None to continue)."""
    response = await page.goto(
        params.url, timeout=min(params.timeout, 30000),
        wait_until="domcontentloaded",
    )
    result["status_code"] = response.status if response else None
    result["final_url"] = page.url
    await _wait_for_page(page, params.wait_for, params.timeout)
    await _await_extra_waits(page, params)
    await _maybe_click(page, params.click_selector)
    await _run_js_inject(page, params, result)
    await human_mouse_movements(page, stealth_viewport())
    await random_scroll_pause(page)
    await page.wait_for_timeout(random.randint(500, 1500))
    if params.scroll_to_bottom:
        await infinite_scroll(page, passes=3, wait_ms=1200)
    if not await check_content_size(page):
        result["error"] = "Page too large (>10MB)"
        result["error_type"] = "content_quality"
        result["elapsed_ms"] = int(timer_ms() - start)
        return result
    full_html = await page.content()
    title = await page.title() or extract_title(full_html)
    result["title"] = title
    if detect_bot_block(full_html, title or ""):
        refreshed = await clear_or_flag(page, params.url, full_html, title or "", result)
        if refreshed is None:
            result["elapsed_ms"] = int(timer_ms() - start)
            return result
        full_html, title = refreshed[0], refreshed[1] or extract_title(refreshed[0])
        result["title"], result["final_url"] = title, page.url
    if params.include_structured_data:
        result["structured_data"] = extract_structured_data(full_html)
    await _maybe_element_screenshot(page, params, result)
    await _maybe_shadow_content(page, params, result)
    await _do_scrape_dispatch(page, params, full_html, result)
    return None


async def _do_scrape_attempt(params: ScrapeAttemptParams, start: float) -> dict:
    """One scrape attempt with fresh context+page, closed in finally."""
    result = _empty_scrape_result(params.url)
    plan = plan_scrape(params)
    result["strategy"] = plan.strategy if plan.strategy == "browser_first" else "http_then_browser"
    result["provenance"] = {
        **(result.get("provenance") or {}),
        "plan_reason": plan.reason,
        "required_features": list(plan.required_features),
    }
    ua = params.user_agent or USER_AGENTS[0]
    vp = stealth_viewport()
    slot = None
    context = None
    page = None
    try:
        browser, slot = await get_browser()
        context = await _make_scrape_context(browser, params, ua, vp)
        page = await context.new_page()
        page.set_default_timeout(min(params.timeout, 30000))
        early = await _do_scrape_attempt_body(page, params, result, start)
        if early is not None:
            return early
    except PWTimeout as exc:
        result["error"] = "Timeout: %s" % exc
        logger.error("Scrape timeout for %s: %s", params.url, exc)
        await _save_crash_screenshot(page, params.url)
        raise exc
    except _SCRAPE_RETRY_EXCEPTIONS as exc:
        result["error"] = str(exc)
        logger.error("Scrape error for %s: %s", params.url, exc, exc_info=True)
        await _save_crash_screenshot(page, params.url)
        raise exc
    finally:
        await close_attempt(page, context, slot, params.url)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def _empty_scrape_failure(url: str, last_err: Optional[BaseException], elapsed_ms: int) -> dict:
    """Result dict for a scrape that exhausted all retries."""
    out = _empty_scrape_result(url)
    out["error"] = str(last_err) if last_err else "unknown error"
    out["error_type"] = classify_error(out["error"]) if last_err else "network_error"
    out["elapsed_ms"] = elapsed_ms
    return out


async def _handle_scrape_retry_failure(exc: BaseException, attempt: int, url: str) -> None:
    """Log + sleep before the next scrape attempt."""
    if attempt >= _MAX_RETRIES:
        logger.error("All %d scrape attempts failed for %s: %s", _MAX_RETRIES, url, exc)
        return
    wait_s = _RETRY_BACKOFF[attempt - 1]
    logger.warning(
        "Scrape attempt %d/%d failed for %s: %s -- retrying in %.1fs",
        attempt, _MAX_RETRIES, url, exc, wait_s,
    )
    await asyncio.sleep(wait_s)


async def _try_one_scrape_attempt(
    params: ScrapeAttemptParams, start: float, attempt: int,
) -> tuple[Optional[dict], Optional[BaseException]]:
    """Run one scrape attempt. Returns (terminal_result_or_None, error_or_None)."""
    try:
        result = await _do_scrape_attempt(params, start)
    except _SCRAPE_RETRY_EXCEPTIONS as exc:
        await _handle_scrape_retry_failure(exc, attempt, params.url)
        return None, exc
    result["elapsed_ms"] = int(timer_ms() - start)
    return result, None


async def _scrape_with_retries(params: ScrapeAttemptParams) -> dict:
    """Run smart_fetch (if eligible) then up to _MAX_RETRIES Playwright attempts."""
    start = timer_ms()
    http_attempted = False
    plan = plan_scrape(params)
    if _smart_fetch_eligible(params):
        sf_result = await _scrape_via_smart_fetch(params, start)
        if sf_result is not None:
            return sf_result
        http_attempted = True
    last_err: Optional[BaseException] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        result, err = await _try_one_scrape_attempt(params, start, attempt)
        if result is not None:
            if http_attempted:
                result["strategy"] = "http_then_browser"
                result["provenance"] = {
                    **(result.get("provenance") or {}),
                    "plan_reason": plan.reason,
                    "required_features": list(plan.required_features),
                    "http_attempted": True,
                    "browser_attempted": True,
                }
            return result
        last_err = err
    failure = _empty_scrape_failure(params.url, last_err, int(timer_ms() - start))
    if http_attempted:
        failure["strategy"] = "http_then_browser"
        failure["provenance"] = {
            **(failure.get("provenance") or {}),
            "plan_reason": plan.reason,
            "required_features": list(plan.required_features),
            "http_attempted": True,
            "browser_attempted": True,
        }
    return failure


_DO_SCRAPE_FIELD_NAMES: tuple[str, ...] = (
    "url", "selector", "selectors", "extract", "wait_for", "timeout",
    "javascript", "user_agent", "headers", "scroll_to_bottom",
    "click_selector", "agent", "include_structured_data", "stealth",
    "js_inject", "js_inject_timeout",
    "wait_for_text", "wait_for_gone", "wait_for_timeout_extra",
    "screenshot_selector", "shadow_selector",
)


async def _do_scrape(*args: Any, **kwargs: Any) -> dict:
    """Legacy scrape shim -- accepts positional args for backwards compat."""
    bag: Dict[str, Any] = dict(zip(_DO_SCRAPE_FIELD_NAMES, args))
    bag.update(kwargs)
    params = ScrapeAttemptParams(**bag)
    return await _scrape_with_retries(params)
