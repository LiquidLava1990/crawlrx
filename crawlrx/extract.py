"""
extract.py -- LLM-powered web data extraction endpoint
Endpoint: POST /extract

Hardening:
- networkidle wait (JS-heavy site support)
- Infinite scroll: 3-pass with content-change detection
- Random mouse movements + pre-scroll (human simulation)
- Stealth viewport: 1366x768 or 1920x1080
- 3 retries with fresh browser context each time
- Screenshot on failure saved to CRAWLRX_SCREENSHOT_DIR
- Model: any OpenAI-compatible endpoint (see model_backend.py)
- Smart content: markdownify primary, Readability fallback, strips nav/footer/ads
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._extract_helpers import (
    BrowserAttemptCtx,
    build_system_prompt,
    build_user_content,
    close_browser_objects,
    post_llm_request,
    run_browser_attempt,
    _safe_save_screenshot,
)
from . import model_backend
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS
from .utils import is_safe_url, timer_ms

logger = logging.getLogger("crawlrx.extract")
router = APIRouter(prefix="/extract", tags=["extract"])

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.5, 3.0]

# Exception families that count as a retryable browser failure.
_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + (OSError, RuntimeError, ValueError, asyncio.TimeoutError)
)


# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

class ExtractRequest(BaseModel):
    model_config = {"populate_by_name": True}
    url: str = Field(..., description="URL to extract data from")
    prompt: str = Field(..., description="Instructions for what to extract from the page")
    output_schema: Optional[Dict[str, Any]] = Field(
        default=None, alias="schema",
        description="Optional JSON schema for structured output",
    )


class ExtractResponse(BaseModel):
    success: bool = Field(..., description="Whether extraction succeeded")
    url: str = Field(..., description="URL that was processed")
    prompt: str = Field(..., description="Extraction prompt used")
    data: Optional[Any] = Field(default=None, description="Extracted data (dict, list, or string)")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# LLM CALL
# ─────────────────────────────────────────────

async def _call_llm_for_extraction(
    page_text: str,
    prompt: str,
    schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Send page text to the configured model and parse the extraction."""
    messages = [
        {"role": "system", "content": build_system_prompt(bool(schema))},
        {"role": "user", "content": build_user_content(prompt, schema, page_text)},
    ]
    return await post_llm_request(messages)


# ─────────────────────────────────────────────
# BROWSER EXTRACTION (single attempt)
# ─────────────────────────────────────────────

async def _do_extract_attempt(
    url: str,
    prompt: str,
    schema: Optional[Dict[str, Any]],
    timeout: int,
) -> Dict[str, Any]:
    """One extraction attempt with fresh browser + context, closed in finally."""
    from playwright.async_api import TimeoutError as PWTimeout

    ctx = BrowserAttemptCtx(
        url=url,
        prompt=prompt,
        schema=schema,
        timeout_ms=min(timeout, 120000),
        result={
            "success": False, "url": url, "prompt": prompt,
            "data": None, "error": None,
        },
    )

    try:
        await run_browser_attempt(ctx, _call_llm_for_extraction)
    except PWTimeout as e:
        ctx.result["error"] = "Timeout loading page: %s" % e
        logger.error("Page load timeout for '%s': %s", url, e)
        await _safe_save_screenshot(ctx.page, url)
        raise e
    except PLAYWRIGHT_EXCEPTIONS as e:
        ctx.result["error"] = "Extraction failed: %s" % e
        logger.error("Browser error extracting from '%s': %s", url, e, exc_info=True)
        await _safe_save_screenshot(ctx.page, url)
        raise e
    except (OSError, RuntimeError, ValueError) as e:
        ctx.result["error"] = "Extraction failed: %s" % e
        logger.error("Runtime error extracting from '%s': %s", url, e, exc_info=True)
        await _safe_save_screenshot(ctx.page, url)
        raise e
    finally:
        await close_browser_objects(ctx)

    return ctx.result


def _build_failure_result(
    url: str,
    prompt: str,
    last_err: Optional[BaseException],
    start_ms: float,
) -> Dict[str, Any]:
    """Compose the terminal failure dict after all retries exhausted."""
    return {
        "success": False,
        "url": url,
        "prompt": prompt,
        "data": None,
        "error": str(last_err),
        "elapsed_ms": int(timer_ms() - start_ms),
    }


async def _handle_retry_failure(
    attempt: int,
    err: BaseException,
    url: str,
) -> None:
    """Log + sleep before next retry."""
    if attempt >= _MAX_RETRIES:
        logger.error(
            "All %d extract attempts failed for %s: %s",
            _MAX_RETRIES, url, err,
        )
        return
    wait_s = _RETRY_BACKOFF[attempt - 1]
    logger.warning(
        "Extract attempt %d/%d failed for %s: %s -- retrying in %.1fs",
        attempt, _MAX_RETRIES, url, err, wait_s,
    )
    await asyncio.sleep(wait_s)


async def _do_extract(
    url: str,
    prompt: str,
    schema: Optional[Dict[str, Any]],
    timeout: int = 30000,
) -> Dict[str, Any]:
    """Extract with up to ``_MAX_RETRIES`` attempts; each uses a fresh browser."""
    start = timer_ms()
    last_err: Optional[BaseException] = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            result = await _do_extract_attempt(url, prompt, schema, timeout)
        except _RETRY_EXCEPTIONS as e:
            last_err = e
            await _handle_retry_failure(attempt, e, url)
            continue

        result["elapsed_ms"] = int(timer_ms() - start)
        return result

    return _build_failure_result(url, prompt, last_err, start)


# ─────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────

@router.post(
    "/",
    response_model=ExtractResponse,
    summary="Extract structured data from a webpage using LLM",
)
async def extract_data(request: ExtractRequest) -> ExtractResponse:
    """Extract structured data from any webpage using an LLM.

    **Hardening:** networkidle wait, infinite scroll, human simulation, stealth viewport,
    3-retry with fresh browser, screenshot on failure, Readability content extraction.
    """
    if not is_safe_url(request.url):
        return ExtractResponse(
            url=request.url, success=False, error="URL blocked by SSRF protection",
            prompt=request.prompt, data={},
        )
    try:
        model_settings = await asyncio.to_thread(model_backend.require_settings)
    except model_backend.ModelBackendUnavailable as exc:
        return ExtractResponse(
            url=request.url, success=False, error=str(exc), prompt=request.prompt,
        )
    timeout = 30000
    wall_timeout = int(timeout // 1000 + model_settings.timeout_s + 30)

    try:
        raw = await asyncio.wait_for(
            _do_extract(request.url, request.prompt, request.output_schema, timeout),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for extract request")
        return ExtractResponse(
            success=False,
            url=request.url,
            prompt=request.prompt,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
        )

    return ExtractResponse(**raw)
