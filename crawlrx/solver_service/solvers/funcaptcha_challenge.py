"""FunCaptcha / Arkose Labs solver -- thin orchestrator.

Coordinates three focused sibling modules:

- ``_funcaptcha_browser``     -- iframe / DOM helpers, screenshot
  capture, audio download, ``WhisperTranscriber``.
- ``_funcaptcha_rotation``    -- configured local ``VisionModel`` +
  ``parse_audio_direction``.
- ``_funcaptcha_strategies``  -- per-strategy runners (botright /
  vision-AI / camoufox / audio fallback) returning
  ``StrategyOutcome`` records.

This module owns the public ``FuncaptchaChallengeHandler`` class, the
``SolverResult`` / ``RotationStep`` dataclasses, the multi-strategy
fan-out, and the ``solve()`` entry-point.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Optional playwright import. Other parts of the solver fleet flag
# ``PLAYWRIGHT_AVAILABLE`` so callers can degrade gracefully.
try:
    from playwright.async_api import Page, async_playwright

    PLAYWRIGHT_AVAILABLE = True
except (ImportError, Exception):  # noqa: BLE001 because optional dep probe
    Page = None  # type: ignore[assignment, misc] because optional-dep stub keeps signatures usable when playwright absent
    async_playwright = None  # type: ignore[assignment] because optional-dep stub keeps callers loadable without playwright
    PLAYWRIGHT_AVAILABLE = False


from ._sentinel_shim import ErrCode, emit  # noqa: E402
from ._solver_helpers import BROAD_EXC  # noqa: E402
from ..solver_base import BaseSolver  # noqa: E402
from ..models import Task, TaskResult  # noqa: E402

from ._funcaptcha_browser import (  # noqa: E402
    ARROW_SELECTORS,
    AUDIO_BUTTON,
    AUDIO_DOWNLOAD_LINK,
    AUDIO_PLAY_BUTTON,
    DETECTION_PATTERNS,
    FC_CONTAINER,
    FC_IFRAME,
    PUZZLE_IMAGE_SELECTOR,
    WhisperTranscriber,
    detect,
    extract_token,
)
from ._funcaptcha_rotation import VisionModel  # noqa: E402
from ._funcaptcha_strategies import (  # noqa: E402
    StrategyOutcome,
    try_audio_fallback,
    try_botright,
    try_camoufox,
    try_vision_ai,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SolverResult:
    """Internal result shape -- adapted into ``TaskResult`` for callers."""

    success: bool = False
    token: str = ""
    error: str = ""
    strategy: str = ""
    solve_time_ms: float = 0.0
    method: str = ""
    duration: float = 0.0
    bypassed: bool = False

    def to_task_result(self) -> TaskResult:
        """Translate this internal shape into the canonical ``TaskResult``."""
        if self.success:
            return TaskResult(
                success=True, token=self.token,
                solve_time_ms=int(self.solve_time_ms or self.duration * 1000),
            )
        return TaskResult(
            success=False,
            error_code="ERROR_FUNCAPTCHA",
            error_message=self.error,
            solve_time_ms=int(self.solve_time_ms or self.duration * 1000),
        )


@dataclass
class RotationStep:
    """Represents a single rotation step in the challenge."""

    step_number: int
    total_steps: int
    direction: Optional[str] = None
    completed: bool = False


def _outcome_to_result(outcome: StrategyOutcome) -> SolverResult:
    """Translate a strategy outcome into the orchestrator's internal shape."""
    return SolverResult(
        success=outcome.success,
        token=outcome.token,
        error=outcome.error_message,
        strategy=outcome.method,
        method=outcome.method,
        duration=outcome.duration,
        bypassed=outcome.bypassed,
        solve_time_ms=outcome.duration * 1000,
    )


