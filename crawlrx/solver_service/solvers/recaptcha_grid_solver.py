"""reCAPTCHA v2 IMAGE GRID solver -- YOLOv11 + vision-LLM (orchestrator).

Solves "select all images with [buses / traffic lights / crosswalks /
bicycles / fire hydrants]" challenges. The implementation is split:

  - ``_recaptcha_grid_models``  -- ``ModelCache`` + COCO mapping + flags
  - ``_recaptcha_grid_tiles``   -- ``TileClassificationMixin`` (YOLO + LLM)
  - ``_recaptcha_grid_dom``     -- ``GridDomMixin`` (frames + buttons)
  - ``_recaptcha_grid_runner``  -- ``GridSolveLoopMixin`` (retries + health)

This file owns the public ``ReCaptchaGridSolver`` class, the
``create_solver`` factory, and re-exports the symbols pinned by the
cc_harness public_api snapshot.
"""
from __future__ import annotations


try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"
    ErrCode = _E()

    def emit(payload: object = None, detail: object = None) -> None:
        """Stub emit when sentinel_errors is unavailable; no-op."""
        del payload, detail


import logging
import time
from typing import Any, Dict

from ..solver_base import BaseSolver, SolverError, SolverTimeoutError
from ..models import Task, TaskResult


from ._solver_helpers import BROAD_EXC

# Re-exports pinned by the cc_harness public_api snapshot.
from ._recaptcha_grid_models import (
    MOONDREAM_REQUIRED,
    RECAPTCHA_TO_COCO,
    ModelCache,
)
from ._recaptcha_grid_tiles import TileClassificationMixin
from ._recaptcha_grid_dom import GridDomMixin
from ._recaptcha_grid_runner import GridSolveLoopMixin


logger = logging.getLogger(__name__)


class ReCaptchaGridSolver(
    TileClassificationMixin,
    GridDomMixin,
    GridSolveLoopMixin,
    BaseSolver,
):
    """reCAPTCHA v2 IMAGE GRID solver using YOLOv11 + vision-LLM."""

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.model_cache = ModelCache()
        self.model_cache.initialize(
            config.get("model_cache_dir", "./model_cache"),
        )
        self.cdp_port: int = config.get("cdp_port", 9231)
        self.max_retries: int = config.get("max_retries", 3)
        self.confidence_threshold: float = config.get(
            "confidence_threshold", 0.7,
        )
        self.human_delay_min: float = config.get("human_delay_min", 0.5)
        self.human_delay_max: float = config.get("human_delay_max", 1.5)

    @property
    def solver_type(self) -> str:
        """Solver discriminator used by the registry."""
        return "recaptcha_v2_grid"

    async def solve(self, task: Task) -> TaskResult:
        """Solve a reCAPTCHA v2 image-grid task."""
        start_time = time.time()
        try:
            googlekey = (
                task.params.get("googlekey")
                or task.params.get("sitekey")
            )
            pageurl = task.params.get("pageurl")
            if not googlekey:
                raise SolverError(
                    "ERROR_BAD_PARAMETERS", "Missing 'googlekey' parameter",
                )
            if not pageurl:
                raise SolverError(
                    "ERROR_BAD_PARAMETERS", "Missing 'pageurl' parameter",
                )
            return await self._solve_with_retries_loop(
                googlekey, pageurl, start_time,
            )
        except SolverTimeoutError:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            return self._failure_result(
                "ERROR_TIMEOUT",
                f"Solver timed out after {self.timeout}s",
                start_time,
            )
        except SolverError as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            return self._failure_result(exc.code, exc.message, start_time)
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            logger.exception("Unexpected error solving reCAPTCHA")
            return self._failure_result(
                "ERROR_CAPTCHA_UNSOLVABLE", str(exc), start_time,
            )

    async def _solve_image_grid(self, challenge_frame: Any, page: Any) -> str:
        """Solve the image-grid challenge across up to 5 inner attempts."""
        max_attempts = 5
        for grid_attempt in range(max_attempts):
            logger.info(
                "Solving image grid (attempt %s/%s)",
                grid_attempt + 1, max_attempts,
            )
            attempt_token = await self._one_grid_attempt(
                challenge_frame, page,
            )
            if attempt_token is not None:
                return attempt_token
        raise SolverError(
            "ERROR_CAPTCHA_UNSOLVABLE",
            f"Failed to solve after {max_attempts} attempts",
        )

    async def _one_grid_attempt(
        self, challenge_frame: Any, page: Any,
    ) -> str | None:
        """One inner attempt: capture, classify, click, verify."""
        prompt_text = await self._get_challenge_prompt(challenge_frame)
        if not prompt_text:
            raise SolverError(
                "ERROR_CAPTCHA_UNSOLVABLE",
                "Could not read challenge prompt",
            )
        logger.info("Challenge prompt: %s", prompt_text)
        target_classes = self._extract_target_classes(prompt_text)
        logger.info("Target COCO classes: %s", target_classes)
        grid_size = await self._detect_grid_size(challenge_frame)
        logger.info("Grid size: %sx%s", grid_size, grid_size)
        grid_image = await self._capture_grid(challenge_frame, grid_size)
        tiles = self._split_grid(grid_image, grid_size)
        matching_tiles = await self._analyze_tiles(
            tiles, target_classes, prompt_text,
        )
        if not matching_tiles:
            logger.warning("No matching tiles found, refreshing challenge")
            await self._click_refresh(challenge_frame)
            await self._human_delay(2.0, 3.0)
            return None
        await self._click_tiles(challenge_frame, matching_tiles, grid_size)
        await self._human_delay(1.0, 2.0)
        if await self._has_new_images(challenge_frame):
            logger.info("New images appeared, continuing to solve...")
            return None
        await self._click_verify(challenge_frame)
        await self._human_delay(1.0, 2.0)
        token = await self._extract_token(page)
        if token:
            logger.info("reCAPTCHA solved successfully")
            return token
        if await self._is_error_visible(challenge_frame):
            logger.warning("Verification failed, retrying...")
            return None
        return None

    def health_check(self) -> Dict[str, Any]:
        """Verify imports work + model cache state (models are optional)."""
        return self._full_health_check()


__all__ = [
    "ReCaptchaGridSolver",
    "ModelCache",
    "RECAPTCHA_TO_COCO",
    "MOONDREAM_REQUIRED",
    "create_solver",
    "BROAD_EXC",
    "ErrCode",
    "emit",
]


def create_solver(config: Dict[str, Any]) -> BaseSolver:
    """Create a ``ReCaptchaGridSolver`` instance."""
    return ReCaptchaGridSolver(config)
