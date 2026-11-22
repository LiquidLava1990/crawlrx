"""Multi-strategy fallback runners for the FunCaptcha solver.

Each public ``try_<strategy>`` function takes a runtime config object
(the orchestrator), a ``Page``, and a ``start_time`` and returns a
``StrategyOutcome`` describing whether the strategy succeeded
(``token``) or yielded so the caller can fall through to the next.
The orchestrator chains them in order.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from playwright.async_api import Page


from ._sentinel_shim import ErrCode, emit  # noqa: E402
from ._solver_helpers import BROAD_EXC  # noqa: E402

from ._funcaptcha_browser import (  # noqa: E402
    capture_audio_state,
    capture_puzzle,
    click_arrow,
    click_audio_button,
    click_next_audio,
    detect,
    download_audio,
    extract_token,
    get_funcaptcha_iframe,
    is_solved,
    resolve_funcaptcha_frame,
    unlink_quiet,
)
from ._funcaptcha_rotation import parse_audio_direction  # noqa: E402

_LOGGER = logging.getLogger(__name__)


@dataclass
class StrategyOutcome:
    """Per-strategy result.

    ``success`` + ``token`` -> stop and return.
    ``success`` only        -> page bypassed detection (camoufox case).
    not ``success`` + ``error_message`` -> caller falls through to next strategy.
    """
    success: bool = False
    token: str = ""
    bypassed: bool = False
    method: str = ""
    duration: float = 0.0
    error_message: str = ""


@runtime_checkable
class StrategyCtx(Protocol):
    """Minimal contract the orchestrator provides to each strategy.

    Public on purpose so callers (the orchestrator + tests) can
    isinstance-check against it.
    """
    timeout: int
    max_retries: int
    vision: Any  # _funcaptcha_rotation.VisionModel
    whisper: Any  # _funcaptcha_browser.WhisperTranscriber


def _failed(reason: str, start_time: float) -> StrategyOutcome:
    """Compose the recurring "strategy failed" outcome shape."""
    return StrategyOutcome(
        success=False, error_message=reason, duration=time.time() - start_time,
    )


def _timeout(where: str, start_time: float) -> StrategyOutcome:
    """Build the recurring timeout-during-strategy failure outcome."""
    return _failed(f"Timeout during {where}", start_time)


async def _solve_check(
    iframe_page: Any, page: Page, start_time: float, method: str,
) -> Optional[StrategyOutcome]:
    """If the challenge is solved, return the success ``StrategyOutcome``."""
    if not await is_solved(iframe_page):
        return None
    token = await extract_token(page)
    if not token:
        return None
    return StrategyOutcome(
        success=True, token=token,
        duration=time.time() - start_time, method=method,
    )


async def _run_strategy(
    label: str,
    runner: Callable[[], Awaitable[Optional[StrategyOutcome]]],
    start_time: float,
) -> StrategyOutcome:
    """Wrap one strategy: log start, swallow ImportError + BROAD_EXC."""
    _LOGGER.info("Attempting %s strategy...", label)
    try:
        result = await runner()
    except ImportError:
        _LOGGER.warning("%s not installed, skipping", label)
        return _failed(f"{label} not installed", start_time)
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("%s error: %s", label, exc)
        return _failed(f"{label} raised", start_time)
    if result is not None:
        return result
    return _failed(f"{label} strategy failed", start_time)


# -- Strategy 1: Botright --

async def _botright_attempt(
    page: Page, start_time: float,
) -> Optional[StrategyOutcome]:
    """One Botright wait pass; returns success or ``None`` to retry."""
    try:
        token_input = await page.wait_for_selector(
            "input[name='fc-token'], input[name='arkose-token'], [data-fc-token]",
            timeout=30000,
        )
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.warning("Botright wait failed: %s", exc)
        await asyncio.sleep(1)
        return None
    if not token_input:
        await asyncio.sleep(2)
        return None
    token = await token_input.get_attribute("value")
    if token and len(token) > 10:
        return StrategyOutcome(
            success=True, token=token,
            duration=time.time() - start_time, method="botright",
        )
    await asyncio.sleep(2)
    return None


async def _botright_runner(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> Optional[StrategyOutcome]:
    """Inner loop for the Botright strategy."""
    for _ in range(ctx.max_retries):
        if time.time() - start_time > ctx.timeout:
            return _timeout("Botright attempt", start_time)
        done = await _botright_attempt(page, start_time)
        if done is not None:
            return done
    return None


async def try_botright(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> StrategyOutcome:
    """Strategy 1: try Botright's built-in challenge resolution."""
    return await _run_strategy(
        "Botright", lambda: _botright_runner(ctx, page, start_time), start_time,
    )


