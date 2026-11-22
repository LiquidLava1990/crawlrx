"""Internal helpers for :mod:`extract` (module split).

Splits the long ``_do_extract_attempt`` and ``_call_llm_for_extraction``
bodies in :mod:`extract` into single-responsibility sibling helpers so the
public surface stays small. All exceptions are narrow per S2 (granular
errors) and tuples come from :mod:`_scraper_helpers` per S5.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from . import model_backend

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)
from .utils import (
    extract_readable_content,
    html_to_markdown,
    human_mouse_movements,
    infinite_scroll,
    random_scroll_pause,
    save_failure_screenshot,
    stealth_viewport,
)

logger = logging.getLogger("crawlrx.extract")


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def build_system_prompt(has_schema: bool) -> str:
    """Pick the system prompt for LLM extraction."""
    if has_schema:
        return (
            "You are a data extraction assistant. Extract structured data from "
            "the provided webpage content according to the user's instructions. "
            "Return ONLY valid JSON that matches the provided schema. Do not "
            "include any explanatory text."
        )
    return (
        "You are a data extraction assistant. Extract data from the provided "
        "webpage content according to the user's instructions. Return your "
        "response as valid JSON when possible (dict, list, or string). Do "
        "not include any explanatory text outside the JSON."
    )


def build_user_content(
    prompt: str,
    schema: Optional[Dict[str, Any]],
    page_text: str,
) -> str:
    """Compose the LLM user message including optional schema block."""
    schema_text = ""
    if schema:
        schema_text = (
            "\n\nSchema (return JSON matching this structure):\n"
            + json.dumps(schema, indent=2)
        )
    return (
        "Extraction Instructions: %s\n%s\n\nWebpage Content:\n---\n%s\n---"
        % (prompt, schema_text, page_text[:50000])
    )


def strip_code_fences(content: str) -> str:
    """Strip ```json``` / ``` fences from LLM response."""
    if "```json" in content:
        start_idx = content.index("```json") + len("```json")
        end_idx = content.rindex("```")
        return content[start_idx:end_idx].strip()
    if "```" in content:
        start_idx = content.index("```") + len("```")
        end_idx = content.rindex("```")
        return content[start_idx:end_idx].strip()
    return content


def parse_llm_response(resp_data: dict) -> dict:
    """Pick the first message content from the LLM response and decode it."""
    choices = resp_data.get("choices")
    if not choices or not isinstance(choices, list):
        return {"success": False, "error": "LLM returned no choices"}
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return {"success": False, "error": "LLM returned empty response"}
    content = strip_code_fences(content)
    try:
        return {"success": True, "data": json.loads(content)}
    except json.JSONDecodeError:
        return {"success": True, "data": content.strip()}


async def post_llm_request(messages: list) -> dict:
    """Send the extraction prompt to the configured model; parse the reply."""
    try:
        resp_data = await model_backend.chat_completion(messages, temperature=0.1, max_tokens=4000)
    except model_backend.ModelBackendUnavailable as e:
        return {"success": False, "error": str(e)}
    except httpx.HTTPStatusError as e:
        logger.error("Model server HTTP error: %s", e)
        return {"success": False, "error": "Model server error: %s" % e}
    except PARSE_EXCEPTIONS as parse_err:
        return {"success": False, "error": "Model response JSON parse failed: %s" % parse_err}
    return parse_llm_response(resp_data)


# ---------------------------------------------------------------------------
# Browser-attempt helpers
# ---------------------------------------------------------------------------

@dataclass
class BrowserAttemptCtx:
    """Mutable context for a single browser extraction attempt."""

    url: str
    prompt: str
    schema: Optional[Dict[str, Any]]
    timeout_ms: int
    result: Dict[str, Any]
    page: Any = None
    context: Any = None
    browser: Any = None


async def _safe_save_screenshot(page: Any, url: str) -> None:
    """Save a failure screenshot, suppressing browser/IO failures."""
    if page is None:
        return
    try:
        await asyncio.wait_for(save_failure_screenshot(page, url), timeout=5)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("save_failure_screenshot suppressed %s: %s", type(exc).__name__, exc)
    except asyncio.TimeoutError:
        logger.debug("save_failure_screenshot timed out for %s", url)


