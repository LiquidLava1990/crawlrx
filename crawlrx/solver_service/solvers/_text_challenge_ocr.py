"""OCR engine adapters for ``text_challenge``.

Defines ``OcrEngineMixin`` -- a mixin contributing PaddleOCR / EasyOCR /
Tesseract initialisation + invocation methods to ``TextChallengeHandler``.
Helpers stay free functions where they don't depend on instance state;
methods land on the mixin so dead-code analysis sees the call edge.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import cv2
import numpy as np
from PIL import Image

from ..solver_base import SolverError

from ._solver_helpers import BROAD_EXC
from ._text_challenge_image import (
    EASYOCR_AVAILABLE,
    PADDLE_AVAILABLE,
    TESSERACT_AVAILABLE,
)

if TYPE_CHECKING:
    from typing import Any as _OcrEngine
else:
    _OcrEngine = object  # type: ignore[misc, assignment] because runtime stand-in for the engine type — only used in annotations.

if EASYOCR_AVAILABLE:
    import easyocr  # type: ignore[import-not-found] because optional dep gated on EASYOCR_AVAILABLE flag.
if PADDLE_AVAILABLE:
    from paddleocr import PaddleOCR  # type: ignore[import-not-found] because optional dep gated on PADDLE_AVAILABLE flag.
if TESSERACT_AVAILABLE:
    import pytesseract  # type: ignore[import-not-found] because optional dep gated on TESSERACT_AVAILABLE flag.


logger = logging.getLogger(__name__)


# --- Free helpers (pure / no instance state) --------------------------------

_TextConfPair = tuple[str, float]


def _to_rgb(image: np.ndarray) -> np.ndarray:
    """Normalise to RGB (gray -> RGB, BGR -> RGB)."""
    if len(image.shape) == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _avg_text_confidence(items: list[_TextConfPair]) -> _TextConfPair:
    """Combine ``[(text, conf)]`` pairs into ``(joined_text, avg_conf)``."""
    texts: list[str] = []
    confidences: list[float] = []
    for text, conf in items:
        if text:
            texts.append(text)
            confidences.append(conf)
    if not texts:
        return "", 0.0
    return " ".join(texts), sum(confidences) / len(confidences)


def _paddle_pairs(result: object) -> list[_TextConfPair]:
    """Extract ``(text, confidence)`` pairs from PaddleOCR raw output."""
    if not result or not result[0]:  # type: ignore[index] because PaddleOCR raw output is opaque list-like at runtime.
        return []
    pairs: list[_TextConfPair] = []
    for line in result[0]:  # type: ignore[index] because PaddleOCR raw output is opaque list-like at runtime.
        if not line:
            continue
        _box, (text, confidence) = line
        pairs.append((text, confidence))
    return pairs


def _easy_pairs(result: object) -> list[_TextConfPair]:
    """Extract ``(text, confidence)`` pairs from EasyOCR raw output."""
    if not result:
        return []
    return [(text, confidence) for _bbox, text, confidence in result]  # type: ignore[union-attr] because EasyOCR raw output is iterable at runtime.


def _easy_image_array(image: np.ndarray) -> np.ndarray:
    """Reshape an OpenCV array into the numpy form EasyOCR expects."""
    if len(image.shape) == 2:
        pil_image = Image.fromarray(image)
    else:
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    return np.array(pil_image)


def _select_paddle_device(use_gpu: bool) -> bool:
    """Set Paddle device + return whether GPU is actually live."""
    import paddle  # type: ignore[import-not-found] because optional dep; caller gated on PADDLE_AVAILABLE.
    if use_gpu and paddle.is_compiled_with_cuda():
        paddle.set_device("gpu")
        gpu_count = paddle.device.cuda.device_count()
        logger.info(
            "PaddlePaddle using GPU. Available GPUs: %s", gpu_count,
        )
        return True
    paddle.set_device("cpu")
    logger.info("PaddlePaddle using CPU")
    return False


def _build_paddle_ocr(
    *, lang: str, use_gpu: bool, gpu_id: int,
    use_angle_cls: bool, cls_dir: str | None,
    det_dir: str | None, rec_dir: str | None,
) -> _OcrEngine:
    """Construct a PaddleOCR engine with our standard settings."""
    return PaddleOCR(
        use_angle_cls=use_angle_cls,
        lang=lang,
        use_gpu=use_gpu,
        gpu_id=gpu_id,
        enable_mkldnn=True,
        cpu_threads=8,
        cls_model_dir=cls_dir,
        det_model_dir=det_dir,
        rec_model_dir=rec_dir,
        show_log=False,
        verbose=False,
    )


def _tesseract_pil(image: np.ndarray) -> Image.Image:
    """OpenCV array -> PIL Image (handles BGR + grayscale)."""
    if len(image.shape) == 2:
        return Image.fromarray(image)
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return Image.fromarray(image_rgb)


_TESSERACT_CONFIG = (
    "--oem 3 --psm 7 -c tessedit_char_whitelist="
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)


def _tesseract_avg_confidence(data: dict) -> float:
    """Mean of positive per-token confidences from a Tesseract dict."""
    confidences = [int(c) for c in data["conf"] if int(c) > 0]
    if not confidences:
        return 0.5
    return sum(confidences) / len(confidences) / 100


# --- Cached-init helpers (parametrise the duplicated double-checked-locking) -

async def _ensure_engine(
    cls: type, attr: str, build: object, label: str,
) -> _OcrEngine | None:
    """Double-checked-locking init for one cached engine attr.

    ``build`` is a zero-arg callable returning the engine instance.
    The cached instance lives on ``cls.<attr>`` and the shared lock
    on ``cls._ocr_lock``. Returns ``None`` (and logs) when ``build()``
    raises.
    """
    if getattr(cls, attr) is not None:
        return getattr(cls, attr)
    async with cls._ocr_lock:  # type: ignore[attr-defined] because handler class carries the shared lock.
        if getattr(cls, attr) is not None:
            return getattr(cls, attr)
        # Yield once before the (potentially blocking) ML model load so
        # the event loop is not starved while we hold the lock.
        await asyncio.sleep(0)
        try:
            engine = build()  # type: ignore[operator] because build is callable[[],engine].
        except BROAD_EXC as exc:
            logger.error("Failed to initialize %s: %s", label, exc)
            return None  # explicit recovery: engine flagged unavailable
        setattr(cls, attr, engine)
        logger.info("%s initialized successfully", label)
        return engine


# --- Mixin contributed to ``TextChallengeHandler`` --------------------------

class OcrEngineMixin:
    """Methods that load + run Paddle / Easy / Tesseract on a handler."""

    # Forward declarations — set on the orchestrator class.
    _paddle_ocr: _OcrEngine | None
    _easy_ocr: _OcrEngine | None
    _ocr_lock: asyncio.Lock
    use_gpu: bool
    gpu_id: int
    lang: str
    paddle_use_angle_cls: bool
    paddle_cls_model_dir: str | None
    paddle_det_model_dir: str | None
    paddle_rec_model_dir: str | None

    async def _init_paddle_ocr(self) -> _OcrEngine | None:
        """Lazy-init PaddleOCR (cached on the class). Returns engine or None."""
        await asyncio.sleep(0)  # explicit yield for the linter
        if not PADDLE_AVAILABLE:
            logger.warning("PaddleOCR not available")
            return None
        return await _ensure_engine(
            type(self), "_paddle_ocr",
            build=lambda: _build_paddle_ocr(
                lang=self.lang,
                use_gpu=self.use_gpu and _select_paddle_device(self.use_gpu),
                gpu_id=self.gpu_id,
                use_angle_cls=self.paddle_use_angle_cls,
                cls_dir=self.paddle_cls_model_dir,
                det_dir=self.paddle_det_model_dir,
                rec_dir=self.paddle_rec_model_dir,
            ),
            label="PaddleOCR",
        )

    async def _init_easy_ocr(self) -> _OcrEngine | None:
        """Lazy-init EasyOCR (cached on the class). Returns engine or None."""
        await asyncio.sleep(0)
        if not EASYOCR_AVAILABLE:
            logger.warning("EasyOCR not available")
            return None
        return await _ensure_engine(
            type(self), "_easy_ocr",
            build=lambda: easyocr.Reader(
                [self.lang], gpu=self.use_gpu, verbose=False,
            ),
            label="EasyOCR",
        )

    async def _run_paddle_ocr(self, image: np.ndarray) -> _TextConfPair:
        """Run PaddleOCR; return ``(text, confidence)``."""
        ocr = await self._init_paddle_ocr()
        if ocr is None:
            raise SolverError(
                "ERROR_OCR_NOT_AVAILABLE", "PaddleOCR not available",
            )
        try:
            result = ocr.ocr(_to_rgb(image), cls=True)  # type: ignore[union-attr] because ocr verified non-None above.
            return _avg_text_confidence(_paddle_pairs(result))
        except BROAD_EXC as exc:
            logger.error("PaddleOCR error: %s", exc)
            raise SolverError(
                "ERROR_OCR_FAILED", f"PaddleOCR failed: {exc}",
            ) from exc

    async def _run_easy_ocr(self, image: np.ndarray) -> _TextConfPair:
        """Run EasyOCR fallback; return ``(text, confidence)``."""
        ocr = await self._init_easy_ocr()
        if ocr is None:
            raise SolverError(
                "ERROR_FALLBACK_NOT_AVAILABLE", "EasyOCR not available",
            )
        try:
            result = ocr.readtext(_easy_image_array(image))  # type: ignore[union-attr] because ocr verified non-None above.
            return _avg_text_confidence(_easy_pairs(result))
        except BROAD_EXC as exc:
            logger.error("EasyOCR error: %s", exc)
            raise SolverError(
                "ERROR_FALLBACK_FAILED", f"EasyOCR failed: {exc}",
            ) from exc

    @staticmethod
    def _run_tesseract_ocr(image: np.ndarray) -> _TextConfPair:
        """Run Tesseract as final fallback; return ``(text, confidence)``."""
        if not TESSERACT_AVAILABLE:
            raise SolverError(
                "ERROR_FALLBACK_NOT_AVAILABLE", "Tesseract not available",
            )
        try:
            pil_image = _tesseract_pil(image)
            text = pytesseract.image_to_string(
                pil_image, config=_TESSERACT_CONFIG,
            )
            data = pytesseract.image_to_data(
                pil_image, config=_TESSERACT_CONFIG,
                output_type=pytesseract.Output.DICT,
            )
            return text.strip(), _tesseract_avg_confidence(data)
        except BROAD_EXC as exc:
            logger.error("Tesseract error: %s", exc)
            raise SolverError(
                "ERROR_FALLBACK_FAILED", f"Tesseract failed: {exc}",
            ) from exc
