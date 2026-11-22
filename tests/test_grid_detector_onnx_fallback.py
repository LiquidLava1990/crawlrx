"""Regression: grid-solver fast lane must work without torch/ultralytics.

ModelCache.get_yolo() prefers our crawlrx-detr-v1.onnx (onnxruntime, already a
base dependency) and only falls back to ultralytics YOLO if present. Caught
live: the unified image ships neither ultralytics nor torch, so every grid
tile degraded to the slow vision-model lane.

The consumer contract (_recaptcha_grid_tiles._yolo_match) needs:
  detector(img_array, verbose=False) -> [result]
  result.boxes -> iterable of boxes with int(box.cls[0]) / float(box.conf[0])
Box geometry is never read, so the adapter exposes scores only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Tuple

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[1]
if (_REPO / "crawlrx-solver").is_dir():  # master tree: solvers/ is top-level
    if str(_REPO / "crawlrx-solver") not in sys.path:
        sys.path.insert(0, str(_REPO / "crawlrx-solver"))
    import solvers._recaptcha_grid_models as _MODELS_MODULE  # noqa: E402
    from solvers._recaptcha_grid_models import (  # noqa: E402,F401
        ModelCache, _DetrOnnxDetector, _OnnxBox,
    )
else:  # public-release layout: crawlrx.solver_service.solvers
    import crawlrx.solver_service.solvers._recaptcha_grid_models as _MODELS_MODULE  # noqa: E402
    from crawlrx.solver_service.solvers._recaptcha_grid_models import (  # noqa: E402,F401
        ModelCache, _DetrOnnxDetector, _OnnxBox,
    )


class _FakeSession:
    """Returns a fixed (1, 300, 84) score matrix: det 0 -> bus (5) @0.9."""

    def __init__(self, scores: np.ndarray) -> None:
        self._scores = scores

    def run(self, _outputs: list, feed: dict) -> list:
        return [self._scores]


def _scores_matrix() -> np.ndarray:
    scores = np.zeros((1, 300, 84), dtype=np.float32)
    scores[0, 0, 4 + 5] = 0.9   # COCO class 5 (bus), 0.9 confidence
    scores[0, 1, 4 + 1] = 0.5   # COCO class 1 (bicycle), 0.5
    return scores


def test_adapter_exposes_yolo_consumer_contract() -> None:
    det = _DetrOnnxDetector.__new__(_DetrOnnxDetector)  # bypass model load
    det._session = _FakeSession(_scores_matrix())
    det._input_name = "images"

    tile = Image_tile()
    results = det(tile, verbose=False)
    assert len(results) == 1
    boxes = list(results[0].boxes)
    assert len(boxes) == 300
    assert int(boxes[0].cls[0]) == 5
    assert float(boxes[0].conf[0]) == pytest.approx(0.9, abs=1e-5)


def test_detections_sorted_by_confidence() -> None:
    det = _DetrOnnxDetector.__new__(_DetrOnnxDetector)
    det._session = _FakeSession(_scores_matrix())
    det._input_name = "images"
    boxes = list(det(Image_tile(), verbose=False)[0].boxes)
    confs = [float(b.conf[0]) for b in boxes[:3]]
    assert confs == sorted(confs, reverse=True)
    assert confs[0] == pytest.approx(0.9, abs=1e-5)


def test_get_yolo_prefers_onnx_without_ultralytics(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """With no ultralytics installed, get_yolo returns our ONNX adapter."""
    cache = ModelCache.__new__(ModelCache)
    cache._yolo = None
    cache._cache_dir = str(tmp_path)
    monkeypatch.setattr(_MODELS_MODULE, "_HAS_ULTRALYTICS", False)

    fake_model = tmp_path / "crawlrx-detr-v1.onnx"
    fake_model.write_bytes(b"x")  # loader only checks existence in this path
    monkeypatch.setattr(
        _DetrOnnxDetector, "load", lambda self: None, raising=True,
    )
    monkeypatch.setattr(
        _DetrOnnxDetector, "locate_weights", staticmethod(lambda: str(fake_model)), raising=True,
    )

    yolo = cache.get_yolo()
    assert isinstance(yolo, _DetrOnnxDetector)


def Image_tile() -> np.ndarray:
    return np.zeros((8, 8, 3), dtype=np.uint8)
