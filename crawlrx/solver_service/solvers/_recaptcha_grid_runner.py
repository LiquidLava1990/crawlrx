"""Solve-loop + retry helpers for the recaptcha v2 grid solver.

Defines ``GridSolveLoopMixin`` -- the per-attempt solving logic, the
retry driver, the health probe, and the failure-result composer used
by ``ReCaptchaGridSolver.solve``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from playwright.async_api import Page, async_playwright

from ..solver_base import SolverError

from ._solver_helpers import BROAD_EXC


logger = logging.getLogger(__name__)


# --- Free helpers (no instance state) ---------------------------------------

def _now_utc() -> datetime:
    """Time-zone aware UTC ``now`` -- separate so tests can monkey-patch."""
    return datetime.now(tz=timezone.utc)


def _elapsed_ms(start_time: float) -> int:
    """Whole milliseconds since ``start_time`` (``time.time()`` baseline)."""
    return int((time.time() - start_time) * 1000)


async def _quiet_close(target: Any) -> None:
    """Close ``target`` swallowing failures (cleanup-only path)."""
    if target is None:
        return
    try:
        await target.close()
    except BROAD_EXC as exc:
        logger.debug("quiet close swallowed: %s", exc)
        return None  # explicit recovery: cleanup-only


async def _quietly_close_browser(context: Any, browser: Any) -> None:
    """Cleanup-only: close context then browser, swallowing all errors."""
    await _quiet_close(context)
    await _quiet_close(browser)


def _probe_required_imports() -> tuple[bool, str | None]:
    """Try the load-bearing imports; return ``(ok, error_msg_if_any)``."""
    try:
        import numpy as np
        from PIL import Image
        from playwright.async_api import async_playwright
        del np, Image, async_playwright  # probe-only; discard bindings
    except ImportError as exc:
        return False, str(exc)
    return True, None


def _probe_optional_module(module_path: str) -> bool:
    """Return ``True`` if ``module_path`` imports cleanly."""
    try:
        __import__(module_path)
    except ImportError:
        return False
    return True


def _probe_torch() -> tuple[bool, bool]:
    """``(torch_ok, cuda_ok)`` -- second is ``False`` when torch missing."""
    try:
        import torch
    except ImportError:
        return False, False
    return True, bool(torch.cuda.is_available())


def _yolo_cache_present() -> bool:
    """Check the user's ultralytics cache for any ``*.pt`` weights."""
    yolo_path = Path.home() / ".cache" / "ultralytics"
    return yolo_path.exists() and any(yolo_path.glob("*.pt"))


def _moondream_cache_present(cache_dir: str | None) -> bool:
    """Check the configured cache_dir for moondream weights."""
    if not cache_dir:
        return False
    moondream_path = Path(cache_dir)
    return moondream_path.exists() and any(
        moondream_path.rglob("moondream*"),
    )


def _build_health_message(warnings: list[str]) -> str:
    """Compose the short summary line for the health-check payload."""
    if not warnings:
        return "Solver ready - all dependencies available"
    return f"Solver ready (with warnings: {'; '.join(warnings)})"


# --- Mixin contributed to ``ReCaptchaGridSolver`` ---------------------------

