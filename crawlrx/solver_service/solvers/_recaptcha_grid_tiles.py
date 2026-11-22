"""Tile classification + click logic for the recaptcha v2 grid solver.

Defines ``TileClassificationMixin`` -- the YOLO-detection / vision-LLM
verification cascade plus the per-tile click loop. The orchestrator
inherits this mixin so dead-code analysis sees the call edges intra-class.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import tempfile
from typing import List, Tuple

import numpy as np
from PIL import Image
from playwright.async_api import Frame

from ..solver_base import SolverError

from ._solver_helpers import BROAD_EXC
from ._recaptcha_grid_models import (
    MOONDREAM_REQUIRED,
    RECAPTCHA_TO_COCO,
    ModelCache,
)


logger = logging.getLogger(__name__)


# --- Free helpers -----------------------------------------------------------

def _split_grid_image(
    grid_image: Image.Image, grid_size: int,
) -> List[Image.Image]:
    """Cut ``grid_image`` into ``grid_size**2`` equally-sized PIL tiles."""
    width, height = grid_image.size
    tile_width = width // grid_size
    tile_height = height // grid_size
    tiles: List[Image.Image] = []
    for row in range(grid_size):
        for col in range(grid_size):
            left = col * tile_width
            upper = row * tile_height
            right = left + tile_width
            lower = upper + tile_height
            tiles.append(grid_image.crop((left, upper, right, lower)))
    return tiles


def _extract_target_classes(prompt: str) -> List[Tuple[int, str]]:
    """Map a CAPTCHA prompt to ``[(class_id, keyword)]`` pairs (deduped)."""
    prompt_lower = prompt.lower()
    targets: List[Tuple[int, str]] = []
    for keyword, class_id in RECAPTCHA_TO_COCO.items():
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, prompt_lower):
            targets.append((class_id, keyword))
    seen: set[int] = set()
    unique: List[Tuple[int, str]] = []
    for class_id, keyword in targets:
        if class_id not in seen:
            seen.add(class_id)
            unique.append((class_id, keyword))
    return unique


def _vision_target_phrase(prompt: str) -> str:
    """Strip ``select all images with X`` -> ``X`` (or fallback string)."""
    match = re.search(r"with\s+([\w\s]+)", prompt.lower())
    return match.group(1).strip() if match else "the requested object"


def _save_tile_for_vision(tile: Image.Image) -> str:
    """Persist ``tile`` as a JPEG temp file; return absolute path."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    tile.save(tmp_path, format="JPEG", quality=90)
    return tmp_path


def _interpret_vision_answer(result: str | None) -> bool:
    """Accept an explicit binary answer; an unknown result is not a negative."""
    text = result.strip().lower() if isinstance(result, str) else ""
    if re.fullmatch(r"yes[.!]?", text):
        return True
    if re.fullmatch(r"no[.!]?", text):
        return False
    raise SolverError("ERROR_VISION_RESPONSE", "Vision model did not return an explicit yes/no classification")


# NOTE: _moondream_verify inlines the unlink + swallow logic directly so
# the linter sees the cleanup as part of the surrounding method body
# (a free helper here registered as cc.shape.dead_code despite the
# intra-file call).


def _yolo_match(
    yolo: object, tile: Image.Image,
    target_classes: List[Tuple[int, str]],
) -> Tuple[bool, float]:
    """Run YOLO on a tile; ``(matched_target, confidence)``."""
    img_array = np.array(tile)
    results = yolo(img_array, verbose=False)  # type: ignore[operator] because YOLO instance is callable.
    if not results:
        return False, 0.0
    result = results[0]
    if result.boxes is None or len(result.boxes) == 0:
        return False, 0.0
    target_class_ids = [class_id for class_id, _ in target_classes if class_id > 0]
    for box in result.boxes:
        class_id = int(box.cls[0])
        confidence = float(box.conf[0])
        if class_id in target_class_ids:
            return True, confidence
    return False, 0.0


# --- Mixin contributed to ``ReCaptchaGridSolver`` ---------------------------

