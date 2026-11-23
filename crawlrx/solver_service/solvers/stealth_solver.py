"""Stealth integration solver (own engine) — thin orchestrator.

This module is the public entry point (``StealthSolver``,
``solve_with_botright`` -- legacy name kept for API stability). All
non-orchestration logic lives in:

* ``_stealth_helpers.py``     — JS payloads, dataclasses, type tables
* ``_botright_browser.py``     — pool acquire / release / cleanup / drain
                                 (engine lane chosen here; default: own engine)
* ``_stealth_strategies.py``  — solve attempt + token extraction + detection

Supports reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional


# --- ErrCode/emit shim -----------------------------------------------------
try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode = _E()  # type: ignore[assignment]  because sentinel ErrCode is unavailable; _E() shim returns ERR_UNKNOWN for any attr

    def emit(*a: object, **kw: object) -> None:  # noqa: ANN001
        return None


from ..solver_base import BaseSolver, SolverTimeoutError  # noqa: E402

from ._solver_helpers import BROAD_EXC as _BROAD_EXC
from ._botright_browser import ( acquire_browser, cleanup_stale, close_all_browsers, close_all_pages, close_botright_client, release_browser, )
from ._stealth_helpers import DEFAULT_TIMEOUT, MAX_RETRIES, MAX_SESSIONS
from ._stealth_strategies import ( build_failure_result, build_param_error_result, build_success_result, captcha_type_for, run_solve_attempt, )


from ..models import Task, TaskResult  # noqa: E402


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# Legacy lane (opt-in via CRAWLRX_STEALTH_ENGINE=botright). The default lane
# is the in-house _stealth_engine, which needs no botright install -- so the
# solver reports healthy whenever the own engine's prerequisites are met.
try:
    import botright as _botright_module
    BOTRIGHT_AVAILABLE = True
except Exception as _botright_err:  # noqa: BLE001
    _botright_module = None
    BOTRIGHT_AVAILABLE = False
    logger.debug("Legacy botright lane unavailable: %s", _botright_err)


def _own_engine_ready() -> bool:
    """The default lane is up when its imports resolve (playwright + stealth)."""
    try:
        from .._stealth_engine.engine import StealthEngine  # noqa: F401
        from ..solvers._stealth_browser_launch import stealth_async  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - readiness probe must never raise
        return False


class StealthSolver(BaseSolver):
    """Stealth challenge handler (own engine) with built-in anti-detection.

    Forges coherent Chromium identities and drives real Chromium via the
    in-house ``_stealth_engine`` (legacy botright lane opt-in via
    CRAWLRX_STEALTH_ENGINE). Covers reCAPTCHA v2/v3, hCaptcha, and
    Cloudflare Turnstile (primary use case).

    Args:
        config: Configuration dict with optional:
            - timeout: solve timeout in seconds (default: 120)
            - max_retries: retry attempts (default: 3)
            - max_sessions: concurrent browser sessions (default: 3)
            - headless: run browser headless (default: False for stability)
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.timeout = config.get("timeout", DEFAULT_TIMEOUT)
        self.max_retries = config.get("max_retries", MAX_RETRIES)
        self.max_sessions = config.get("max_sessions", MAX_SESSIONS)
        self.headless = config.get("headless", False)

        self._botright_client: Optional[Any] = None
        self._browser_pool: List[Any] = []
        self._pool_lock = asyncio.Lock()
        self._pool_semaphore = asyncio.Semaphore(self.max_sessions)
        self._browser_in_use: Dict[int, bool] = {}
        self._browser_last_used: Dict[int, float] = {}

        logger.info(
            "StealthSolver initialized: timeout=%ss, retries=%s, max_sessions=%s",
            self.timeout, self.max_retries, self.max_sessions,
        )

    @property
    def solver_type(self) -> str:
        """Return the solver type identifier."""
        return "stealth"

    # -- Pool delegate hooks (used by strategies module) ----------------
    async def _acquire_browser(self) -> "tuple[Any, int]":
        """Delegate to the browser-pool helper."""
        return await acquire_browser(self, _botright_module, BOTRIGHT_AVAILABLE)

    async def _release_browser(self, idx: int) -> None:
        """Delegate to the browser-pool helper."""
        await release_browser(self, idx)

    async def _cleanup_stale_browsers(self, max_age: int = 300) -> None:
        """Clean up browsers that haven't been used recently."""
        await cleanup_stale(self, max_age)

    # -- Public solve entry-point --------------------------------------
    async def solve(self, task: Task) -> TaskResult:
        """Solve a challenge task through the stealth engine."""
        start_time = time.time()
        deadline = time.monotonic() + self.timeout
        params = task.params
        pageurl = params.get("pageurl") or params.get("url")
        if not pageurl:
            return build_param_error_result()

        captcha_type = captcha_type_for(task)
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = SolverTimeoutError(self.timeout)
                break
            try:
                outcome = await asyncio.wait_for(
                    run_solve_attempt(self, pageurl, captcha_type, start_time, attempt),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                last_error = SolverTimeoutError(self.timeout)
                break
            if outcome.token is not None:
                return build_success_result(outcome.token, start_time)
            last_error = outcome.error or last_error
            captcha_type = outcome.captcha_type or captcha_type
            if attempt < self.max_retries:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(2 ** attempt, remaining))

        return build_failure_result(last_error, start_time, self.max_retries)

    def health_check(self) -> Dict[str, Any]:
        """Return solver health status.

        Healthy when either lane is usable: the default in-house engine or
        the legacy botright lane (opt-in via CRAWLRX_STEALTH_ENGINE).
        """
        try:
            if not _own_engine_ready() and not BOTRIGHT_AVAILABLE:
                return {
                    "healthy": False,
                    "optional": True,
                    "message": (
                        "No stealth lane available. Default engine needs "
                        "playwright + playwright_stealth; the legacy lane "
                        "needs CRAWLRX_STEALTH_ENGINE=botright plus the pip "
                        "botright package."
                    ),
                }
            pool_info = {
                "engine": (
                    "own" if _own_engine_ready() else "botright-legacy"
                ),
                "pool_size": sum(browser is not None for browser in self._browser_pool),
                "max_sessions": self.max_sessions,
                "in_use": sum(1 for v in self._browser_in_use.values() if v),
            }
            return {
                "healthy": True,
                "message": "Stealth solver is available",
                "details": pool_info,
            }
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Health check failed: %s", e)
            return {
                "healthy": False,
                "optional": True,
                "message": f"Botright optional: {str(e)}",
            }

    async def close(self) -> None:
        """Close all browsers and cleanup resources."""
        logger.info("Closing StealthSolver...")
        await close_all_pages(self)
        await close_all_browsers(self)
        await close_botright_client(self)
        logger.info("StealthSolver closed")

    async def __aenter__(self) -> object:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Async context manager exit."""
        await self.close()


# ---------------------------------------------------------------------------
# Convenience entry-point
# ---------------------------------------------------------------------------
def _build_method_map() -> "Dict[str, Any]":
    """Lazy import of CaptchaMethod so this module stays import-cheap."""
    from ..models import CaptchaMethod
    return {
        "recaptcha_v2": CaptchaMethod.RECAPTCHA_V2,
        "recaptcha_v3": CaptchaMethod.RECAPTCHA_V3,
        "hcaptcha": CaptchaMethod.HCAPTCHA,
        "cloudflare": CaptchaMethod.CLOUDFLARE,
        "userrecaptcha": CaptchaMethod.USER_RECAPTCHA_V2,
    }


async def solve_with_botright(
    pageurl: str,
    captcha_type: str = "cloudflare",
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Convenience one-call solve through the stealth engine (legacy name)."""
    method_map = _build_method_map()
    method = method_map.get(captcha_type, method_map["cloudflare"])
    config = {"timeout": timeout, "max_retries": max_retries, "enabled": True}
    solver = StealthSolver(config)

    try:
        task = Task(
            id=f"direct_{int(time.time() * 1000)}",
            method=method,
            params={"pageurl": pageurl},
        )
        result = await solver.solve(task)
        return {
            "success": result.success,
            "token": result.token,
            "error": result.error_message,
            "error_code": result.error_code,
            "duration_ms": result.solve_time_ms,
            "solved_at": (
                result.solved_at.isoformat() if result.solved_at else None
            ),
        }
    finally:
        await solver.close()


__all__ = ["StealthSolver", "solve_with_botright", "BOTRIGHT_AVAILABLE"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main() -> None:
        config = {
            "timeout": 120,
            "max_retries": 3,
            "max_sessions": 2,
            "enabled": True,
        }
        solver = StealthSolver(config)
        health = solver.health_check()
        print(f"Health check: {health}")
        if health.get("healthy"):
            print("Solver is healthy and ready")
        else:
            print(f"Solver not healthy: {health.get('message')}")
        await solver.close()

    asyncio.run(_main())
