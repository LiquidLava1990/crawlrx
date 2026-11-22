"""Rotation-direction analysis for the FunCaptcha 3D puzzle.

Owns the configured vision route and the optional legacy Moondream2 helper.
Keeping ML imports here avoids the
``cc.arch.god_module_imports`` finding on the orchestrator.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Optional


from ._sentinel_shim import ErrCode, emit  # noqa: E402
from ._solver_helpers import BROAD_EXC  # noqa: E402

_LOGGER = logging.getLogger(__name__)


class VisionModel:
    """Lazy wrapper around the Moondream2 vision model.

    Holds the loaded torch model + tokenizer pair and exposes a single
    ``analyze`` entry point. One instance per solver keeps the model
    cached across rotation steps.
    """

    def __init__(self) -> None:
        self._model: object = None
        self._tokenizer: object = None

    def _load(self) -> bool:
        """Load Moondream2; ``True`` on success, ``False`` on import/runtime fail."""
        if self._model is not None:
            return True
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import torch
        except ImportError:
            return False
        try:
            model_id = "vikhyatk/moondream2"
            dtype = torch.float16 if torch.cuda.is_available() else torch.float32
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = AutoModelForCausalLM.from_pretrained(
                model_id, torch_dtype=dtype, trust_remote_code=True,
            ).to(device)
            self._tokenizer = AutoTokenizer.from_pretrained(model_id)
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            _LOGGER.error("Failed to load Moondream2: %s", exc)
            return False
        return True

    def analyze(self, screenshot_bytes: bytes) -> Optional[str]:
        """Ask the model which direction the puzzle should rotate."""
        if not self._load():
            return None
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(screenshot_bytes))
            prompt = (
                "What direction should this 3D object be rotated? "
                "Answer: left, right, up, or down."
            )
            enc_image = self._model.encode_image(image)  # type: ignore[union-attr] because _load returned True
            answer = self._model.answer_question(  # type: ignore[union-attr] because _load returned True
                enc_image, prompt, self._tokenizer,
            )
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            _LOGGER.error("Vision analysis error: %s", exc)
            return None
        return _direction_from_answer(answer)

    async def analyze_async(self, screenshot_bytes: bytes, *, timeout: float) -> Optional[str]:
        """Ask the configured local vision endpoint for a single safe action."""
        from ..core.vision_router import analyze_bytes

        answer = await analyze_bytes(
            screenshot_bytes,
            prompt=(
                "Inspect this puzzle and its visible instructions. Which one arrow "
                "should be clicked next? Reply with exactly one word: LEFT, RIGHT, "
                "UP, DOWN, or NONE if the correct action is unclear."
            ),
            timeout=timeout,
        )
        if not answer:
            return None
        # Ambiguous or explanatory output must never turn into a click.
        direction = answer.strip().strip(".!").lower()
        return direction if direction in {"left", "right", "up", "down"} else None


def _direction_from_answer(answer: str) -> Optional[str]:
    """Accept the single direction requested by the legacy model's prompt."""
    direction = answer.strip().strip(".!").lower()
    return direction if direction in {"left", "right", "up", "down"} else None


def parse_audio_direction(transcription: str) -> Optional[str]:
    """Parse rotation direction from an audio-challenge transcription."""
    text = transcription.casefold()
    if re.search(r"\b(?:no|not|never|avoid|without|except|cannot|dont|cant)\b|n['’]t\b", text):
        return None
    words = re.findall(
        r"\b(?:counter[\s-]*clockwise|anti[\s-]*clockwise|clockwise|"
        r"left|right|up|down|above|below|forward|backward|back)\b", text,
    )
    aliases = {
        'left': 'left', 'counterclockwise': 'left', 'anticlockwise': 'left',
        'right': 'right', 'clockwise': 'right',
        'up': 'up', 'above': 'up', 'forward': 'up',
        'down': 'down', 'below': 'down', 'backward': 'down', 'back': 'down',
    }
    directions = {aliases[re.sub(r"[\s-]", "", word)] for word in words}
    return next(iter(directions)) if len(directions) == 1 else None


__all__ = ["VisionModel", "parse_audio_direction"]
