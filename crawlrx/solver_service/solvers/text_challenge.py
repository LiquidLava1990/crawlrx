"""PaddleOCR-based text/image challenge handler (orchestrator).

Solves distorted text CAPTCHAs using deep-learning OCR with optional
GPU acceleration. The implementation is split across:

  - ``_text_challenge_image``  -- decode + preprocessing + optional-dep flags
  - ``_text_challenge_ocr``    -- ``OcrEngineMixin`` (Paddle/Easy/Tesseract)
  - ``_text_challenge_runner`` -- ``SolvePipelineMixin`` (cascade + health)

This file owns the public ``TextChallengeHandler`` class (composed
from the two mixins), the ``create_solver`` factory, and re-exports
the optional-dep flags captured by the cc_harness public_api snapshot.
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


import asyncio
import logging
import time
from typing import Any, Dict, Optional

from ..solver_base import BaseSolver, SolverError, SolverValidationError
from ..models import Task, TaskResult


from ._solver_helpers import BROAD_EXC

# Re-export flags pinned by the cc_harness public_api snapshot.
from ._text_challenge_image import (
    EASYOCR_AVAILABLE,
    PADDLE_AVAILABLE,
    TESSERACT_AVAILABLE,
)
from ._text_challenge_ocr import OcrEngineMixin
from ._text_challenge_runner import SolvePipelineMixin

# The harness ``public_api`` snapshot pins these names on this module too.
try:
    from paddleocr import PaddleOCR  # noqa: F401
except ImportError:
    PaddleOCR = None  # type: ignore[assignment, misc] because optional dep absent at runtime; rebind to None.

try:
    import pytesseract  # noqa: F401
except ImportError:
    pytesseract = None  # type: ignore[assignment, misc] because optional dep absent at runtime; rebind to None.


logger = logging.getLogger(__name__)


class TextChallengeHandler(OcrEngineMixin, SolvePipelineMixin, BaseSolver):
    """Text-challenge handler -- PaddleOCR with EasyOCR/Tesseract fallbacks."""

    _paddle_ocr: Optional[Any] = None
    _easy_ocr: Optional[Any] = None
    _ocr_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.confidence_threshold: float = config.get(
            "confidence_threshold", 0.5,
        )
        self.fallback_threshold: float = config.get(
            "fallback_threshold", 0.5,
        )
        self.use_gpu: bool = config.get("use_gpu", True)
        self.gpu_id: int = config.get("gpu_id", 0)
        self.lang: str = config.get("lang", "en")
        self.preprocess_enabled: bool = config.get(
            "preprocess_enabled", True,
        )
        self.max_image_size: int = config.get("max_image_size", 4096)
        self.paddle_use_angle_cls: bool = config.get(
            "paddle_use_angle_cls", True,
        )
        self.paddle_cls_model_dir: Optional[str] = config.get(
            "paddle_cls_model_dir",
        )
        self.paddle_det_model_dir: Optional[str] = config.get(
            "paddle_det_model_dir",
        )
        self.paddle_rec_model_dir: Optional[str] = config.get(
            "paddle_rec_model_dir",
        )

    @property
    def solver_type(self) -> str:
        """Solver discriminator used by the registry."""
        return "image_captcha"

    async def solve(self, task: Task) -> TaskResult:
        """Solve a text/image CAPTCHA. Returns a ``TaskResult``."""
        start_time = time.time()
        try:
            invalid = self._validate_task_method(task)
            if invalid is not None:
                return invalid
            text, confidence = await self._solve_pipeline(task)
            return self._compose_success(text, confidence, start_time)
        except (SolverValidationError, SolverError) as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            return self._build_failure_result(
                exc.code, exc.message,
                int((time.time() - start_time) * 1000),
            )
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            logger.exception("Unexpected error solving CAPTCHA: %s", exc)
            return self._build_failure_result(
                "ERROR_INTERNAL",
                f"Internal error: {exc}",
                int((time.time() - start_time) * 1000),
            )

    def _compose_success(
        self, text: str, confidence: float, start_time: float,
    ) -> TaskResult:
        """Branch on extracted text quality + return a ``TaskResult``."""
        solve_time_ms = int((time.time() - start_time) * 1000)
        if not text:
            return self._build_failure_result(
                "ERROR_CAPTCHA_UNSOLVABLE",
                "Could not extract text from CAPTCHA image",
                solve_time_ms,
            )
        if confidence < self.confidence_threshold:
            return self._build_failure_result(
                "ERROR_CAPTCHA_UNSOLVABLE",
                f"OCR confidence {confidence:.2f} is below the configured threshold",
                solve_time_ms,
            )
        logger.info(
            "Successfully solved CAPTCHA: '%s' with confidence %.2f",
            text, confidence,
        )
        return TaskResult(
            success=True, token=text, solve_time_ms=solve_time_ms,
        )

    async def health_check(self) -> Dict[str, object]:
        """Engine availability + GPU summary."""
        return await self._full_health_check()


__all__ = [
    "TextChallengeHandler",
    "create_solver",
    "PADDLE_AVAILABLE",
    "EASYOCR_AVAILABLE",
    "TESSERACT_AVAILABLE",
    "PaddleOCR",
    "pytesseract",
    "BROAD_EXC",
    "ErrCode",
    "emit",
]


def create_solver(config: Dict[str, Any]) -> TextChallengeHandler:
    """Create a new ``TextChallengeHandler`` instance."""
    return TextChallengeHandler(config)
