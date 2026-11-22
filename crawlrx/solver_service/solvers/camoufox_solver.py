"""Camoufox stealth browser solver - thin orchestrator over the split siblings.

Concrete pieces live in:
    * ``_camoufox_bootstrap``  -- sentinel shim
    * ``_camoufox_helpers``    -- fingerprint fallback + Task shim + spec dataclasses
    * ``_camoufox_browser``    -- BrowserInstance + BrowserPool
    * ``_camoufox_strategies`` -- poll loops + per-method handlers (mixin)

Public surface preserved:
    * :class:`CamoufoxSolver`
    * :class:`BrowserPool`
    * :class:`BrowserInstance`
    * :func:`create_solver`
"""
from __future__ import annotations




import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ._camoufox_bootstrap import BROAD_EXC, ErrCode, emit
from ._camoufox_browser import BrowserInstance, BrowserPool  # noqa: F401
from ._camoufox_helpers import RoundOutcome, Task, TaskResult
from ._camoufox_strategies import CamoufoxStrategiesMixin
from ..solver_base import BaseSolver, SolverError

logger = logging.getLogger(__name__)


class CamoufoxSolver(CamoufoxStrategiesMixin, BaseSolver):
    """Camoufox-based challenge handler using stealth browser automation.

    Primary solver for:
        - DataDome (fingerprint-based detection)
        - Cloudflare challenges
        - reCAPTCHA v2/v3 with strict environment-check
        - hCaptcha with advanced fingerprinting

    Features:
        - Browser pool with 3 instances and unique fingerprints
        - Cookie persistence between sessions
        - Human-like behavior (mouse movements, typing patterns)
        - WebRTC leak prevention
        - Automatic retry with different fingerprints
    """

    SOLVER_TIMEOUT = 90  # seconds
    MAX_RETRIES = 3
    POOL_SIZE = 3

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.timeout = config.get('timeout', self.SOLVER_TIMEOUT)
        self.max_retries = config.get('max_retries', self.MAX_RETRIES)
        self.pool_size = config.get('pool_size', self.POOL_SIZE)
        self.headless = config.get('headless', True)
        self.cookie_dir = Path(
            config.get('cookie_dir', Path.home() / ".camoufox_cookies"),
        )
        self._pool: Optional[BrowserPool] = None
        self._pool_lock = asyncio.Lock()

    @property
    def solver_type(self) -> str:
        return "camoufox"

    async def _get_pool(self) -> BrowserPool:
        """Get or initialize the browser pool."""
        async with self._pool_lock:
            if self._pool is None:
                self._pool = BrowserPool(
                    pool_size=self.pool_size,
                    headless=self.headless,
                    cookie_dir=self.cookie_dir,
                )
                if not await self._pool.initialize():
                    raise SolverError(
                        "ERROR_POOL_INIT", "Failed to initialize browser pool",
                    )
            return self._pool

    async def health_check(self) -> Dict[str, Any]:
        """Check solver health by verifying browser pool status."""
        try:
            if not self.enabled:
                return {'healthy': False, 'message': 'Solver disabled'}
            pool = await self._get_pool()
            healthy_instances = sum(
                1 for inst in pool.instances if inst.browser is not None
            )
            if healthy_instances == 0:
                return {
                    'healthy': False,
                    'message': 'No browser instances available',
                }
            return {
                'healthy': True,
                'message': f'{healthy_instances}/{self.pool_size} instances ready',
                'details': {
                    'pool_size': self.pool_size,
                    'healthy_instances': healthy_instances,
                    'timeout': self.timeout,
                    'max_retries': self.max_retries,
                },
            }
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return {
                'healthy': False,
                'message': f'Health check failed: {str(e)}',
            }

    async def solve(self, task: Task) -> TaskResult:
        """Solve CAPTCHA using Camoufox stealth browser."""
        start_time = time.time()
        method = task.params.get("method", "generic")
        url = task.params.get("url") or task.params.get("pageurl")
        if not url:
            return self._camoufox_failure(
                "ERROR_BAD_PARAMETERS",
                "URL required for Camoufox solver",
                start_time,
            )
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            outcome = await self._try_solve_round(task, url, method, attempt)
            if outcome.result is not None:
                return outcome.result
            last_error = outcome.error or last_error
            await self._wait_between_attempts(attempt)
        return self._camoufox_failure(
            "ERROR_CAPTCHA_UNSOLVABLE",
            str(last_error) if last_error else "All retry attempts exhausted",
            start_time,
        )

    def _round_outcome_for_attempt_failure(
        self, attempt: int, error_code: str, message: str, exc: BaseException,
    ) -> RoundOutcome:
        """Build the per-attempt outcome for a caught exception, or final result on last attempt."""
        if attempt >= self.max_retries:
            return RoundOutcome(
                result=self._camoufox_failure(error_code, message, time.time()),
            )
        return RoundOutcome(error=exc)

    @staticmethod
    def _round_outcome_from_browser_result(result: TaskResult) -> RoundOutcome:
        """Map a browser solve outcome to a per-attempt round outcome."""
        if result.success:
            return RoundOutcome(result=result)
        if result.error_code in ("ERROR_BAD_PARAMETERS", "ERROR_URL_BLOCKED"):
            return RoundOutcome(result=result)
        return RoundOutcome()

    async def _try_solve_round(
        self, task: Task, url: str, method: str, attempt: int,
    ) -> RoundOutcome:
        """One retry attempt; returns either a final TaskResult or an error."""
        try:
            logger.info(
                "[Attempt %s/%s] Solving %s for %s",
                attempt,
                self.max_retries,
                method,
                url,
            )
            return self._round_outcome_from_browser_result(
                await self._solve_with_browser(task, url, method),
            )
        except asyncio.TimeoutError as exc:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            logger.warning("Attempt %s timed out", attempt)
            return self._round_outcome_for_attempt_failure(
                attempt,
                "ERROR_TIMEOUT",
                f"Solver timed out after {self.timeout}s",
                exc,
            )
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            logger.error("Attempt %s failed: %s", attempt, exc)
            return self._round_outcome_for_attempt_failure(
                attempt, "ERROR_CAPTCHA_UNSOLVABLE", str(exc), exc,
            )

    async def _wait_between_attempts(self, attempt: int) -> None:
        """Random-jittered sleep between retry attempts."""
        if attempt >= self.max_retries:
            return
        wait_time = random.uniform(2, 5) * attempt
        logger.info("Retrying in %.1fs...", wait_time)
        await asyncio.sleep(wait_time)

    async def _close_page_safely(self, page: Any) -> None:
        """Best-effort page close; swallows known browser-shutdown errors."""
        if not page:
            return
        try:
            await page.close()
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    async def _navigate_or_failure(
        self, page: Any, url: str, start_time: float,
    ) -> Optional[TaskResult]:
        """Navigate ``page`` to ``url``; return failure TaskResult or ``None`` on success."""
        response = await page.goto(
            url, wait_until='domcontentloaded', timeout=self.timeout * 1000,
        )
        if response:
            return None
        return TaskResult(
            success=False,
            error_code="ERROR_NAVIGATION_FAILED",
            error_message="Failed to navigate to URL",
            solved_at=datetime.now(tz=timezone.utc),
            solve_time_ms=int((time.time() - start_time) * 1000),
        )

    async def _solve_with_browser(
        self, task: Task, url: str, method: str,
    ) -> TaskResult:
        """Execute solve using a browser instance."""
        start_time = time.time()
        pool = await self._get_pool()
        instance = await pool.get_instance()
        async with instance._lease_lock:
            return await self._solve_with_instance(instance, task, url, method, start_time)

    async def _solve_with_instance(self, instance, task, url, method, start_time):
        """Keep each pooled browser's page and cookie state owned by one solve."""
        page = None
        try:
            page = await instance.new_page()
            failure = await self._navigate_or_failure(page, url, start_time)
            if failure is not None:
                return failure
            handler = self._captcha_handler_for(method)
            return await handler(page, task, start_time)
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            logger.error("Browser solve error: %s", exc)
            raise exc
        finally:
            await instance.save_cookies()
            await self._close_page_safely(page)
            if instance.page is page:
                instance.page = None
            instance.last_used = time.time()

    async def close(self) -> None:
        """Close all browser instances in the pool."""
        if self._pool:
            await self._pool.close_all()
            self._pool = None


def create_solver(config: Dict[str, Any]) -> CamoufoxSolver:
    """Create a CamoufoxSolver instance."""
    return CamoufoxSolver(config)
