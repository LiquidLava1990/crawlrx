"""Speech-to-text adapters for the reCAPTCHA v2 audio solver.

Splits the Whisper / faster-whisper bootstrap and transcription off the
``recaptcha_audio_solver`` orchestrator (cc.arch.god_module_imports
sibling). The orchestrator stays thin and free of ML imports; this
module owns the lazy model load, the dual-backend transcribe call, and
post-processing of the raw transcript text.

Design rules followed:
- Module-level cache prevents re-loading the model on every solve.
- Errors raise ``RuntimeError`` with an actionable install hint.
- No state stored on caller objects; pure functions + module-level cache.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import tempfile
import threading
from typing import Optional


from ._sentinel_shim import ErrCode, emit  # noqa: E402
from ._solver_helpers import BROAD_EXC  # noqa: E402


# Module-level cache so we load the model once per process.
_WHISPER_MODEL: object = None
_WHISPER_TYPE: Optional[str] = None  # "faster" | "standard"
_INIT_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


def _try_faster_whisper() -> bool:
    """Load faster-whisper (CTranslate2 backend, ~4x faster). True on success."""
    global _WHISPER_MODEL, _WHISPER_TYPE
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return False
    bundled_model = os.environ.get("CRAWLRX_WHISPER_MODEL_DIR")
    model_path = os.path.join(tempfile.gettempdir(), "whisper_models", "base")
    _WHISPER_MODEL = WhisperModel(
        bundled_model or "base",
        device="cpu",
        compute_type="int8",
        download_root=model_path,
    )
    _WHISPER_TYPE = "faster"
    print("[ReCaptchaAudioSolver] Using faster-whisper (CTranslate2)")
    return True


def _try_openai_whisper() -> bool:
    """Fallback to standard openai-whisper. True on success."""
    global _WHISPER_MODEL, _WHISPER_TYPE
    try:
        import whisper
    except ImportError:
        return False
    model_path = os.path.join(tempfile.gettempdir(), "whisper_models")
    os.makedirs(model_path, exist_ok=True)
    _WHISPER_MODEL = whisper.load_model("base", download_root=model_path)
    _WHISPER_TYPE = "standard"
    print("[ReCaptchaAudioSolver] Using openai-whisper")
    return True


def init_whisper() -> None:
    """Initialise the Whisper model once and cache it for reuse."""
    with _INIT_LOCK:
        if _WHISPER_MODEL is not None:
            return
        if _try_faster_whisper():
            return
        if _try_openai_whisper():
            return
        raise RuntimeError(
            "Neither faster-whisper nor openai-whisper installed. "
            "Install with: pip install faster-whisper or pip install openai-whisper"
        )


def _transcribe_with_faster(audio_path: str) -> str:
    """Run a faster-whisper pass; ``init_whisper`` ensures model is non-None."""
    segments, _ = _WHISPER_MODEL.transcribe(  # type: ignore[union-attr] because init_whisper sets the model non-None before this helper runs
        audio_path,
        beam_size=5,
        language="en",
        task="transcribe",
    )
    return " ".join(segment.text for segment in segments)


def _transcribe_with_standard(audio_path: str) -> str:
    """Run openai-whisper pass; ``init_whisper`` ensures model is non-None."""
    result = _WHISPER_MODEL.transcribe(  # type: ignore[union-attr] because init_whisper sets the model non-None before this helper runs
        audio_path,
        language="en",
        task="transcribe",
        fp16=False,
    )
    return result.get("text", "")


def _cleanup_audio(audio_path: str) -> None:
    """Best-effort delete of the downloaded mp3."""
    try:
        os.remove(audio_path)
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


def _normalise_transcript(text: str) -> str:
    """Strip Whisper artefacts and normalise the response text."""
    text = text.strip().lower()
    text = text.replace("the answer is", "").replace("the code is", "")
    return text.strip()


def transcribe_audio(audio_path: str) -> str:
    """Transcribe the audio at ``audio_path`` and clean up the temp file."""
    try:
        init_whisper()
        if _WHISPER_TYPE == "faster":
            text = _transcribe_with_faster(audio_path)
        else:
            text = _transcribe_with_standard(audio_path)
        return _normalise_transcript(text)
    finally:
        _cleanup_audio(audio_path)


def _observe_transcription_result(future: asyncio.Future[str]) -> None:
    """Consume errors that finish after the caller has been interrupted."""
    if not future.cancelled():
        error = future.exception()
        if error is not None:
            _LOGGER.warning("reCAPTCHA transcription failed: %s", type(error).__name__)


async def transcribe_audio_async(audio_path: str) -> str:
    """Keep decoding responsive; its worker owns cleanup through cancellation."""
    context = contextvars.copy_context()
    try:
        future = asyncio.get_running_loop().run_in_executor(
            None, context.run, transcribe_audio, audio_path,
        )
    except RuntimeError:
        _cleanup_audio(audio_path)
        raise
    future.add_done_callback(_observe_transcription_result)
    return await asyncio.shield(future)


__all__ = ["init_whisper", "transcribe_audio", "transcribe_audio_async"]
