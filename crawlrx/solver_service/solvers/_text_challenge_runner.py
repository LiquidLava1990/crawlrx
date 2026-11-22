"""Solve-pipeline + health-probe mixin for ``text_challenge``.

Defines ``SolvePipelineMixin`` -- the OCR cascade (Paddle -> Easy ->
Tesseract), the ``TaskResult`` failure builder, the method validator,
and the per-engine health probe used by ``health_check``. Methods
land on the mixin so dead-code analysis sees the call edge inside the
final orchestrator class.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict

import numpy as np

from ..solver_base import SolverError
from ..models import CaptchaMethod, Task, TaskResult

from ._solver_helpers import BROAD_EXC
from ._text_challenge_image import (
    EASYOCR_AVAILABLE,
    PADDLE_AVAILABLE,
    TESSERACT_AVAILABLE,
    decode_image,
    post_process_text,
    preprocess_image,
)

if TESSERACT_AVAILABLE:
    import pytesseract  # type: ignore[import-not-found] because optional dep gated on TESSERACT_AVAILABLE flag.


logger = logging.getLogger(__name__)


_SUPPORTED_METHODS = (
    CaptchaMethod.IMAGE_CAPTCHA,
    CaptchaMethod.IMAGE,
    CaptchaMethod.BASE64,
)


# --- Free helpers -----------------------------------------------------------

def _failure_result(
    code: str, message: str, solve_time_ms: int,
) -> TaskResult:
    """Compose a typed failure ``TaskResult``."""
    return TaskResult(
        success=False,
        error_code=code,
        error_message=message,
        solve_time_ms=solve_time_ms,
    )


def _validate_method(task: Task) -> TaskResult | None:
    """Return a failure ``TaskResult`` if the method is unsupported."""
    if task.method in _SUPPORTED_METHODS:
        return None
    return _failure_result(
        "ERROR_UNSUPPORTED_METHOD",
        f"Method {task.method} not supported by text challenge resolver",
        0,
    )


def _empty_health() -> Dict[str, object]:
    """Initial health-probe dict."""
    return {
        "healthy": False,
        "paddle_ocr": False,
        "easy_ocr": False,
        "tesseract_ocr": False,
        "gpu_available": False,
        "message": "",
    }


def _summarise_health(health: Dict[str, object]) -> None:
    """Compose ``health['message']`` from per-engine flags."""
    engines: list[str] = []
    if health["paddle_ocr"]:
        engines.append("PaddleOCR")
    if health["easy_ocr"]:
        engines.append("EasyOCR")
    if health["tesseract_ocr"]:
        engines.append("Tesseract")
    health["healthy"] = bool(engines)
    if not health["healthy"]:
        health["message"] = (
            "No OCR engines available. Install easyocr or pytesseract."
        )
        return
    msg = f"Available engines: {', '.join(engines)}"
    if health.get("gpu_available"):
        msg += " (GPU enabled)"
    health["message"] = msg


async def _probe_one_engine(
    health: Dict[str, object], *,
    flag_key: str,
    engine_label: str,
    init: Callable[[], Awaitable[object]],
) -> bool:
    """Probe one OCR engine; writes ``flag_key`` either way."""
    try:
        instance = await init()
        health[flag_key] = instance is not None
        return bool(health[flag_key])
    except BROAD_EXC as exc:
        health[flag_key] = False
        logger.warning("%s health check failed: %s", engine_label, exc)
        return False  # explicit recovery: engine flagged unavailable


def _check_tesseract_health(health: Dict[str, object]) -> None:
    """Probe Tesseract; mutate ``health`` in place."""
    if not TESSERACT_AVAILABLE:
        return
    try:
        version = pytesseract.get_tesseract_version()
        health["tesseract_ocr"] = True
        health["tesseract_version"] = str(version)
    except BROAD_EXC as exc:
        health["tesseract_ocr"] = False
        logger.warning("Tesseract health check failed: %s", exc)
        return None  # explicit recovery: probe non-fatal


# --- Mixin contributed to ``TextChallengeHandler`` --------------------------

class SolvePipelineMixin:
    """Methods that drive the OCR cascade + health probes."""

    # Forward declarations — populated by the orchestrator class.
    fallback_threshold: float
    preprocess_enabled: bool
    max_image_size: int

    # Provided by the OcrEngineMixin sibling.
    async def _init_paddle_ocr(self) -> object | None: ...  # pragma: no cover
    async def _init_easy_ocr(self) -> object | None: ...    # pragma: no cover
    async def _run_paddle_ocr(self, image: np.ndarray) -> tuple[str, float]: ...  # pragma: no cover
    async def _run_easy_ocr(self, image: np.ndarray) -> tuple[str, float]: ...    # pragma: no cover
    @staticmethod
    def _run_tesseract_ocr(image: np.ndarray) -> tuple[str, float]: ...  # pragma: no cover

    async def _run_paddle_first(
        self, image: np.ndarray,
    ) -> tuple[str, float]:
        """First-stage OCR + post-process; returns ``(text, confidence)``."""
        try:
            text, confidence = await self._run_paddle_ocr(image)
        except (SolverError, *BROAD_EXC) as exc:
            logger.warning("PaddleOCR failed; trying fallback engines: %s", exc)
            return "", 0.0
        return post_process_text(text), confidence

    async def _try_easy_fallback(
        self, image: np.ndarray, text: str, confidence: float,
    ) -> tuple[str, float]:
        """If Paddle was weak, try EasyOCR; otherwise return inputs."""
        if confidence >= self.fallback_threshold and text:
            return text, confidence
        logger.info(
            "PaddleOCR confidence %.2f below threshold, "
            "trying EasyOCR fallback",
            confidence,
        )
        try:
            fb_text, fb_conf = await self._run_easy_ocr(image)
        except (SolverError, *BROAD_EXC) as exc:
            logger.warning("EasyOCR fallback failed: %s", exc)
            return text, confidence  # explicit recovery: engine optional
        fb_text = post_process_text(fb_text)
        if fb_text and (fb_conf > confidence or not text):
            logger.info("Using EasyOCR result with confidence %.2f", fb_conf)
            return fb_text, fb_conf
        return text, confidence

    def _try_tesseract_fallback(
        self, image: np.ndarray, text: str, confidence: float,
    ) -> tuple[str, float]:
        """Final-stage Tesseract; return inputs unchanged on failure."""
        if text and confidence >= self.fallback_threshold:
            return text, confidence
        logger.info("Trying Tesseract OCR as final fallback")
        try:
            fb_text, fb_conf = self._run_tesseract_ocr(image)
        except BROAD_EXC as exc:
            logger.warning("Tesseract fallback failed: %s", exc)
            return text, confidence  # explicit recovery: engine optional
        fb_text = post_process_text(fb_text)
        if fb_text:
            logger.info("Using Tesseract result with confidence %.2f", fb_conf)
            return fb_text, fb_conf
        return text, confidence

    async def _solve_pipeline(
        self, task: Task,
    ) -> tuple[str, float]:
        """Decode -> preprocess -> OCR cascade. Returns ``(text, confidence)``."""
        logger.info("Decoding image for task %s", task.id)
        image = decode_image(task)
        logger.info("Preprocessing image for task %s", task.id)
        processed = preprocess_image(
            image,
            enabled=self.preprocess_enabled,
            max_image_size=self.max_image_size,
        )
        logger.info("Running PaddleOCR for task %s", task.id)
        text, confidence = await self._run_paddle_first(processed)
        text, confidence = await self._try_easy_fallback(
            processed, text, confidence,
        )
        text, confidence = self._try_tesseract_fallback(
            processed, text, confidence,
        )
        if confidence < self.fallback_threshold and self.preprocess_enabled:
            raw_text, raw_confidence = self._try_tesseract_fallback(image, "", 0.0)
            if raw_text and raw_confidence > confidence:
                text, confidence = raw_text, raw_confidence
        return text, confidence

    async def _check_paddle_health(self, health: Dict[str, object]) -> None:
        """Probe PaddleOCR + GPU; mutate ``health`` in place."""
        if not PADDLE_AVAILABLE:
            return
        ok = await _probe_one_engine(
            health,
            flag_key="paddle_ocr",
            engine_label="PaddleOCR",
            init=self._init_paddle_ocr,
        )
        if not ok:
            return
        try:
            import paddle  # type: ignore[import-not-found] because optional dep; gated on PADDLE_AVAILABLE above.
            health["gpu_available"] = (
                paddle.is_compiled_with_cuda()
                and paddle.device.cuda.device_count() > 0
            )
        except BROAD_EXC as exc:
            logger.warning("GPU detection failed: %s", exc)
            return None  # explicit recovery: GPU detection non-fatal

    async def _check_easy_health(self, health: Dict[str, object]) -> None:
        """Probe EasyOCR; mutate ``health`` in place."""
        if not EASYOCR_AVAILABLE:
            return
        await _probe_one_engine(
            health,
            flag_key="easy_ocr",
            engine_label="EasyOCR",
            init=self._init_easy_ocr,
        )

    async def _full_health_check(self) -> Dict[str, object]:
        """Run all engine health probes + return the summary dict."""
        health = _empty_health()
        try:
            await self._check_paddle_health(health)
            await self._check_easy_health(health)
            _check_tesseract_health(health)
            _summarise_health(health)
        except BROAD_EXC as exc:
            health["message"] = f"Health check error: {exc}"
            return health  # explicit recovery: probe failure non-fatal
        return health

    def _validate_task_method(self, task: Task) -> TaskResult | None:
        """Method-allow-list gate. Returns failure ``TaskResult`` or ``None``."""
        return _validate_method(task)

    @staticmethod
    def _build_failure_result(
        code: str, message: str, solve_time_ms: int,
    ) -> TaskResult:
        """Type-stable failure-result builder."""
        return _failure_result(code, message, solve_time_ms)