async def _wait_for_load(page: Any, timeout_ms: int) -> None:
    """Smart load-state wait with networkidle then domcontentloaded fallback."""
    from playwright.async_api import TimeoutError as PWTimeout

    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        return
    except PWTimeout:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 8000))
    except PWTimeout:
        pass
    await page.wait_for_timeout(1500)


async def _open_browser_page(p: Any, viewport: dict, url: str, timeout_ms: int):
    """Launch a chromium browser, build a fresh context, navigate, return ctx."""
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=viewport,
        locale="en-US",
        extra_http_headers=_DEFAULT_HEADERS,
    )
    page = await context.new_page()
    logger.info("Navigating to URL: %s", url)
    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    return browser, context, page


async def _humanize_and_scroll(page: Any, viewport: dict) -> None:
    """Run mouse-movement / random pause / pre-extract delay / infinite scroll."""
    await human_mouse_movements(page, viewport)
    await random_scroll_pause(page)
    await page.wait_for_timeout(random.randint(500, 1500))
    await infinite_scroll(page, passes=3, wait_ms=1200)


async def _resolve_page_text(page: Any, url: str) -> str:
    """Pull readable / markdown text from the live page."""
    html = await page.content()
    readable = extract_readable_content(html, url)
    page_text = readable.get("text", "")
    if not page_text or len(page_text.strip()) < 50:
        logger.debug(
            "Readability extraction thin (%d chars), falling back to markdown",
            len(page_text),
        )
        page_text = html_to_markdown(html, base_url=url)
    return page_text


async def _safe_close_one(obj: Any, name: str) -> None:
    """Close a single browser object, swallowing recoverable errors."""
    if obj is None:
        return
    try:
        await obj.close()
    except PLAYWRIGHT_EXCEPTIONS as close_err:
        logger.warning("Error closing %s: %s", name, close_err)
        return None  # explicit recovery: cleanup is best-effort; PLAYWRIGHT_EXCEPTIONS is non-fatal
    except OSError as close_err:
        logger.warning("OS error closing %s: %s", name, close_err)
        return None  # explicit recovery: cleanup is best-effort; OSError is non-fatal


async def close_browser_objects(ctx: BrowserAttemptCtx) -> None:
    """Best-effort close of page/context/browser, narrow exception filter."""
    await _safe_close_one(ctx.page, "page")
    await _safe_close_one(ctx.context, "context")
    await _safe_close_one(ctx.browser, "browser")


LLMCaller = Callable[
    [str, str, Optional[Dict[str, Any]]],
    Awaitable[Dict[str, Any]],
]


async def run_browser_attempt(
    ctx: BrowserAttemptCtx,
    llm_caller: LLMCaller,
) -> None:
    """Drive a single browser fetch + LLM call into ``ctx.result``.

    ``llm_caller`` is an async ``(page_text, prompt, schema) -> dict`` thunk
    so the helper doesn't reach back into the parent module.
    """
    from playwright.async_api import async_playwright

    viewport = stealth_viewport()
    async with async_playwright() as p:
        ctx.browser, ctx.context, ctx.page = await _open_browser_page(
            p, viewport, ctx.url, ctx.timeout_ms,
        )
        await _wait_for_load(ctx.page, ctx.timeout_ms)
        await _humanize_and_scroll(ctx.page, viewport)
        page_text = await _resolve_page_text(ctx.page, ctx.url)

        if not page_text or len(page_text.strip()) < 10:
            ctx.result["error"] = "No meaningful content found on page"
            logger.warning("Empty page content for URL: %s", ctx.url)
            await _safe_save_screenshot(ctx.page, ctx.url)
            return

        logger.info(
            "Extracted %d chars of page text, calling LLM...", len(page_text),
        )
        llm_result = await llm_caller(page_text, ctx.prompt, ctx.schema)
        if llm_result.get("success"):
            ctx.result["success"] = True
            ctx.result["data"] = llm_result.get("data")
            logger.info("LLM extraction succeeded for URL: %s", ctx.url)
            return

        ctx.result["error"] = llm_result.get("error", "Unknown LLM error")
        logger.error(
            "LLM extraction failed for URL %s: %s", ctx.url, ctx.result["error"],
        )


__all__ = [
    "BrowserAttemptCtx",
    "build_system_prompt",
    "build_user_content",
    "close_browser_objects",
    "post_llm_request",
    "run_browser_attempt",
    "_safe_save_screenshot",
    "SCRAPE_FAILURE_EXCEPTIONS",
]
