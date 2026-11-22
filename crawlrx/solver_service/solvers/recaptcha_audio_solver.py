"""reCAPTCHA v2 audio challenge solver — thin orchestrator.

Coordinates two focused sibling modules:

- ``_recaptcha_audio_transcribe`` -- speech-to-text adapters
  (faster-whisper + openai-whisper fallback).
- ``_recaptcha_audio_browser``    -- page-flow against the recaptcha
  iframes (checkbox click, audio-mode switch, mp3 download, response
  submit, token extraction).

This module owns the public ``ReCaptchaAudioSolver`` class, the CDP
attach + retry envelope, and the ``solve()`` entry-point.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

# canonical FlareSolverr/solver_base.py (the sibling under solvers/ is
# only a backwards-compat shim that re-exports from the parent).

from ._sentinel_shim import ErrCode, emit  # noqa: E402
from ._solver_helpers import BROAD_EXC  # noqa: E402
from ..solver_base import BaseSolver  # noqa: E402

from ._recaptcha_audio_browser import (  # noqa: E402
    capture_audio_state,
    click_recaptcha_checkbox,
    extract_token,
    get_audio_challenge,
    is_challenge_visible,
    random_delay,
    submit_audio_response,
    switch_to_audio_challenge,
)
from ._recaptcha_audio_transcribe import transcribe_audio, transcribe_audio_async  # noqa: E402


class ReCaptchaAudioSolver(BaseSolver):
    """Solve reCAPTCHA v2 via the audio challenge + Whisper transcription.

    Connects via Playwright CDP to port 9236.
    """

    # CDP port for Cruz.
    CDP_PORT = 9236

    @property
    def solver_type(self) -> str:
        return "recaptcha_v2_audio"

    def __init__(self, config: object = None) -> None:
        if config is None:
            config = {"enabled": True, "timeout": 90}
        super().__init__(config)
        self.retries = config.get("retries", 3)
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    async def _solve_once(self, page: Page, pageurl: str) -> Optional[str]:
        """Drive one full challenge sweep. Returns the token or ``None``."""
        await page.goto(pageurl, wait_until="networkidle", timeout=60000)
        await random_delay(2, 3)
        await click_recaptcha_checkbox(page)
        await random_delay(2, 4)

        if await is_challenge_visible(page):
            await switch_to_audio_challenge(page)
            await random_delay(2, 3)
            audio_state = await capture_audio_state(page)
            audio_path = await get_audio_challenge(page)
            transcribed_text = await transcribe_audio_async(audio_path)
            if not transcribed_text:
                raise RuntimeError("Audio transcription failed")
            await submit_audio_response(page, transcribed_text, expected_state=audio_state)
            await random_delay(2, 4)

        for _ in range(10):
            token = await extract_token(page)
            if token:
                return token
            await asyncio.sleep(1)
        return None

    @staticmethod
    async def _silent_close(target: Any, attr: str = "close") -> None:
        """Close ``target`` swallowing failures (cleanup-only)."""
        if not target:
            return
        try:
            close = getattr(target, attr)
            await close()
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    async def _cleanup(self, page: Any, playwright: Any) -> None:
        """Close page/context/browser/playwright, ignoring teardown errors."""
        await self._silent_close(page)
        await self._silent_close(self.context)
        await self._silent_close(self.browser)
        if playwright:
            try:
                await playwright.stop()
            except BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    async def _refresh_page_quiet(self, page: Any) -> None:
        """Reset the page to about:blank, swallowing failures."""
        try:
            await page.goto("about:blank")
            await random_delay(2, 3)
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    async def _solve_with_retries(self, page: Any, pageurl: str) -> dict:
        """Drive the retry loop. Returns a public ``solve()`` result dict."""
        last_error: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            try:
                token = await asyncio.wait_for(
                    self._solve_once(page, pageurl), timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                emit(ErrCode.TIMEOUT_ERROR, "unhandled")
                last_error = f"Timeout after {self.timeout}s"
                await self._refresh_page_quiet(page)
                continue
            except BROAD_EXC as e:
                emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
                last_error = str(e)
                await self._refresh_page_quiet(page)
                continue
            if token:
                return {"success": True, "token": token, "attempt": attempt}
            last_error = "No token received"
        return {
            "success": False,
            "error": f"Failed after {self.retries} attempts. Last error: {last_error}",
        }

    async def solve(self, task: dict) -> dict:
        """Solve a reCAPTCHA v2 task.

        Args:
            task: dict with ``googlekey`` (or ``sitekey``) and ``pageurl``.

        Returns dict with ``success``, ``token``, optional ``error``.
        """
        googlekey = task.get("googlekey") or task.get("sitekey")
        pageurl = task.get("pageurl")
        if not googlekey:
            return {"success": False, "error": "Missing required field: googlekey"}
        if not pageurl:
            return {"success": False, "error": "Missing required field: pageurl"}

        playwright = None
        page = None
        try:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.connect_over_cdp(
                f"http://localhost:{self.CDP_PORT}",
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await self.context.new_page()
            return await self._solve_with_retries(page, pageurl)
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return {"success": False, "error": f"Solver error: {str(e)}"}
        finally:
            await self._cleanup(page, playwright)


    def health_check(self) -> dict:
        """Report whether at least one transcription backend is importable."""
        details = {
            "playwright": True,
            "faster_whisper": _module_available("faster_whisper"),
            "whisper": _module_available("whisper"),
        }
        healthy = details["faster_whisper"] or details["whisper"]
        message = (
            "ReCaptchaAudioSolver ready"
            if healthy
            else "Install faster-whisper or openai-whisper for audio transcription"
        )
        return {"healthy": healthy, "details": details, "message": message}


def _module_available(module_name: str) -> bool:
    """True iff ``module_name`` is importable in the current environment."""
    import importlib.util
    return importlib.util.find_spec(module_name) is not None
