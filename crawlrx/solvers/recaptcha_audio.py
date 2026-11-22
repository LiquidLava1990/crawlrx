"""reCAPTCHA v2 through its audio challenge, using playwright-recaptcha.

playwright-recaptcha (https://github.com/Xewdy444/Playwright-reCAPTCHA, MIT)
clicks the checkbox, asks for the audio challenge and transcribes it with the
SpeechRecognition package (Google's free web speech endpoint). The library
ships with CrawlRX; ffmpeg must be on the PATH (the Docker image has it). Only the audio path
is used (``image_challenge=False``), so the library's paid CapSolver path never runs.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Optional

from ..captcha_handoff import CaptchaChallenge, page_still_blocked
from ._page_tokens import submit_token
from .base import SolverUnavailable

logger = logging.getLogger("crawlrx.solvers.recaptcha_audio")

_DEFAULT_TIMEOUT_S = 90.0
_ATTEMPTS = 3


class RecaptchaAudioSolver:
    """Solves reCAPTCHA v2 checkboxes with the audio challenge."""

    name = "recaptcha-audio"
    kinds = frozenset({"recaptcha"})

    def __init__(self, recaptchav2: Any, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._recaptchav2 = recaptchav2
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> Optional["RecaptchaAudioSolver"]:
        from playwright_recaptcha import recaptchav2

        if shutil.which("ffmpeg") is None:
            raise SolverUnavailable(
                "needs ffmpeg on the PATH (winget install ffmpeg / brew install ffmpeg / apt install ffmpeg)")

        raw = os.getenv("CRAWLRX_RECAPTCHA_TIMEOUT", "").strip()
        try:
            timeout = float(raw) if raw else _DEFAULT_TIMEOUT_S
        except ValueError:
            logger.warning("CRAWLRX_RECAPTCHA_TIMEOUT=%r is not a number; using %.0fs", raw, _DEFAULT_TIMEOUT_S)
            timeout = _DEFAULT_TIMEOUT_S
        return cls(recaptchav2, timeout)

    async def solve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        # image_challenge=False: audio only, so the library never calls CapSolver (a paid service).
        async with self._recaptchav2.AsyncSolver(page, attempts=_ATTEMPTS) as solver:
            token = await solver.solve_recaptcha(wait=True, wait_timeout=20, image_challenge=False)
        if not token:
            return False
        outcome = await submit_token(page, "recaptcha", token)
        logger.info("reCAPTCHA audio solved on %s (%s)", challenge.url, outcome)
        return not await page_still_blocked(page)
