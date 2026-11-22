"""ML-model registry + COCO mapping for the recaptcha v2 grid solver.

Holds the singleton ``ModelCache`` for the grid detector + Moondream loaders,
plus the CAPTCHA-prompt -> COCO-class-id mapping table and the set of
prompts that fall back to vision LLM verification.

The fast lane is our own ``crawlrx-detr-v1.onnx`` (RT-DETR via onnxruntime --
a base dependency, no torch needed). Ultralytics YOLO remains a fallback for
environments where it is installed; the slow vision-LLM lane stays last.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional


logger = logging.getLogger(__name__)

try:
    import ultralytics  # noqa: F401
    _HAS_ULTRALYTICS = True
except ImportError:
    _HAS_ULTRALYTICS = False

_DETR_WEIGHTS_NAME = "crawlrx-detr-v1.onnx"


# CAPTCHA prompt keyword -> COCO class id (zero == "needs vision LLM").
RECAPTCHA_TO_COCO: dict[str, int] = {
    # Vehicles
    "bus": 5, "buses": 5,
    "car": 2, "cars": 2,
    "truck": 7, "trucks": 7,
    "motorcycle": 3, "motorcycles": 3,
    "bicycle": 1, "bicycles": 1, "bike": 1, "bikes": 1,
    # Traffic / street
    "traffic light": 9, "traffic lights": 9,
    "fire hydrant": 10, "fire hydrants": 10,
    "stop sign": 11, "stop signs": 11,
    "parking meter": 12, "parking meters": 12,
    # Pedestrian / scene -- needs vision-LLM verification (class id 0).
    "crosswalk": 0, "crosswalks": 0,
    "pedestrian crossing": 0,
    "parking lot": 0,
    "bridge": 0,
    "stairs": 0, "stair": 0,
    # Vehicles (extended)
    "train": 6, "trains": 6,
    "boat": 8, "boats": 8,
    "airplane": 4, "airplanes": 4,
}


# Prompt keywords routed to the vision-LLM (Moondream / minicpm-v) path.
MOONDREAM_REQUIRED: frozenset[str] = frozenset({
    "crosswalk", "crosswalks", "pedestrian crossing",
    "parking lot", "bridge", "stairs", "stair",
})


class _OnnxBox:
    """Consumer-facing single detection: ``.cls[0]`` / ``.conf[0]`` surface."""

    __slots__ = ("cls", "conf")

    def __init__(self, cls: Any, conf: Any) -> None:
        self.cls = cls
        self.conf = conf


class _DetrOnnxDetector:
    """RT-DETR ONNX detector matching the ultralytics consumer contract.

    ``detector(img_array, verbose=False) -> [result]`` where ``result.boxes``
    yields ``_OnnxBox`` items. Box geometry is never read by callers, so only
    class/confidence projections of the ``(1, 300, 84)`` output are exposed.
    """

    _INPUT_SIZE = 640

    def __init__(self, weights_path: str) -> None:
        self.weights_path = weights_path
        self._session: Any = None
        self._input_name = "images"

    @staticmethod
    def locate_weights() -> str:
        """Find the detr weights: env override, model cache dir, package tree."""
        override = os.getenv("CRAWLRX_DETR_MODEL_PATH", "").strip()
        if override and os.path.isfile(override):
            return override
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "model_weights", _DETR_WEIGHTS_NAME),
            os.path.join(here, "..", "model_weights", _DETR_WEIGHTS_NAME),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        # Last resort: pull it via the SHA256-verified download hook.
        try:
            from .models.download_models import MODELS, _fetch

            entry = next(m for m in MODELS if m[0] == _DETR_WEIGHTS_NAME)
            os.makedirs(os.path.dirname(candidates[0]), exist_ok=True)
            os.chdir(os.path.dirname(candidates[0]))
            if _fetch(entry[0], entry[1], entry[2]):
                return candidates[0]
        except Exception as exc:  # noqa: BLE001 -- any loader failure degrades to next lane
            logger.debug("detr weights fetch failed: %s", exc)
        raise ImportError(
            f"{_DETR_WEIGHTS_NAME} not found; set CRAWLRX_DETR_MODEL_PATH or run "
            "python -m solvers.models.download_models"
        )

    def load(self) -> None:
        import numpy as np
        import onnxruntime as ort

        self._np = np
        path = self.locate_weights() if self._session is None else self.weights_path
        self._session = ort.InferenceSession(
            path, providers=["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        logger.info("Grid detector loaded: %s", path)

    def __call__(self, img_array: Any, verbose: bool = False) -> list:
        np = getattr(self, "_np", None)
        if np is None:
            import numpy as _np_mod

            np = self._np = _np_mod
        if self._session is None:
            self.load()
        img = img_array[:, :, :3].astype(np.float32) / 255.0
        h, w = img.shape[:2]
        scale = self._INPUT_SIZE / max(h, w)
        resized = np.asarray(
            _resize_rgb(img, self._INPUT_SIZE, self._INPUT_SIZE), dtype=np.float32,
        )
        chw = np.expand_dims(resized.transpose(2, 0, 1), axis=0)
        raw = self._session.run(None, {self._input_name: chw})[0]  # (1, 300, 84)
        det = raw[0]
        boxes_xywh = det[:, :4] * np.array([w, h, w, h], dtype=np.float32) / scale
        scores = det[:, 4:]
        class_ids = scores.argmax(axis=1)
        confidences = scores.max(axis=1)
        order = confidences.argsort()[::-1]
        return [
            _GridResult(
                [
                    _OnnxBox(
                        np.array([int(class_ids[i])], dtype=np.int64),
                        np.array([float(confidences[i])], dtype=np.float32),
                    )
                    for i in order
                ]
            )
        ]


class _GridResult:
    """Consumer-facing result: ``.boxes`` sequence of ``_OnnxBox``."""

    __slots__ = ("boxes",)

    def __init__(self, boxes: List[_OnnxBox]) -> None:
        self.boxes = boxes


def _resize_rgb(img: Any, width: int, height: int) -> Any:
    """Nearest-neighbour resize without PIL/OpenCV dependencies."""
    np = img.__class__.__module__ and __import__("numpy")
    h, w = img.shape[:2]
    row_idx = (np.arange(height) * (h / height)).astype(np.int64).clip(0, h - 1)
    col_idx = (np.arange(width) * (w / width)).astype(np.int64).clip(0, w - 1)
    return img[row_idx][:, col_idx]


class ModelCache:
    """Process-lifetime singleton: loads ML models once + reuses thereafter.

    Using a class-level ``_instance`` rather than ``functools.cache`` keeps
    the explicit ``initialize(cache_dir)`` step required by the YOLO loader.
    """

    _instance: Optional["ModelCache"] = None

    def __new__(cls) -> "ModelCache":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._yolo = None
            inst._moondream = None
            inst._moondream_processor = None
            inst._device = None
            inst._cache_dir = None
            cls._instance = inst
        return cls._instance

    def initialize(self, cache_dir: str) -> None:
        """Initialise model cache with on-disk model directory."""
        self._cache_dir = cache_dir

    @property
    def cache_dir(self) -> str | None:
        """Currently-configured on-disk model cache directory."""
        return self._cache_dir

    @property
    def device(self) -> str:
        """Compute device label (``cuda`` if torch+CUDA, else ``cpu``)."""
        if self._device is None:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Using device: %s", self._device)
        return self._device

    def get_yolo(self) -> object:
        """Fast-lane grid detector: our ONNX detr, ultralytics YOLO fallback.

        Raises ImportError when neither lane is available (caller degrades to
        the vision-LLM lane, non-fatal).
        """
        if self._yolo is None:
            try:
                weights = _DetrOnnxDetector.locate_weights()
            except ImportError:
                weights = None
            if weights is not None:
                logger.info("Loading grid detector (onnx detr): %s", weights)
                self._yolo = _DetrOnnxDetector(weights)
                self._yolo.load()
                logger.info("Grid detector loaded successfully (onnxruntime)")
            elif _HAS_ULTRALYTICS:
                from ultralytics import YOLO

                logger.info("Loading YOLOv11 nano model (ultralytics lane)...")
                self._yolo = YOLO("yolo11n.pt")
                logger.info("YOLOv11 nano loaded successfully")
            else:
                raise ImportError(
                    "No grid detector available: crawlrx-detr-v1.onnx not found "
                    "and ultralytics not installed"
                )
        return self._yolo

    def get_moondream(self) -> tuple[None, None]:
        """Stub -- vision is now routed through ``vision_router``."""
        return None, None
