"""Solve-attempt + token-extraction strategies for the Botright solver.

Owns the core logic of one solve attempt: navigate, detect, poll, extract.
The orchestrator passes itself in via the ``host`` argument so this module
can call back into pool-management and timeout fields without re-creating
those primitives.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

from ._stealth_helpers import ( _DETECT_CAPTCHA_JS, _GENERIC_TOKEN_JS, _METHOD_CAPTCHA_TYPE, _SolveOutcome, _TOKEN_EXTRACTORS, )
from ._sentinel_shim import ErrCode, emit
from ._solver_helpers import BROAD_EXC as _BROAD_EXC

from ..models import Task, TaskResult
from ..solver_base import SolverError, SolverTimeoutError  # noqa: E402


logger = logging.getLogger(__name__)


class _SolveHost(Protocol):
    """Subset of ``StealthSolver`` the strategy code reaches into."""

    timeout: float
    max_retries: int

    async def _acquire_browser(self) -> "tuple[Any, int]": ...
    async def _release_browser(self, idx: int) -> None: ...


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
async def _extract_typed_token(page: Any, captcha_type: str) -> Optional[str]:
    """Try the type-specific extraction script for ``captcha_type``."""
    script = _TOKEN_EXTRACTORS.get(captcha_type)
    if script is None:
        return None
    token = await page.evaluate(script)
    if token and len(token) > 20:
        return token
    return None


async def _extract_generic_token(page: Any) -> Optional[str]:
    """Fall back to a selector sweep for any CAPTCHA response field."""
    token = await page.evaluate(_GENERIC_TOKEN_JS)
    if token and len(token) > 20:
        return token
    return None


async def extract_token(page: "Page", captcha_type: str) -> Optional[str]:
    """Extract CAPTCHA token from page based on type."""
    try:
        specific = await _extract_typed_token(page, captcha_type)
        if specific:
            return specific
        return await _extract_generic_token(page)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.debug("Token extraction error: %s", e)
        return None


async def detect_captcha_type(page: "Page") -> Optional[str]:
    """Detect the type of CAPTCHA present on the page."""
    try:
        captcha_info = await page.evaluate(_DETECT_CAPTCHA_JS)
        if captcha_info.get("type"):
            logger.debug("Detected CAPTCHA type: %s", captcha_info["type"])
            return captcha_info["type"]
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.debug("CAPTCHA detection error: %s", e)
    return None


def captcha_type_for(task: Task) -> Optional[str]:
    """Map a Task method to the canonical captcha-type string."""
    method = task.method.value
    if method in ("userrecaptcha", "recaptcha_v2"):
        return "recaptcha_v2"
    return _METHOD_CAPTCHA_TYPE.get(method)


# ---------------------------------------------------------------------------
# Solve-attempt control flow
# ---------------------------------------------------------------------------
async def _navigate_with_timeout(
    page: Any, pageurl: str, timeout: float,
) -> None:
    """Navigate ``page`` to ``pageurl`` and translate timeout into SolverTimeoutError."""
    logger.debug("Navigating to %s", pageurl)
    try:
        await asyncio.wait_for(
            page.goto(pageurl, wait_until="networkidle"),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        emit(ErrCode.TIMEOUT_ERROR, "unhandled")
        raise SolverTimeoutError(timeout)


def _navigated_away(page: Any, pageurl: str) -> bool:
    """True iff ``page`` has navigated off the captcha/challenge URL."""
    current_url = page.url
    if current_url == pageurl:
        return False
    return not any(marker in current_url for marker in ("captcha", "challenge"))


async def _wait_for_token(
    page: Any,
    pageurl: str,
    captcha_type: Optional[str],
    start_time: float,
    timeout: float,
) -> Optional[str]:
    """Poll page for token until ``timeout`` is exhausted."""
    elapsed = time.time() - start_time
    remaining = max(0.0, timeout - elapsed)
    check_interval = 1.0
    total_waited = 0.0
    ctype = captcha_type or "unknown"
    while total_waited < remaining:
        token = await extract_token(page, ctype)
        if token:
            return token
        if _navigated_away(page, pageurl):
            token = await extract_token(page, ctype)
            if token:
                return token
        await asyncio.sleep(check_interval)
        total_waited += check_interval
    return None


async def _run_active_solver(
    page: Any, captcha_type: Optional[str], start_time: float, timeout: float,
) -> Optional[str]:
    """Invoke Botright's interactive solver when the page exposes one."""
    method_name = {
        "hcaptcha": "solve_hcaptcha",
        "recaptcha_v2": "solve_recaptcha",
        "userrecaptcha": "solve_recaptcha",
    }.get(captcha_type)
    method = getattr(page, method_name, None) if method_name else None
    remaining = timeout - (time.time() - start_time)
    if not callable(method) or remaining <= 0:
        return None
    try:
        result = await asyncio.wait_for(method(), timeout=remaining)
    except asyncio.CancelledError:
        raise
    except _BROAD_EXC as exc:
        logger.warning("Botright %s did not complete: %s", method_name, type(exc).__name__)
        return None
    # Some versions return a human-readable failure string instead of a token.
    if (isinstance(result, str) and len(result) > 20
            and not any(char.isspace() for char in result)
            and not result.lower().startswith(("error", "failed", "exceeded"))):
        return result
    return None


