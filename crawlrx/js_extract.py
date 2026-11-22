"""
js_extract.py -- POST /scrape/extract-js
JavaScript evaluation endpoint for CrawlRX price/data extraction.

For sites where prices render in CSS classes and inner_text('body') misses them.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from playwright.async_api import TimeoutError as PWTimeout

from .models import JSExtractRequest, JSExtractResponse
from .browser_pool import get_browser, release_browser
from .utils import create_stealth_context, timer_ms
from .cache import get_cache
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.js_extract")
router = APIRouter(prefix="/scrape", tags=["scrape"])


@dataclass(frozen=True)
class _JSExtractParams:
    """Bundled inputs for ``_do_js_extract`` so the call shape stays under
    the param-count ceiling and arguments travel together."""

    url: str
    js: str
    wait_for: Optional[str]
    timeout: int
    scroll_to_bottom: bool
    stealth: bool


_SCROLL_JS = """async () => {
    await new Promise(resolve => {
        const step = () => {
            window.scrollBy(0, window.innerHeight);
            if ((window.scrollY + window.innerHeight) < document.body.scrollHeight) {
                setTimeout(step, 80);
            } else { resolve(); }
        };
        step();
    });
}"""


async def _new_context(browser: Any, stealth: bool) -> Any:
    """Build the Playwright context (stealth or vanilla)."""
    if stealth:
        return await create_stealth_context(browser)
    return await browser.new_context()


async def _wait_for_load(page: Any, state: str, timeout_ms: int) -> None:
    """Run ``page.wait_for_load_state`` swallowing only timeouts."""
    try:
        await page.wait_for_load_state(state, timeout=timeout_ms)
    except PWTimeout:
        pass


async def _wait_for_selector(page: Any, selector: str, timeout: int, url: str) -> None:
    """Wait for a selector, logging if it does not appear in time."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
    except PWTimeout:
        logger.warning("wait_for selector '%s' timed out on %s", selector, url)
        return None  # explicit recovery: best-effort wait; PWTimeout is non-fatal


async def _apply_wait_state(page: Any, wait_for: Optional[str], timeout: int, url: str) -> None:
    """Resolve the wait_for option to a concrete page-readiness probe."""
    if wait_for is None:
        await _wait_for_load(page, "domcontentloaded", min(timeout, 8000))
        await page.wait_for_timeout(1500)
        return
    if wait_for == "networkidle":
        await _wait_for_load(page, "networkidle", min(timeout, 15000))
        return
    if wait_for == "load":
        await _wait_for_load(page, "load", timeout)
        return
    if wait_for != "domcontentloaded":
        await _wait_for_selector(page, wait_for, timeout, url)


async def _scroll_page_to_bottom(page: Any) -> None:
    """Run the in-page scroll loop, swallowing browser-scrape failures."""
    try:
        await page.evaluate(_SCROLL_JS)
        await page.wait_for_timeout(300)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Scroll-to-bottom suppressed (%s): %s", type(exc).__name__, exc)


async def close_quietly(obj: "Page | BrowserContext | Browser | None", name: str, url: str) -> None:
    """Close ``obj``, downgrading scrape failures to a warning."""
    if obj is None:
        return
    try:
        await obj.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Error closing %s for %s: %s", name, url, exc)
        return None  # explicit recovery: cleanup is best-effort; BROWSER_SCRAPE_EXCEPTIONS is non-fatal


async def _run_js_on_page(page: Any, params: _JSExtractParams, result: dict) -> None:
    """Apply wait-state + scroll, then evaluate the user JS."""
    await page.goto(params.url, timeout=params.timeout, wait_until="domcontentloaded")
    await _apply_wait_state(page, params.wait_for, params.timeout, params.url)
    if params.scroll_to_bottom:
        await _scroll_page_to_bottom(page)
    result["result"] = await page.evaluate(params.js)
    result["success"] = True


async def _do_js_extract(params: _JSExtractParams) -> dict:
    """Synchronous Playwright JS extraction; returns a dict for JSExtractResponse."""
    start = timer_ms()
    result: dict = {
        "success": False,
        "url": params.url,
        "result": None,
        "elapsed_ms": 0,
        "error": None,
    }
    slot = None
    context = None
    page = None
    try:
        browser, slot = await get_browser()
        context = await _new_context(browser, params.stealth)
        page = await context.new_page()
        await _run_js_on_page(page, params, result)
    except PWTimeout as exc:
        result["error"] = f"Timeout: {exc}"
        logger.error("JS extract timeout for %s: %s", params.url, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        result["error"] = str(exc)
        logger.error("JS extract error for %s: %s", params.url, exc, exc_info=True)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_quietly(page, "page", params.url)
        await close_quietly(context, "context", params.url)
        if slot is not None:
            await release_browser(slot)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


def _params_from_request(request: JSExtractRequest) -> _JSExtractParams:
    """Extract the call-shape dataclass from the inbound request model."""
    return _JSExtractParams(
        url=request.url,
        js=request.js,
        wait_for=request.wait_for,
        timeout=request.timeout,
        scroll_to_bottom=request.scroll_to_bottom,
        stealth=request.stealth,
    )


async def _run_with_wall_timeout(
    params: _JSExtractParams, wall_timeout: int,
) -> dict:
    """Run ``_do_js_extract`` under an outer wall-clock guard."""
    try:
        return await asyncio.wait_for(_do_js_extract(params), timeout=wall_timeout)
    except asyncio.TimeoutError:
        logger.error(
            "Wall-clock timeout (%ds) exceeded for extract-js: %s",
            wall_timeout, params.url,
        )
        return {
            "success": False,
            "url": params.url,
            "result": None,
            "elapsed_ms": wall_timeout * 1000,
            "error": f"Global timeout exceeded ({wall_timeout}s wall-clock)",
        }


@router.post("/extract-js", response_model=JSExtractResponse, summary="Evaluate JavaScript on a page")
async def extract_js(request: JSExtractRequest) -> JSExtractResponse:
    """Navigate to a page and evaluate a JavaScript expression."""
    cache = get_cache()
    cache_key: Optional[str] = None
    if request.cache:
        cache_key = cache.make_key("/scrape/extract-js", request.model_dump())
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for extract-js: %s", request.url)
            return JSExtractResponse(**{**cached, "cache_hit": True})

    params = _params_from_request(request)
    wall_timeout = request.timeout // 1000 + 20
    raw = await _run_with_wall_timeout(params, wall_timeout)

    if request.cache and raw.get("success") and cache_key:
        cache.set(cache_key, raw, ttl_override=request.cache_ttl)
    raw["cache_hit"] = False
    return JSExtractResponse(**raw)