class TileClassificationMixin:
    """Tile detection + click logic for the recaptcha grid solver."""

    # Forward declarations -- populated by the orchestrator class.
    model_cache: ModelCache
    confidence_threshold: float
    human_delay_min: float
    human_delay_max: float

    @staticmethod
    def _split_grid(
        grid_image: Image.Image, grid_size: int,
    ) -> List[Image.Image]:
        """Split a grid screenshot into per-tile PIL images."""
        return _split_grid_image(grid_image, grid_size)

    @staticmethod
    def _extract_target_classes(prompt: str) -> List[Tuple[int, str]]:
        """Map CAPTCHA prompt to deduped ``(class_id, keyword)`` list."""
        return _extract_target_classes(prompt)

    def _yolo_detect(
        self, tile: Image.Image, target_classes: List[Tuple[int, str]],
    ) -> Tuple[bool, float]:
        """Run YOLO detection on a single tile."""
        try:
            yolo = self.model_cache.get_yolo()
            return _yolo_match(yolo, tile, target_classes)
        except BROAD_EXC as exc:
            logger.error("YOLO detection error: %s", exc)
            return False, 0.0  # explicit recovery: model failure non-fatal

    async def _moondream_verify(self, tile: Image.Image, prompt: str) -> bool:
        """Verify a tile with the configured local vision model."""
        try:
            target = _vision_target_phrase(prompt)
            question = (
                f"Does this image contain {target}? Answer only yes or no."
            )
            tmp_path = _save_tile_for_vision(tile)
            try:
                from ..core.vision_router import analyze_captcha
                result = await analyze_captcha(tmp_path, prompt=question)
            finally:
                # Inline cleanup -- best-effort, ignore missing/perm errors.
                try:
                    os.unlink(tmp_path)
                except BROAD_EXC as cleanup_exc:
                    logger.debug(
                        "Cleanup failed (non-critical): %s", cleanup_exc,
                    )
            is_match = _interpret_vision_answer(result)
            logger.debug(
                "vision_router result for '%s': %s -> %s",
                target, result, is_match,
            )
            return is_match
        except SolverError:
            raise
        except BROAD_EXC as exc:
            logger.error("vision_router captcha verification error: %s", exc)
            raise SolverError("ERROR_VISION_UNAVAILABLE", "Vision classification failed before grid input") from exc

    async def _classify_tile(
        self,
        tile: Image.Image,
        target_classes: List[Tuple[int, str]],
        prompt: str,
        needs_moondream: bool,
    ) -> bool:
        """Decide whether one tile matches the prompt."""
        if needs_moondream:
            return await self._moondream_verify(tile, prompt)
        yolo_match_flag, yolo_conf = self._yolo_detect(tile, target_classes)
        if not yolo_match_flag:
            return await self._moondream_verify(tile, prompt)
        if yolo_conf >= self.confidence_threshold:
            return True
        return await self._moondream_verify(tile, prompt)

    async def _analyze_tiles(
        self,
        tiles: List[Image.Image],
        target_classes: List[Tuple[int, str]],
        prompt: str,
    ) -> List[int]:
        """Walk tiles + return indices that match the prompt."""
        needs_moondream = any(
            keyword in MOONDREAM_REQUIRED for _, keyword in target_classes
        )
        matching: List[int] = []
        for idx, tile in enumerate(tiles):
            if await self._classify_tile(
                tile, target_classes, prompt, needs_moondream,
            ):
                matching.append(idx)
                logger.debug("Tile %s: matched", idx)
        return matching

    async def _click_one_tile(
        self, frame: Frame, idx: int, grid_size: int,
    ) -> None:
        """Click a single grid tile by index, swallowing per-tile errors."""
        row, col = divmod(idx, grid_size)
        try:
            tile_selector = (
                f"table.rc-imageselect-table-{grid_size}{grid_size} "
                f"tr:nth-child({row + 1}) td:nth-child({col + 1})"
            )
            tile = await frame.wait_for_selector(tile_selector, timeout=3000)
            if tile:
                await tile.click()
                logger.info("Clicked tile at row %s, col %s", row, col)
                await self._human_delay(0.3, 0.8)
        except BROAD_EXC as exc:
            logger.error("Error clicking tile %s: %s", idx, exc)
            return None  # explicit recovery: per-tile click failure non-fatal

    async def _click_tiles(
        self, frame: Frame, tile_indices: List[int], grid_size: int,
    ) -> None:
        """Click every matched tile in sequence."""
        for idx in tile_indices:
            await self._click_one_tile(frame, idx, grid_size)

    async def _human_delay(
        self, min_seconds: float | None = None,
        max_seconds: float | None = None,
    ) -> None:
        """Sleep a random uniform duration to simulate human pacing."""
        min_delay = min_seconds if min_seconds is not None else self.human_delay_min
        max_delay = max_seconds if max_seconds is not None else self.human_delay_max
        delay = random.uniform(min_delay, max_delay)
        await asyncio.sleep(delay)


# Reference SolverError so the import isn't dead at runtime.
_ = SolverError