async def _release_solve_resources(
    host: _SolveHost,
    page: Optional[Any],
    browser_idx: Optional[int],
) -> None:
    """Close any open page and release the browser back to the pool."""
    try:
        if page is not None:
            await page.close()
    except asyncio.CancelledError:
        raise
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.debug("Error closing page: %s", e)
    finally:
        if browser_idx is not None:
            await host._release_browser(browser_idx)


async def _try_solve_once(
    host: _SolveHost,
    pageurl: str,
    captcha_type: Optional[str],
    start_time: float,
    attempt: int,
) -> _SolveOutcome:
    """Inner body of one solve attempt; raises are mapped to outcomes upstream."""
    logger.info("Solve attempt %s/%s for %s", attempt, host.max_retries, pageurl)
    browser, browser_idx = await host._acquire_browser()
    page = None
    try:
        page = await browser.new_page()
        await _navigate_with_timeout(page, pageurl, host.timeout)
        await asyncio.sleep(2)
        ctype = captcha_type or await detect_captcha_type(page)
        token = await extract_token(page, ctype or "unknown")
        if not token:
            token = await _run_active_solver(page, ctype, start_time, host.timeout)
        if not token:
            token = await _wait_for_token(page, pageurl, ctype, start_time, host.timeout)
        if token:
            logger.info("Successfully solved CAPTCHA on attempt %s", attempt)
            return _SolveOutcome(token=token, captcha_type=ctype)
        raise SolverError(
            "ERROR_CAPTCHA_UNSOLVABLE",
            "Could not extract token within timeout",
        )
    finally:
        await _release_solve_resources(host, page, browser_idx)


async def run_solve_attempt(
    host: _SolveHost,
    pageurl: str,
    captcha_type: Optional[str],
    start_time: float,
    attempt: int,
) -> _SolveOutcome:
    """Run one solve attempt; return the outcome dataclass."""
    try:
        return await _try_solve_once(
            host, pageurl, captcha_type, start_time, attempt,
        )
    except asyncio.CancelledError:
        raise
    except SolverTimeoutError as e:
        emit(ErrCode.TIMEOUT_ERROR, str(e))
        logger.warning("Attempt %s timed out: %s", attempt, e)
        return _SolveOutcome(error=e, captcha_type=captcha_type)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.error("Attempt %s failed: %s", attempt, e)
        return _SolveOutcome(error=e, captcha_type=captcha_type)


async def sleep_between_attempts(host: _SolveHost, attempt: int) -> None:
    """Exponential backoff between solve attempts."""
    if attempt >= host.max_retries:
        return
    delay = 2 ** attempt
    logger.debug("Retrying in %ss...", delay)
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------
def build_success_result(token: str, start_time: float) -> TaskResult:
    """Wrap a successful solve token in a TaskResult."""
    solve_time_ms = int((time.time() - start_time) * 1000)
    return TaskResult(
        success=True,
        token=token,
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=solve_time_ms,
    )


def build_failure_result(
    last_error: Optional[Exception],
    start_time: float,
    max_retries: int,
) -> TaskResult:
    """Wrap an exhausted-retries failure in a TaskResult."""
    solve_time_ms = int((time.time() - start_time) * 1000)
    message = str(last_error) if last_error else "Max retries exceeded"
    logger.error(
        "Failed to solve after %s attempts: %s", max_retries, message,
    )
    return TaskResult(
        success=False,
        error_code="ERROR_CAPTCHA_UNSOLVABLE",
        error_message=message,
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=solve_time_ms,
    )


def build_param_error_result() -> TaskResult:
    """Standard ``pageurl missing`` failure result."""
    return TaskResult(
        success=False,
        error_code="ERROR_BAD_PARAMETERS",
        error_message="Missing required parameter: pageurl",
        solved_at=datetime.now(tz=timezone.utc),
    )


__all__ = [
    "captcha_type_for",
    "detect_captcha_type",
    "extract_token",
    "run_solve_attempt",
    "sleep_between_attempts",
    "build_success_result",
    "build_failure_result",
    "build_param_error_result",
]
