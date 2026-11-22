"""Challenge-solving strategies for the GeeTest solver.

Owns the three browser-driven solve paths (sliding puzzle, icon click,
space verification) plus the shared helpers (puzzle-image capture,
slide-distance calculation, human-like drag, token extraction). The
OpenCV fallback lives in ``_geetest_opencv.py``; the orchestrator owns
``self.page`` and is passed in via ``host``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol, Tuple

import cv2
import numpy as np
from playwright.async_api import Error as PlaywrightError

from ._geetest_drag import human_like_drag
from ._geetest_helpers import ( GEETEST_SELECTORS, GEETEST_TYPE_PROBES, TOKEN_JS, )
from ._solver_helpers import BROAD_EXC as _BROAD_EXC

# Cancellation must reach the router; it is not a recoverable browser error.
_BROAD_EXC = tuple(error for error in _BROAD_EXC if error is not asyncio.CancelledError)

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:  # pragma: no cover
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode = _E()  # type: ignore[assignment]  because sentinel ErrCode is unavailable; _E() shim returns ERR_UNKNOWN for any attr

    def emit(*a: object, **kw: object) -> None:  # noqa: ANN001
        return None


from ..models import TaskResult  # noqa: E402


from ._geetest_slider import calculate_slide_distance, _match_v4_slide_distance, _v4_slide_distance, _find_slider


logger = logging.getLogger(__name__)


class _StrategyHost(Protocol):
    """Subset of ``GeeTestSolver`` the strategy code reaches into."""

    page: Optional[Any]


class _AttemptHost(Protocol):
    """Subset of ``GeeTestSolver`` the top-level attempt loop reaches into.

    Extends ``_StrategyHost`` with the orchestration hooks ``attempt_solve``
    drives: timeout-bounded solve + per-attempt cleanup.
    """

    max_retries: int

    async def _solve_with_timeout(
        self, gt: str, challenge: Optional[str], pageurl: str,
    ) -> TaskResult: ...

    async def _cleanup(self) -> None: ...


@dataclass(frozen=True)
class AttemptCtx:
    """Per-attempt input bundle for ``attempt_solve``.

    Grouping the params here keeps the call site below the 5-positional
    threshold without resorting to ``**kwargs``.
    """

    gt: str
    challenge: Optional[str]
    pageurl: str
    attempt: int
    start_time: float


# ---------------------------------------------------------------------------
# Captcha-type detection
# ---------------------------------------------------------------------------
async def _probe_once(host: _StrategyHost) -> Optional[str]:
    """Single pass over the known selectors; ``None`` means no match."""
    try:
        for sel_key, label in GEETEST_TYPE_PROBES:
            locator = host.page.locator(GEETEST_SELECTORS[sel_key])
            if await locator.count() > 0 and await locator.first.is_visible():
                return label
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
    return None


async def detect_captcha_type(host: _StrategyHost) -> str:
    """Detect the type of GeeTest challenge."""
    if not host.page:
        return "slider"
    for _ in range(10):
        hit = await _probe_once(host)
        if hit:
            return hit
        await asyncio.sleep(0.5)
    return "slider"


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
async def _evaluate_token_js(host: _StrategyHost) -> Optional[str]:
    """Evaluate the canonical token-extractor JS, swallowing recoverable errors."""
    try:
        token = await host.page.evaluate(TOKEN_JS, {
            'version': getattr(host, '_geetest_version', 3),
            'captchaId': getattr(host, '_geetest_expected_id', None),
        })
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return None
    if not isinstance(token, dict):
        return None
    fields = (("captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output")
              if getattr(host, '_geetest_version', 3) == 4 else ("challenge", "validate", "seccode"))
    if any(not isinstance(token.get(name), str) or not token[name].strip() for name in fields):
        return None
    return json.dumps({name: token[name] for name in fields}, separators=(",", ":"))


async def _extract_token_once(host: _StrategyHost) -> Optional[str]:
    """One pass at fetching the validation token; ``None`` if nothing yet."""
    return await _evaluate_token_js(host)


async def extract_token(host: _StrategyHost) -> Optional[str]:
    """Extract the validation token from the page."""
    if not host.page:
        return None
    for _ in range(5):
        token = await _extract_token_once(host)
        if token:
            return token
        await asyncio.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Image capture + slide distance
# ---------------------------------------------------------------------------
async def _show_canvases(host: _StrategyHost) -> None:
    """Force-display every known puzzle canvas. Best-effort, no raise."""
    selectors = (
        "canvas.geetest_canvas_bg",
        "canvas.geetest_canvas_slice",
        ".geetest_box canvas",
        ".gt_box canvas",
    )
    for selector in selectors:
        try:
            canvas = host.page.locator(selector).first
            if await canvas.count() > 0:
                await canvas.evaluate("el => el.style.display = 'block'")
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _capture_box_pair(
    host: _StrategyHost,
) -> "Optional[Tuple[Optional[Any], Optional[Any]]]":
    """Capture (bg, slice) from the puzzle box; ``None`` if no box found."""
    puzzle_box = host.page.locator(
        ".geetest_box, .gt_box, .geetest_widget",
    ).first
    if await puzzle_box.count() == 0:
        return None
    screenshot = await puzzle_box.screenshot()
    bg_image = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)
    slice_el = host.page.locator(
        ".geetest_slice, .gt_slice, img[src*='slice'], img[src*='puzzle']",
    ).first
    slice_image: Optional[Any] = None
    if await slice_el.count() > 0:
        slice_screenshot = await slice_el.screenshot()
        slice_image = cv2.imdecode(
            np.frombuffer(slice_screenshot, np.uint8), cv2.IMREAD_COLOR,
        )
    return bg_image, slice_image


async def capture_puzzle_images(
    host: _StrategyHost,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Capture background and slice images using screenshots."""
    if not host.page:
        return None, None
    try:
        await _show_canvases(host)
        await asyncio.sleep(0.5)
        pair = await _capture_box_pair(host)
        if pair is not None:
            return pair
        full_screenshot = await host.page.screenshot()
        bg_image = cv2.imdecode(
            np.frombuffer(full_screenshot, np.uint8), cv2.IMREAD_COLOR,
        )
        return bg_image, None
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.error("Failed to capture images: %s", e)
        return None, None