# -- Strategy 2: configured local vision endpoint --

async def _vision_step(
    ctx: StrategyCtx, iframe_page: Any, page: Page, start_time: float, step: int,
    iframe_element: Any = None,
) -> Optional[StrategyOutcome]:
    """One rotation step. Success on solve, ``None`` to continue."""
    if time.time() - start_time > ctx.timeout:
        return _timeout("Vision AI solve", start_time)
    solved = await _solve_check(iframe_page, page, start_time, "vision_ai")
    if solved is not None:
        return solved
    # Capture the entire widget so the model sees both instructions and puzzle.
    screenshot_bytes = await capture_puzzle(iframe_page, iframe_element)
    if not screenshot_bytes:
        await asyncio.sleep(1)
        return None
    remaining = ctx.timeout - (time.time() - start_time)
    if remaining <= 0:
        return _timeout("Vision AI solve", start_time)
    direction = await ctx.vision.analyze_async(
        screenshot_bytes, timeout=min(remaining, 60.0),
    )
    if direction:
        if time.time() - start_time >= ctx.timeout:
            return _timeout("Vision AI solve", start_time)
        current_image = await capture_puzzle(iframe_page, iframe_element)
        if current_image != screenshot_bytes:
            _LOGGER.warning("FunCaptcha changed during inference; ignoring stale direction")
            return None
        if time.time() - start_time >= ctx.timeout:
            return _timeout("Vision AI solve", start_time)
        _LOGGER.info("Step %d: detected rotation direction: %s", step + 1, direction)
        await click_arrow(iframe_page, direction)
        await asyncio.sleep(1.5)
    else:
        _LOGGER.warning("Step %d: could not determine direction", step + 1)
        await asyncio.sleep(0.5)
    return None