class GridSolveLoopMixin:
    """Retry + solve-attempt loop, health-check, failure-result composer."""

    # Forward declarations -- populated by the orchestrator class.
    cdp_port: int
    max_retries: int
    timeout: int
    model_cache: Any  # ModelCache from _recaptcha_grid_models

    # Provided by sibling mixins (DOM + tile classification).
    async def _wait_for_challenge_frame(self, page: Page, timeout: int = 10) -> Any: ...  # pragma: no cover
    def _find_recaptcha_frame(self, page: Page) -> Any: ...  # pragma: no cover
    async def _click_checkbox(self, frame: Any) -> None: ...  # pragma: no cover
    async def _human_delay(self, lo: float | None = None, hi: float | None = None) -> None: ...  # pragma: no cover
    async def _extract_token(self, page: Page) -> str | None: ...  # pragma: no cover
    async def _solve_image_grid(self, frame: Any, page: Page) -> str: ...  # pragma: no cover
    async def _open_browser_context(self, p: Any, cdp_port: int) -> tuple[Any, Any]: ...  # pragma: no cover

    # --- Result helpers -----------------------------------------------------

    def _failure_result(
        self, code: str, message: str, start_time: float,
    ) -> Any:
        """Compose a typed failure ``TaskResult`` for ``solve()``."""
        from ..models import TaskResult
        return TaskResult(
            success=False,
            error_code=code,
            error_message=message,
            solved_at=_now_utc(),
            solve_time_ms=_elapsed_ms(start_time),
        )

    def _success_result(self, token: str, start_time: float) -> Any:
        """Compose a typed success ``TaskResult`` for ``solve()``."""
        from ..models import TaskResult
        return TaskResult(
            success=True,
            token=token,
            solved_at=_now_utc(),
            solve_time_ms=_elapsed_ms(start_time),
        )

    # --- Solve helpers -----------------------------------------------------

    async def _open_solving_page(
        self, p: Any, pageurl: str,
    ) -> tuple[Any, Any, Any]:
        """Connect via CDP, navigate to ``pageurl``, return browser+context+page."""
        browser, context = await self._open_browser_context(p, self.cdp_port)
        page = await context.new_page()
        logger.info("Navigating to %s", pageurl)
        await page.goto(pageurl, wait_until="networkidle", timeout=30000)
        await self._human_delay(1.0, 2.0)
        return browser, context, page

    async def _click_then_solve(self, page: Page) -> str:
        """Click checkbox, then either harvest token or solve the grid."""
        recaptcha_frame = self._find_recaptcha_frame(page)
        if not recaptcha_frame:
            raise SolverError(
                "ERROR_CAPTCHA_NOT_FOUND",
                "Could not find reCAPTCHA iframe",
            )
        await self._click_checkbox(recaptcha_frame)
        await self._human_delay(0.5, 1.5)
        challenge_frame = await self._wait_for_challenge_frame(page)
        if challenge_frame:
            return await self._solve_image_grid(challenge_frame, page)
        token = await self._extract_token(page)
        if token:
            return token
        raise SolverError(
            "ERROR_CAPTCHA_UNSOLVABLE",
            "No challenge appeared and no token found",
        )

    async def _do_solve(self, _googlekey: str, pageurl: str, _attempt: int) -> str:
        """One pass through the solve flow with browser cleanup."""
        browser = None
        context = None
        try:
            async with async_playwright() as p:
                browser, context, page = await self._open_solving_page(
                    p, pageurl,
                )
                token = await self._click_then_solve(page)
                await _quietly_close_browser(context, browser)
                return token
        except BROAD_EXC:
            await _quietly_close_browser(context, browser)
            raise

    async def _solve_with_retry(
        self, googlekey: str, pageurl: str, attempt: int,
    ) -> str:
        """Attempt to solve recaptcha with timeout handling."""
        return await asyncio.wait_for(
            self._do_solve(googlekey, pageurl, attempt),
            timeout=self.timeout,
        )

    async def _try_one_attempt(
        self, googlekey: str, pageurl: str, attempt: int,
    ) -> str | None:
        """One retry pass. Returns token or ``None`` (re-raises on last)."""
        try:
            return await self._solve_with_retry(googlekey, pageurl, attempt)
        except BROAD_EXC as exc:
            logger.warning("Attempt %s failed: %s", attempt, exc)
            if attempt >= self.max_retries:
                raise
            await asyncio.sleep(random.uniform(1.0, 3.0))
            return None  # explicit recovery: caller continues retry loop

    async def _solve_with_retries_loop(
        self, googlekey: str, pageurl: str, start_time: float,
    ) -> Any:
        """Drive the retry loop and return a ``TaskResult`` directly."""
        for attempt in range(1, self.max_retries + 1):
            token = await self._try_one_attempt(googlekey, pageurl, attempt)
            if token:
                return self._success_result(token, start_time)
        raise SolverError(
            "ERROR_CAPTCHA_UNSOLVABLE", "All retry attempts failed",
        )

    # --- Health-check -------------------------------------------------------

    def _probe_model_caches(self, details: Dict[str, Any]) -> None:
        """Mutate ``details`` with yolo / moondream cache-presence flags."""
        try:
            details["yolo_cached"] = _yolo_cache_present()
            details["moondream_cached"] = _moondream_cache_present(
                self.model_cache.cache_dir,
            )
        except BROAD_EXC as exc:
            logger.debug("Model cache probe failed: %s", exc)
            details["yolo_cached"] = False
            details["moondream_cached"] = False
            return None  # explicit recovery: cache probe non-fatal

    def _full_health_check(self) -> Dict[str, Any]:
        """Run every dependency probe + return the summary payload."""
        details: Dict[str, Any] = {}
        warnings: list[str] = []

        imports_ok, err = _probe_required_imports()
        if not imports_ok:
            return {
                "healthy": False,
                "message": f"Missing required dependency: {err}",
                "details": {"imports_ok": False, "error": err},
            }
        details["imports_ok"] = True

        details["yolo_available"] = _probe_optional_module("ultralytics")
        if not details["yolo_available"]:
            warnings.append("YOLO not installed")
        details["moondream_available"] = _probe_optional_module(
            "transformers",
        )
        if not details["moondream_available"]:
            warnings.append("transformers not installed")
        torch_ok, cuda_ok = _probe_torch()
        details["torch_available"] = torch_ok
        details["cuda_available"] = cuda_ok
        if not torch_ok:
            warnings.append("torch not installed")

        self._probe_model_caches(details)

        return {
            "healthy": True,
            "message": _build_health_message(warnings),
            "details": {
                "cdp_port": self.cdp_port,
                "imports_ok": details.get("imports_ok", False),
                "yolo_available": details.get("yolo_available", False),
                "yolo_cached": details.get("yolo_cached", False),
                "moondream_available": details.get(
                    "moondream_available", False,
                ),
                "moondream_cached": details.get("moondream_cached", False),
                "torch_available": details.get("torch_available", False),
                "cuda_available": details.get("cuda_available", False),
                "warnings": warnings,
            },
        }
