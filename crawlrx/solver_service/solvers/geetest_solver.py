"""GeeTest sliding-puzzle solver — thin orchestrator.

Public surface: ``GeeTestSolver``. All non-orchestration logic lives in:

* ``_geetest_helpers.py``     — selectors, JS payloads, bezier math
* ``_geetest_browser.py``     — CDP connect + cleanup
* ``_geetest_strategies.py``  — slider/icon/space challenge solvers
* ``_geetest_drag.py``        — human-like drag motion
* ``_geetest_opencv.py``      — fallback template-matching path

Supports GeeTest v3/v4 sliding puzzles, icon clicks, and space verification.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Path bootstrap (mirrors the original solver's bootstrap so this module can
# run both as ``solvers.geetest_solver`` and as a top-level script).
try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode = _E()  # type: ignore[assignment]  because sentinel ErrCode is unavailable; _E() shim returns ERR_UNKNOWN for any attr

    def emit(*a: object, **kw: object) -> None:  # noqa: ANN001
        return None


from playwright.async_api import (  # noqa: E402
    Browser, BrowserContext, Page, async_playwright,
)

from ..solver_base import BaseSolver  # noqa: E402

from ._geetest_browser import ( activate_v4_widget, cleanup as _browser_cleanup, connect_cdp_or_fail, ensure_context, setup_anti_detection, )
from ._geetest_opencv import solve_with_opencv
from ._geetest_strategies import ( AttemptCtx, attempt_solve, detect_captcha_type, dispatch_challenge, )
from ._solver_helpers import BROAD_EXC as _BROAD_EXC


# Cancellation must reach the router; it is not a recoverable browser error.
_BROAD_EXC = tuple(error for error in _BROAD_EXC if error is not asyncio.CancelledError)

from ..models import Task, TaskResult  # noqa: E402


logger = logging.getLogger(__name__)


class GeeTestSolver(BaseSolver):
    """Solver for GeeTest v3/v4 sliding puzzle CAPTCHAs."""

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        # CDP port: env var > config.yaml > default 9222
        self.cdp_port = int(
            os.getenv("GEETEST_CDP_PORT", config.get("cdp_port", 9222)),
        )
        self.timeout = config.get("timeout", 60)
        self.max_retries = config.get("max_retries", 3)
        self.headless = config.get("headless", False)
        self.use_opencv_fallback = config.get("use_opencv_fallback", True)
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._solve_lock = asyncio.Lock()
        self._geetest_expected_id: Optional[str] = None
        self._geetest_version = 3

    @property
    def solver_type(self) -> str:
        """Return the solver type identifier."""
        return "geetest"

    def health_check(self) -> Dict[str, Any]:
        """Check imports and the configured browser endpoint without opening a page."""
        try:
            import cv2
            import httpx  # noqa: F401  -- import-only health check
            import numpy  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
            cv2_version = cv2.__version__
            endpoint = f"http://localhost:{self.cdp_port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(endpoint + "/json/version", timeout=0.5) as response:
                    version = json.loads(response.read(65536))
                if not isinstance(version, dict) or not isinstance(version.get("webSocketDebuggerUrl"), str) or not version["webSocketDebuggerUrl"]:
                    raise ValueError("CDP browser endpoint missing")
            except (OSError, ValueError, TypeError):
                return {
                    "healthy": False,
                    "message": f"Browser CDP endpoint unavailable at {endpoint}.",
                    "details": {"cdp_port": self.cdp_port, "cdp_available": False},
                }
            return {
                "healthy": True,
                "message": (
                    "Browser CDP and dependencies available. "
                    "Embedded-image icon challenges are not supported; "
                    "OpenCV alone cannot produce a verified token."
                ),
                "details": {
                    "cdp_port": self.cdp_port,
                    "cdp_available": True,
                    "cdp_port_configurable": True,
                    "opencv_fallback_enabled": self.use_opencv_fallback,
                    "opencv_verified_token_available": False,
                    "embedded_icon_image_supported": False,
                    "opencv_version": cv2_version,
                    "numpy_available": True,
                    "playwright_available": True,
                    "httpx_available": True,
                },
            }
        except ImportError as e:
            return {
                "healthy": False,
                "message": f"Missing required dependency: {e}",
                "details": {"imports_ok": False, "error": str(e)},
            }

    # -- Internal hooks (called by strategy module via duck-typing) ----
    async def _cleanup(self) -> None:
        """Delegate to the browser-cleanup helper."""
        await _browser_cleanup(self)

    async def _solve_with_timeout(
        self, gt: str, challenge: Optional[str], pageurl: str,
    ) -> TaskResult:
        """Execute solve with timeout protection."""
        return await asyncio.wait_for(
            self._execute_solve(gt, challenge, pageurl),
            timeout=self.timeout,
        )

    async def _execute_solve(
        self, gt: str, challenge: Optional[str], pageurl: str,
    ) -> TaskResult:
        """Main solving logic with CDP connection and OpenCV fallback."""
        try:
            return await self._solve_with_browser(gt, challenge, pageurl)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.warning("CDP browser approach failed: %s", e)
            if self.use_opencv_fallback:
                logger.info("Falling back to OpenCV template matching approach")
                return await solve_with_opencv(gt, challenge)
            raise

    async def _solve_with_browser(
        self, gt: str, challenge: Optional[str], pageurl: str,
    ) -> TaskResult:
        """Solve using CDP browser connection."""
        async with async_playwright() as p:
            try:
                refused = await connect_cdp_or_fail(self, p)
                if refused is not None:
                    return refused
                await ensure_context(self)
                self.page = await self.context.new_page()
                await setup_anti_detection(self)
                logger.info("Navigating to %s", pageurl)
                await self.page.goto(
                    pageurl, wait_until="domcontentloaded", timeout=30000,
                )
                if self._geetest_version == 4:
                    if not await activate_v4_widget(self, gt):
                        return TaskResult(success=False, error_code="ERROR_CAPTCHA_UNSOLVABLE",
                                          error_message="GeeTest v4 widget could not be activated")
                await asyncio.sleep(random.uniform(1, 2))
                captcha_type = await detect_captcha_type(self)
                logger.info("Detected captcha type: %s", captcha_type)
                token = await dispatch_challenge(self, captcha_type)
                if token:
                    return TaskResult(success=True, token=token)
                return TaskResult(
                    success=False,
                    error_code="ERROR_CAPTCHA_UNSOLVABLE",
                    error_message="Failed to extract validation token",
                )
            finally:
                await self._cleanup()

    # -- Public solve entry-point --------------------------------------
    async def solve(self, task: Task) -> TaskResult:
        """Serialize access to the browser fields shared by this solver instance."""
        async with self._solve_lock:
            return await self._solve_serial(task)

    async def _solve_serial(self, task: Task) -> TaskResult:
        """Solve a GeeTest CAPTCHA task.

        Args:
            task: Task with params containing gt, challenge, pageurl.

        Returns:
            TaskResult with success status and validation token.
        """
        start_time = time.time()
        params = task.params
        is_v4 = params.get("method_hint") == "geetest_v4"
        gt = (params.get("captcha_id") if is_v4 else
              params.get("gt") or params.get("sitekey") or params.get("googlekey"))
        self._geetest_expected_id = gt if is_v4 else None
        self._geetest_version = 4 if is_v4 else 3
        challenge = params.get("challenge")
        pageurl = params.get("pageurl")

        if not gt or not pageurl:
            return TaskResult(
                success=False,
                error_code="ERROR_BAD_PARAMETERS",
                error_message=("Missing required parameters: captcha_id and pageurl"
                               if is_v4 else "Missing required parameters: gt and pageurl"),
            )

        last_error: Optional[str] = None
        for attempt in range(self.max_retries):
            ctx = AttemptCtx(
                gt=gt, challenge=challenge, pageurl=pageurl,
                attempt=attempt, start_time=start_time,
            )
            result, err = await attempt_solve(self, ctx)
            if result is not None:
                return result
            last_error = err

        solve_time_ms = int((time.time() - start_time) * 1000)
        return TaskResult(
            success=False,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            error_message=(
                f"Failed after {self.max_retries} attempts. "
                f"Last error: {last_error}"
            ),
            solved_at=datetime.now(tz=timezone.utc),
            solve_time_ms=solve_time_ms,
        )


__all__ = ["GeeTestSolver"]


async def _test_solver() -> None:
    """Smoke-test entry point for direct execution."""
    config = {
        "enabled": True,
        "timeout": 60,
        "max_retries": 3,
        "cdp_port": 9234,
        "headless": False,
    }
    solver = GeeTestSolver(config)
    health = solver.health_check()
    print(f"Health check: {health}")
    if not health["healthy"]:
        print("Solver not healthy, exiting")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_test_solver())