async def solve_sliding_puzzle(host: _StrategyHost) -> Optional[str]:
    """Solve the sliding puzzle challenge."""
    if not host.page:
        return None
    slider = await _find_slider(host)
    if not slider:
        logger.error("Slider not found")
        return None
    await asyncio.sleep(random.uniform(0.5, 1.0))
    if getattr(host, '_geetest_version', 3) == 4:
        slide_distance = await _v4_slide_distance(host)
        if slide_distance is None:
            logger.warning("GeeTest v4 slider image could not be matched")
            return None
        logger.info("Calculated slide distance: %spx", slide_distance)
        await human_like_drag(host, slider, slide_distance)
        await asyncio.sleep(random.uniform(1.5, 2.5))
        return await extract_token(host)
    bg_image, slice_image = await capture_puzzle_images(host)
    if bg_image is None:
        logger.error("Failed to capture puzzle images")
        return None
    slide_distance = calculate_slide_distance(bg_image, slice_image)
    logger.info("Calculated slide distance: %spx", slide_distance)
    await human_like_drag(host, slider, slide_distance)
    await asyncio.sleep(random.uniform(1.5, 2.5))
    return await extract_token(host)


# ---------------------------------------------------------------------------
# Icon-click strategy
# ---------------------------------------------------------------------------
async def _detect_target_icons(image: bytes, instruction: str, count: int) -> List[int]:
    """Ask the configured local model for numbered icons; reject ambiguous output."""
    from ..core.vision_router import analyze_bytes

    answer = await analyze_bytes(
        image,
        prompt=(
            "Read the instruction and select the numbered icons that satisfy it, "
            "in the requested order. Number labels are zero-based. "
            'Return only JSON like {"indices":[0,2]}. If uncertain, return '
            '{"indices":[]}. Do not invent icons. Instruction: ' + instruction[:500]
        ),
        timeout=35,
    )
    try:
        raw = (answer or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        parsed = json.loads(fenced.group(1) if fenced else raw)
        indices = parsed.get("indices") if isinstance(parsed, dict) else None
        if not isinstance(indices, list) or not indices or len(indices) > count:
            return []
        if any(type(i) is not int or i < 0 or i >= count for i in indices):
            return []
        if len(set(indices)) != len(indices):
            return []
        return indices
    except (ValueError, TypeError):
        return []


async def _click_icon_at(host: _StrategyHost, icon: Any) -> None:
    """Click ``icon`` with a small randomized offset."""
    box = await icon.bounding_box()
    if not box:
        return
    await host.page.mouse.click(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
    )


async def _click_target_icons(
    host: _StrategyHost, icons: list, target_idxs: List[int],
) -> None:
    """Click each target icon at a small jitter offset."""
    for idx in target_idxs:
        if idx >= len(icons):
            continue
        try:
            await _click_icon_at(host, icons[idx])
            await asyncio.sleep(random.uniform(0.3, 0.7))
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _maybe_log_instruction(host: _StrategyHost) -> None:
    """Log the icon-challenge instruction text if it is on the page."""
    instruction = host.page.locator(
        ".geetest_tip, .geetest_text, .gt_prompt",
    ).first
    if await instruction.count() > 0:
        text = await instruction.inner_text()
        logger.info("Icon challenge instruction: %s", text)


async def solve_icon_challenge(host: _StrategyHost) -> Optional[str]:
    """Solve visible icon challenges with the configured local vision model."""
    if not host.page:
        return None
    logger.info("Solving icon challenge")
    await asyncio.sleep(random.uniform(1, 2))
    instruction_locator = host.page.locator(".geetest_tip, .geetest_text, .gt_prompt").first
    if not await instruction_locator.count():
        return None
    instruction = (await instruction_locator.inner_text()).strip()
    if not instruction:
        return None
    icons = await host.page.locator(
        ".geetest_item, .geetest_icons_item, [class*='icon-item']",
    ).all()
    if not icons:
        return await extract_token(host)
    if len(icons) > 16:
        return None
    boxes = [await icon.bounding_box() for icon in icons]
    if any(not box or box["width"] < 1 or box["height"] < 1 for box in boxes):
        return None
    page_url = host.page.url
    screenshot = await host.page.screenshot()
    image = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    viewport = await host.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    sx, sy = image.shape[1] / viewport["width"], image.shape[0] / viewport["height"]
    for idx, box in enumerate(boxes):
        x, y = int(box["x"] * sx), int(box["y"] * sy)
        right = int((box["x"] + box["width"]) * sx)
        bottom = int((box["y"] + box["height"]) * sy)
        if x < 0 or y < 0 or right > image.shape[1] or bottom > image.shape[0]:
            return None
        cv2.rectangle(image, (x, y), (right, bottom), (0, 0, 255), 2)
        cv2.rectangle(image, (x, y), (min(right, x + 22), min(bottom, y + 20)), (0, 0, 0), -1)
        cv2.putText(image, str(idx), (x + 2, y + 15), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 255), 1)
    encoded, buffer = cv2.imencode(".png", image)
    if not encoded:
        return None
    target_idxs = await _detect_target_icons(buffer.tobytes(), instruction, len(icons))
    if not target_idxs or host.page.url != page_url:
        return None
    current_boxes = [await icon.bounding_box() for icon in icons]
    if any(not current or any(abs(current[key] - original[key]) > 2 for key in ("x", "y", "width", "height"))
           for original, current in zip(boxes, current_boxes)):
        return None
    await _click_target_icons(host, icons, target_idxs)
    await asyncio.sleep(random.uniform(1, 2))
    confirm_btn = host.page.locator(
        ".geetest_commit, .geetest_submit, .gt_submit",
    ).first
    if await confirm_btn.count() > 0:
        await confirm_btn.click()
        await asyncio.sleep(random.uniform(1, 2))
    return await extract_token(host)