class FuncaptchaChallengeHandler(BaseSolver):
    """FunCaptcha/Arkose Labs solver with multi-strategy fallback.

    Strategies (in order):
      1. Botright -- built-in challenge resolution.
      2. Vision AI -- analyses 3D puzzle screenshots with the configured endpoint.
      3. Camoufox -- stealth browser to avoid detection.
      4. Audio fallback -- transcribes audio challenges.
    """

    solver_type = "funcaptcha"

    # Re-export selector tables / patterns at the class level so external
    # code that consulted them via the class name keeps working.
    DETECTION_PATTERNS = DETECTION_PATTERNS
    FC_CONTAINER = FC_CONTAINER
    FC_IFRAME = FC_IFRAME
    ARROW_SELECTORS = ARROW_SELECTORS
    PUZZLE_IMAGE_SELECTOR = PUZZLE_IMAGE_SELECTOR
    AUDIO_BUTTON = AUDIO_BUTTON
    AUDIO_PLAY_BUTTON = AUDIO_PLAY_BUTTON
    AUDIO_DOWNLOAD_LINK = AUDIO_DOWNLOAD_LINK

    def __init__(self, config: object = None) -> None:
        if config is None:
            config = {"enabled": True, "timeout": 180}
        super().__init__(config)
        self.max_retries: int = config.get("retries", 3)
        # Lazy-loaded ML helpers: instances cache state across rotations.
        self.vision: VisionModel = VisionModel()
        self.whisper: WhisperTranscriber = WhisperTranscriber()

    async def detect(self, page: Page) -> bool:
        """Detect if FunCaptcha/Arkose Labs is present on the page."""
        return await detect(page)

    async def _solve_page(self, page: Page) -> SolverResult:
        """Drive the multi-strategy fan-out against ``page``."""
        start_time = time.time()
        if not await self.detect(page):
            return SolverResult(
                success=False,
                error="FunCaptcha not detected on page",
                duration=time.time() - start_time,
            )
        _LOGGER.info("FunCaptcha detected, starting multi-strategy solve")
        for runner in (try_botright, try_vision_ai, try_camoufox, try_audio_fallback):
            outcome = await runner(self, page, start_time)
            if outcome.success:
                return _outcome_to_result(outcome)
        return SolverResult(
            success=False, error="All strategies failed",
            duration=time.time() - start_time,
        )

    async def _solve_via_task(self, task: Task) -> TaskResult:
        """Open a Playwright page from a ``Task`` payload then solve."""
        pageurl = task.params.get("pageurl", "")
        if not pageurl:
            return TaskResult(
                success=False,
                error_code="ERROR_BAD_PARAMETERS",
                error_message="Missing pageurl",
            )
        start = time.time()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(pageurl, wait_until="networkidle", timeout=30000)
                result = await self._solve_page(page)
                await browser.close()
                return result.to_task_result()
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            return TaskResult(
                success=False,
                error_code="ERROR_INTERNAL",
                error_message=str(exc),
                solve_time_ms=int((time.time() - start) * 1000),
            )

    async def solve(self, task: object) -> TaskResult:
        """Accept a ``Task`` (open a page) or a ``Page`` (older callers)."""
        if isinstance(task, Task):
            return await self._solve_via_task(task)
        # Older callers pass a Page directly; preserve that path.
        result = await self._solve_page(task)  # type: ignore[arg-type] because older callers pass a Page where the new signature wants a Task
        return result.to_task_result()

    def health_check(self) -> Dict[str, Any]:
        """Report which strategy backends are importable in this env."""
        details: Dict[str, Any] = _probe_optional_backends()
        healthy = PLAYWRIGHT_AVAILABLE
        if healthy:
            optional = ", ".join(
                k for k, v in details.items() if v and k != "playwright"
            ) or "none"
            message = f"FunChallenge resolver ready. Optional strategies: {optional}"
        else:
            message = "Playwright required. Install: pip install playwright && playwright install"
        return {"healthy": healthy, "details": details, "message": message}


# Probe results are stable for the process lifetime: a missing module
# is not going to appear without a restart. Cache once.
_OPTIONAL_BACKEND_CACHE: Optional[Dict[str, bool]] = None


def _probe_optional_backends() -> Dict[str, bool]:
    """Snapshot ``importlib`` availability for the four strategy backends."""
    global _OPTIONAL_BACKEND_CACHE
    if _OPTIONAL_BACKEND_CACHE is not None:
        return dict(_OPTIONAL_BACKEND_CACHE)
    import importlib.util
    cache: Dict[str, bool] = {
        "playwright": PLAYWRIGHT_AVAILABLE,
        "botright": importlib.util.find_spec("botright") is not None,
        "camoufox": importlib.util.find_spec("camoufox") is not None,
        "vision_model": importlib.util.find_spec("transformers") is not None,
        "vision_endpoint_configured": bool(os.environ.get("CRAWLRX_VISION_URL")),
        "whisper": importlib.util.find_spec("faster_whisper") is not None,
    }
    _OPTIONAL_BACKEND_CACHE = cache
    return dict(cache)


def create_solver(config: object = None) -> FuncaptchaChallengeHandler:
    """Create and return a ``FuncaptchaChallengeHandler`` instance."""
    return FuncaptchaChallengeHandler(config)