async def try_vision_ai(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> StrategyOutcome:
    """Strategy 2: use the configured vision endpoint for puzzle actions."""
    _LOGGER.info("Attempting Vision AI strategy...")
    try:
        iframe = await get_funcaptcha_iframe(page)
        if not iframe:
            return _failed("FunCaptcha iframe not found", start_time)
        iframe_page = resolve_funcaptcha_frame(page)
        for step in range(6):
            done = await _vision_step(ctx, iframe_page, page, start_time, step, iframe)
            if done is not None:
                return done
        final = await _solve_check(iframe_page, page, start_time, "vision_ai")
        if final is not None:
            return final
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Vision AI error: %s", exc)
        return _failed("Vision AI raised", start_time)
    return _failed("Vision AI strategy failed", start_time)


# -- Strategy 3: Camoufox --

async def _camoufox_check(
    ctx: StrategyCtx, new_page: Any, start_time: float,
) -> Optional[StrategyOutcome]:
    """Single check during the Camoufox poll loop."""
    if time.time() - start_time > ctx.timeout:
        return None
    if not await detect(new_page):
        return StrategyOutcome(
            success=True, bypassed=True,
            duration=time.time() - start_time, method="camoufox",
        )
    token = await extract_token(new_page)
    if token:
        return StrategyOutcome(
            success=True, token=token,
            duration=time.time() - start_time, method="camoufox",
        )
    return None


async def _drive_camoufox_session(
    ctx: StrategyCtx, fox: Any, page: Page, start_time: float,
) -> Optional[StrategyOutcome]:
    """Run the per-step camoufox loop inside an open AsyncCamoufox context."""
    new_page = await fox.new_page()
    await new_page.goto(page.url, wait_until="networkidle")
    for _ in range(ctx.timeout // 5):
        done = await _camoufox_check(ctx, new_page, start_time)
        if done is not None:
            return done
        await asyncio.sleep(5)
    return None


async def _camoufox_runner(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> Optional[StrategyOutcome]:
    """Inner runner for the Camoufox strategy (defers import)."""
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox() as fox:
        return await _drive_camoufox_session(ctx, fox, page, start_time)


async def try_camoufox(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> StrategyOutcome:
    """Strategy 3: use Camoufox stealth browser to avoid detection."""
    return await _run_strategy(
        "Camoufox", lambda: _camoufox_runner(ctx, page, start_time), start_time,
    )


# -- Strategy 4: Audio fallback --

def _transcribe_owned_audio(whisper: Any, audio_path: str) -> Optional[str]:
    """The decoder owns its temporary file until it actually stops using it."""
    try:
        return whisper.transcribe(audio_path)
    finally:
        unlink_quiet(audio_path)


def _observe_audio_result(future: asyncio.Future[Optional[str]]) -> None:
    """Retrieve late failures even if the caller has cancelled or timed out."""
    if not future.cancelled():
        error = future.exception()
        if error is not None:
            _LOGGER.warning("FunCaptcha transcription failed: %s", type(error).__name__)


async def _audio_attempt(
    ctx: StrategyCtx, iframe_page: Any, page: Page, start_time: float,
) -> Optional[StrategyOutcome]:
    """One audio download + transcribe + arrow-click iteration."""
    if time.time() - start_time > ctx.timeout:
        return _timeout("audio fallback", start_time)
    audio_state = await capture_audio_state(iframe_page)
    if audio_state is None:
        return None
    audio_path = await download_audio(iframe_page)
    if not audio_path:
        await asyncio.sleep(1)
        return None
    remaining = ctx.timeout - (time.time() - start_time)
    if remaining <= 0:
        unlink_quiet(audio_path)
        return _timeout("audio fallback", start_time)
    # Submit before the next cancellation point. Shielding keeps queued/running
    # work alive solely to finish decoding and release its owned file.
    context = contextvars.copy_context()
    try:
        inference = asyncio.get_running_loop().run_in_executor(
            None, context.run, _transcribe_owned_audio, ctx.whisper, audio_path,
        )
    except RuntimeError:
        unlink_quiet(audio_path)
        raise
    inference.add_done_callback(_observe_audio_result)
    try:
        transcription = await asyncio.wait_for(asyncio.shield(inference), remaining)
    except asyncio.TimeoutError:
        return _timeout("audio fallback", start_time)
    if time.time() - start_time >= ctx.timeout:
        return _timeout("audio fallback", start_time)
    current_state = await capture_audio_state(iframe_page)
    if current_state is None or current_state != audio_state:
        _LOGGER.warning("FunCaptcha audio challenge changed during inference; ignoring answer")
        return None
    if time.time() - start_time >= ctx.timeout:
        return _timeout("audio fallback", start_time)
    if transcription:
        _LOGGER.info("Audio transcription: %s", transcription)
        direction = parse_audio_direction(transcription)
        if direction:
            await click_arrow(iframe_page, direction)
            current_state = await capture_audio_state(iframe_page)
            if current_state is None or current_state[1] != audio_state[1]:
                return None
            await asyncio.sleep(2)
    solved = await _solve_check(iframe_page, page, start_time, "audio_fallback")
    if solved is not None:
        return solved
    if time.time() - start_time >= ctx.timeout:
        return _timeout("audio fallback", start_time)
    if current_state is None or await capture_audio_state(iframe_page) != current_state:
        return None
    if time.time() - start_time >= ctx.timeout:
        return _timeout("audio fallback", start_time)
    await click_next_audio(iframe_page)
    await asyncio.sleep(2)
    return None


async def try_audio_fallback(
    ctx: StrategyCtx, page: Page, start_time: float,
) -> StrategyOutcome:
    """Strategy 4: use audio challenge as fallback."""
    _LOGGER.info("Attempting Audio fallback strategy...")
    try:
        iframe_page = resolve_funcaptcha_frame(page)
        await click_audio_button(iframe_page)
        for _ in range(ctx.max_retries):
            done = await _audio_attempt(ctx, iframe_page, page, start_time)
            if done is not None:
                return done
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Audio fallback error: %s", exc)
        return _failed("Audio fallback raised", start_time)
    return _failed("Audio fallback strategy failed", start_time)


__all__ = [
    "StrategyOutcome",
    "try_audio_fallback",
    "try_botright",
    "try_camoufox",
    "try_vision_ai",
]
