"""Camoufox solve-strategy mixin: poll loops, challenge handlers, success/error builders.

Extracted from ``camoufox_solver.py`` so the orchestrator stays focused on
configuration + retry orchestration.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ._camoufox_bootstrap import BROAD_EXC, ErrCode, emit
from ._camoufox_helpers import (
    CHALLENGE_SPECS,
    TOKEN_POLL_SPECS,
    ChallengeSpec,
    PollLoopParams,
    Task,
    TaskResult,
    TokenPollSpec,
)


class CamoufoxStrategiesMixin:
    """Solve-strategy primitives for the CamoufoxSolver orchestrator.

    Designed as a mixin so :class:`CamoufoxSolver` inherits these methods
    without growing the file beyond the 500-LOC sibling cap.
    """

    # ------------------------------------------------------------------ utils
    def _camoufox_failure(
        self, error_code: str, error_message: str, start_time: float,
    ) -> TaskResult:
        """Build a standard TaskResult failure shape with timing."""
        return TaskResult(
            success=False,
            error_code=error_code,
            error_message=error_message,
            solved_at=datetime.now(tz=timezone.utc),
            solve_time_ms=int((time.time() - start_time) * 1000),
        )

    def _success_result(self, token: str, start_time: float) -> TaskResult:
        """Build the standard TaskResult success shape with timing."""
        return TaskResult(
            success=True,
            token=token,
            solved_at=datetime.now(tz=timezone.utc),
            solve_time_ms=int((time.time() - start_time) * 1000),
        )

    def _error_state_result(
        self, error_text: str, start_time: float, label: str,
    ) -> TaskResult:
        """Build a TaskResult for an error-state detection."""
        return self._camoufox_failure(
            "ERROR_BLOCKED", f"{label} error: {error_text}", start_time,
        )

    # ---------------------------------------------------------------- handler
    def _captcha_handler_for(self, method: str):
        """Return the bound handler coroutine for ``method`` (defaults to generic)."""
        handlers = {
            'cloudflare': self._handle_cloudflare,
            'datadome': self._handle_datadome,
            'recaptcha_v2': self._handle_recaptcha_v2,
            'userrecaptcha': self._handle_recaptcha_v2,
            'recaptcha_v3': self._handle_recaptcha_v3,
            'hcaptcha': self._handle_hcaptcha,
        }
        return handlers.get(method, self._handle_generic)

    # ------------------------------------------------------------- poll loops
    async def _run_poll_loop(
        self,
        *,
        page: Any,
        start_time: float,
        params: PollLoopParams,
    ) -> TaskResult:
        """Shared polling loop driving params.check(page) once per interval."""
        try:
            await asyncio.sleep(params.initial_wait)
            for _ in range(params.max_polls):
                await asyncio.sleep(params.poll_interval)
                outcome = await params.check(page)
                if outcome is not None:
                    return outcome
            return self._camoufox_failure(
                "ERROR_TIMEOUT", params.timeout_message, start_time,
            )
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return self._camoufox_failure(
                "ERROR_CAPTCHA_UNSOLVABLE",
                f"{params.label} solve error: {e}",
                start_time,
            )

    async def _poll_for_cookie_solve(
        self,
        *,
        page: Any,
        start_time: float,
        spec: ChallengeSpec,
    ) -> TaskResult:
        """Generic polling loop: wait for challenge to clear, then read a cookie."""
        params = PollLoopParams(
            label=spec.label,
            initial_wait=spec.initial_wait,
            poll_interval=spec.poll_interval,
            max_polls=spec.max_polls,
            check=lambda pg: self._cookie_check(pg, spec, start_time),
            timeout_message=f"{spec.label} challenge not resolved in time",
        )
        return await self._run_poll_loop(
            page=page, start_time=start_time, params=params,
        )

    async def _poll_for_token_solve(
        self,
        *,
        page: Any,
        task: Task,
        start_time: float,
        spec: TokenPollSpec,
    ) -> TaskResult:
        """Generic poll loop reading a token via JS evaluation."""
        params = PollLoopParams(
            label=spec.label,
            initial_wait=spec.initial_wait,
            poll_interval=spec.poll_interval,
            max_polls=spec.max_polls,
            check=lambda pg: self._token_check(pg, spec, start_time),
            timeout_message=f"{spec.label} token not extracted in time",
        )
        return await self._run_poll_loop(
            page=page, start_time=start_time, params=params,
        )

    # --------------------------------------------------------------- per-iter
    async def _cookie_check(
        self,
        page: Any,
        spec: ChallengeSpec,
        start_time: float,
    ) -> Optional[TaskResult]:
        """One iteration check for the cookie-poll family."""
        if await self._challenge_cleared(page, spec):
            cookie_value = await self._read_target_cookie(page, spec.cookie_name)
            if cookie_value:
                return self._success_result(cookie_value, start_time)
        err = await self._check_for_error_state(page, spec)
        if err is not None:
            return self._error_state_result(err, start_time, spec.label)
        return None

    async def _token_check(
        self,
        page: Any,
        spec: TokenPollSpec,
        start_time: float,
    ) -> Optional[TaskResult]:
        """One iteration check for the token-poll family."""
        token = await page.evaluate(spec.token_js)
        if token:
            return self._success_result(token, start_time)
        return None

    async def _challenge_cleared(self, page: Any, spec: ChallengeSpec) -> bool:
        """True iff none of the challenge markers are present on the page."""
        if spec.challenge_eval_js is not None:
            return not await page.evaluate(spec.challenge_eval_js)
        return not await self._any_element_exists(page, spec.challenge_selectors)

    async def _read_target_cookie(self, page: Any, name: str) -> Optional[str]:
        """Return the value of the named cookie if present."""
        cookies = await page.context.cookies()
        return next((c["value"] for c in cookies if c["name"] == name), None)

    async def _check_for_error_state(
        self, page: Any, spec: ChallengeSpec,
    ) -> Optional[str]:
        """Return error text if page shows the configured error pattern."""
        if spec.error_eval_js is None:
            return None
        text = await page.evaluate(spec.error_eval_js)
        if text and spec.error_marker in text:
            return text
        return None

    async def _any_element_exists(self, page: Any, selectors) -> bool:
        """Check if any of the selectors exist on the page."""
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    return True
            except BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return False

    # ----------------------------------------------------------- per-method handlers
    async def _handle_cloudflare(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Handle Cloudflare challenge."""
        return await self._poll_for_cookie_solve(
            page=page, start_time=start_time, spec=CHALLENGE_SPECS["cloudflare"],
        )

    async def _handle_datadome(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Handle DataDome fingerprint-based protection."""
        return await self._poll_for_cookie_solve(
            page=page, start_time=start_time, spec=CHALLENGE_SPECS["datadome"],
        )

    async def _handle_recaptcha_v2(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Handle reCAPTCHA v2 via Camoufox stealth + JS token polling."""
        return await self._poll_for_token_solve(
            page=page, task=task, start_time=start_time,
            spec=TOKEN_POLL_SPECS["recaptcha_v2"],
        )

    async def _handle_recaptcha_v3(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Handle reCAPTCHA v3 via Camoufox + token polling."""
        return await self._poll_for_token_solve(
            page=page, task=task, start_time=start_time,
            spec=TOKEN_POLL_SPECS["recaptcha_v3"],
        )

    async def _handle_hcaptcha(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Handle hCaptcha via Camoufox + token polling."""
        return await self._poll_for_token_solve(
            page=page, task=task, start_time=start_time,
            spec=TOKEN_POLL_SPECS["hcaptcha"],
        )

    async def _handle_generic(
        self, page: Any, task: Task, start_time: float,
    ) -> TaskResult:
        """Generic handler: wait, then return any cookie/HTML snapshot."""
        return await self._poll_for_token_solve(
            page=page, task=task, start_time=start_time,
            spec=TOKEN_POLL_SPECS["generic"],
        )
