"""Optional page actions for /scrape attempts."""
from __future__ import annotations

import asyncio
import base64
import logging

from playwright.async_api import TimeoutError as PWTimeout

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS

logger = logging.getLogger("crawlrx.scrape")


async def _await_extra_waits(page: object, params: object) -> None:
    """Honour wait_for_text / wait_for_gone / wait_for_timeout_extra."""
    if params.wait_for_text:
        try:
            await page.wait_for_function(
                "(text) => document.body && document.body.innerText.includes(text)",
                arg=params.wait_for_text,
                timeout=min(params.timeout, 15000),
            )
        except (PWTimeout, RuntimeError):
            logger.warning(
                "wait_for_text '%s' not found on %s",
                params.wait_for_text, params.url,
            )
            return None  # explicit recovery: non-fatal wait miss
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


async def _run_js_inject(page: object, params: object, result: dict) -> None:
    """Run caller-supplied pre-extraction JavaScript (best-effort)."""
    if not params.js_inject:
        return
    try:
        js_val = await asyncio.wait_for(
            page.evaluate(params.js_inject),
            timeout=min(params.js_inject_timeout / 1000, 10),
        )
        if isinstance(js_val, (dict, list, str, int, float, bool)) or js_val is None:
            result["js_result"] = js_val
        await page.wait_for_timeout(min(params.js_inject_timeout, 2000))
    except asyncio.TimeoutError:
        logger.warning("js_inject timed out on %s", params.url)
        result["js_result"] = {"error": "timeout"}
        return None  # explicit recovery: non-fatal JS timeout
    except (*PLAYWRIGHT_EXCEPTIONS, ValueError, TypeError) as exc:
        logger.warning("js_inject failed on %s: %s", params.url, exc)
        result["js_result"] = {"error": str(exc)}
        return None  # explicit recovery: non-fatal JS failure


async def _maybe_element_screenshot(page: object, params: object, result: dict) -> None:
    """Capture a base64 PNG for screenshot_selector."""
    if not params.screenshot_selector:
        return
    try:
        element = await page.query_selector(params.screenshot_selector)
        if element is None:
            logger.warning(
                "screenshot_selector '%s' matched nothing on %s",
                params.screenshot_selector, params.url,
            )
            return
        png = await element.screenshot(type="png")
        result["screenshot_b64"] = base64.b64encode(png).decode("ascii")
    except (*PLAYWRIGHT_EXCEPTIONS, OSError, RuntimeError) as exc:
        logger.warning("Element screenshot failed on %s: %s", params.url, exc)
        return None  # explicit recovery: non-fatal screenshot failure


async def _maybe_shadow_content(page: object, params: object, result: dict) -> None:
    """Extract text from an open shadow root when shadow_selector is provided."""
    if not params.shadow_selector:
        return
    try:
        result["shadow_content"] = await page.eval_on_selector(
            params.shadow_selector,
            "(host) => host && host.shadowRoot ? host.shadowRoot.textContent.trim() : null",
        )
    except (*PLAYWRIGHT_EXCEPTIONS, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("shadow_selector failed on %s: %s", params.url, exc)
        return None  # explicit recovery: non-fatal shadow-root miss