# ---------------------------------------------------------------------------
# Space-verification strategy
# ---------------------------------------------------------------------------
async def solve_space_verification(host: _StrategyHost) -> Optional[str]:
    """Solve space verification challenge (similar to sliding)."""
    if not host.page:
        return None
    logger.info("Solving space verification challenge")
    slider = await _find_slider(host)
    if slider:
        bg_image, slice_image = await capture_puzzle_images(host)
        if bg_image is not None:
            distance = calculate_slide_distance(bg_image, slice_image)
            await human_like_drag(host, slider, distance)
    await asyncio.sleep(random.uniform(1.5, 2.5))
    return await extract_token(host)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
async def dispatch_challenge(
    host: _StrategyHost, captcha_type: str,
) -> Optional[str]:
    """Dispatch the per-challenge solver. Returns the validation token."""
    if captcha_type == "icon":
        return await solve_icon_challenge(host)
    if captcha_type == "space":
        return await solve_space_verification(host)
    return await solve_sliding_puzzle(host)


# ---------------------------------------------------------------------------
# Top-level attempt loop
# ---------------------------------------------------------------------------
async def _run_attempt_inner(host: _AttemptHost, ctx: AttemptCtx) -> TaskResult:
    """Run the timed solve, then stamp the result and clean up."""
    result = await host._solve_with_timeout(ctx.gt, ctx.challenge, ctx.pageurl)
    solve_time_ms = int((time.time() - ctx.start_time) * 1000)
    result.solved_at = datetime.now(tz=timezone.utc)
    result.solve_time_ms = solve_time_ms
    await host._cleanup()
    return result


async def _try_run_attempt(
    host: _AttemptHost, ctx: AttemptCtx,
) -> "Tuple[Optional[TaskResult], Optional[str]]":
    """Run one attempt, returning (result, None) on success or (None, err)."""
    try:
        logger.info(
            "GeeTest solve attempt %s/%s for gt=%s",
            ctx.attempt + 1, host.max_retries, ctx.gt,
        )
        result = await _run_attempt_inner(host, ctx)
        return result, None
    except asyncio.TimeoutError:
        emit(ErrCode.TIMEOUT_ERROR, "unhandled")
        err = f"Timeout on attempt {ctx.attempt + 1}"
        logger.warning(err)
        return None, err  # explicit recovery: TimeoutError surfaced as last_error
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        err = f"Error on attempt {ctx.attempt + 1}: {str(e)}"
        logger.error(err)
        return None, err  # explicit recovery: broad failure surfaced as last_error


async def attempt_solve(
    host: _AttemptHost, ctx: AttemptCtx,
) -> "Tuple[Optional[TaskResult], Optional[str]]":
    """One solve attempt. Returns (result, last_error) — exactly one is non-None."""
    result, err = await _try_run_attempt(host, ctx)
    if result is not None:
        return result, None
    await host._cleanup()
    await asyncio.sleep(random.uniform(1, 2))
    return None, err


__all__ = [
    "AttemptCtx",
    "detect_captcha_type",
    "extract_token",
    "capture_puzzle_images",
    "calculate_slide_distance",
    "human_like_drag",
    "solve_sliding_puzzle",
    "solve_icon_challenge",
    "solve_space_verification",
    "dispatch_challenge",
    "attempt_solve",
]
