import asyncio
"""screenshot.py -- POST /screenshot

Full-page screenshot capture using Playwright CDP.
Returns base64-encoded PNG of any URL.
"""

import base64
import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .models import ScreenshotRequest, ScreenshotResponse
from .stealth_context import make_stealth_context, close_stealth_context

logger = logging.getLogger("crawlrx.screenshot")
router = APIRouter()


@dataclass(frozen=True)
class _ShotSpec:
    url: str
    full_page: bool
    width: int
    height: int
    timeout_ms: int


def _ms_since(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


def _err_response(spec: _ShotSpec, error_message: str, elapsed_ms: int) -> ScreenshotResponse:
    return ScreenshotResponse(
        success=False, url=spec.url, error=error_message,
        width=spec.width, height=spec.height, elapsed_ms=elapsed_ms,
    )


async def _capture_png(page, spec: _ShotSpec) -> str:
    await page.set_viewport_size({"width": spec.width, "height": spec.height})
    logger.info("[Screenshot] Navigating to %s", spec.url)
    await page.goto(spec.url, wait_until="networkidle", timeout=spec.timeout_ms)
    screenshot_bytes = await page.screenshot(full_page=spec.full_page)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


async def _do_screenshot(url: str, full_page: bool, width: int, height: int, timeout_ms: int) -> ScreenshotResponse:
    """Single-shot screenshot capture using a stealth context."""
    spec = _ShotSpec(url=url, full_page=full_page, width=width, height=height, timeout_ms=timeout_ms)
    start_time = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            screenshot_base64 = await _capture_png(page, spec)
            elapsed_ms = _ms_since(start_time)
            logger.info(
                "[Screenshot] Captured %s (%dx%d, full_page=%s) in %dms",
                spec.url, spec.width, spec.height, spec.full_page, elapsed_ms,
            )
            return ScreenshotResponse(
                success=True, url=spec.url, screenshot_base64=screenshot_base64,
                width=spec.width, height=spec.height, elapsed_ms=elapsed_ms,
            )
    except PlaywrightTimeoutError as e:
        logger.error("[Screenshot] Timeout for %s: %s", spec.url, e)
        return _err_response(spec, "Timeout: %s" % e, _ms_since(start_time))
    except (OSError, RuntimeError, ValueError, AttributeError) as e:
        logger.error("[Screenshot] Error capturing %s: %s", spec.url, e)
        return _err_response(spec, str(e), _ms_since(start_time))
    finally:
        await close_stealth_context(browser, ctx, page)


@router.post("/screenshot", response_model=ScreenshotResponse, summary="Capture screenshot of URL")
async def screenshot(request: ScreenshotRequest) -> ScreenshotResponse:
    """
    Capture a screenshot of any URL.

    - **url**: URL to screenshot (required)
    - **full_page**: Capture full page or just viewport (default: true)
    - **width**: Viewport width in pixels (default: 1280)
    - **height**: Viewport height in pixels (default: 800)
    - **session_id**: Optional session ID (unused for now)

    Returns base64-encoded PNG image.
    Uses a CDP connection to CRAWLRX_CDP_URL (default http://localhost:9222).
    """
    timeout_seconds = 60
    
    try:
        return await asyncio.wait_for(
            _do_screenshot(request.url, request.full_page, request.width, request.height, 45000),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError, AttributeError) as e:
        logger.error("[Screenshot] Request failed: %s", e)
        return ScreenshotResponse(
            success=False,
            url=request.url,
            error="Request failed: %s" % e,
            width=request.width,
            height=request.height,
            elapsed_ms=timeout_seconds * 1000,
        )
