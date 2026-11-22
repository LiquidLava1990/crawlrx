"""Lazy, thread-safe speech backend for the FunCaptcha audio strategy."""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

try:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC
except ImportError:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC

_LOGGER = logging.getLogger(__name__)


class WhisperTranscriber:
    """Lazy faster-whisper wrapper for the audio fallback strategy."""

    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        self._load_lock = threading.Lock()

    def _check_cuda(self) -> bool:
        """Check if CUDA is available."""
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def _load(self) -> bool:
        """Load faster-whisper; ``True`` on success, ``False`` otherwise."""
        with self._load_lock:
            return self._load_locked()

    def _load_locked(self) -> bool:
        """Initialize once when simultaneous solves share this transcriber."""
        if self._model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return False
        try:
            device = "cuda" if self._check_cuda() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            bundled_model = os.environ.get("CRAWLRX_WHISPER_MODEL_DIR")
            self._model = WhisperModel(
                bundled_model or "base", device=device, compute_type=compute_type,
                local_files_only=bool(bundled_model),
            )
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            _LOGGER.error("Failed to load faster-whisper: %s", exc)
            return False
        return True

    def transcribe(self, audio_path: str) -> Optional[str]:
        """Transcribe ``audio_path``; ``None`` if backend or transcription fails."""
        if not self._load():
            return None
        model = self._model
        if model is None:
            return None
        try:
            segments, _ = model.transcribe(
                audio_path, language="en",
            )
            return " ".join(segment.text for segment in segments).strip()
        except BROAD_EXC as exc:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
            _LOGGER.error("Transcription error: %s", exc)
            return None

