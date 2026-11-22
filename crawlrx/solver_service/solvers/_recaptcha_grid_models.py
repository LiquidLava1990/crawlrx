"""ML-model registry + COCO mapping for the recaptcha v2 grid solver.

Holds the singleton ``ModelCache`` for YOLOv11/Moondream loaders, plus
the CAPTCHA-prompt -> COCO-class-id mapping table and the set of
prompts that fall back to vision LLM verification.
"""
from __future__ import annotations

import logging
from typing import Optional


logger = logging.getLogger(__name__)


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
        """Lazy-load YOLOv11-nano. Raises ImportError if ultralytics absent."""
        if self._yolo is None:
            from ultralytics import YOLO
            logger.info("Loading YOLOv11 nano model...")
            self._yolo = YOLO("yolo11n.pt")
            logger.info("YOLOv11 nano loaded successfully")
        return self._yolo

    def get_moondream(self) -> tuple[None, None]:
        """Stub -- vision is now routed through ``vision_router``."""
        return None, None
