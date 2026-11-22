"""OpenCV-fallback solver for the GeeTest challenge.

Used when the CDP browser path is unavailable; fetches puzzle images from
the GeeTest public API and runs template-matching to estimate the slide
distance. Pure compute + httpx — no Playwright dependency.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Tuple

import cv2
import numpy as np

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


logger = logging.getLogger(__name__)


def _opencv_failure(message: str) -> TaskResult:
    """Compose the recurring ``ERROR_CAPTCHA_UNSOLVABLE`` failure."""
    return TaskResult(
        success=False,
        error_code="ERROR_CAPTCHA_UNSOLVABLE",
        error_message=message,
    )


async def _resolve_challenge_token(
    client: Any, gt: str, challenge: Optional[str],
) -> Optional[str]:
    """Return a usable challenge token; pulls one from register.php if needed."""
    if challenge:
        return challenge
    register_url = f"https://api.geetest.com/register.php?gt={gt}"
    resp = await client.get(register_url)
    if resp.status_code != 200:
        return None
    return (resp.json() or {}).get("challenge", "") or None


async def _fetch_puzzle_pair(
    client: Any, api_url: str,
) -> "Tuple[Optional[Any], Optional[Any]]":
    """Return ``(bg_image, slice_image)`` or ``(None, reason)`` on failure."""
    resp = await client.get(api_url)
    if resp.status_code != 200:
        return None, "Failed to fetch challenge from GeeTest API"
    challenge_data = resp.json() or {}
    bg_url = challenge_data.get("bg", "")
    slice_url = challenge_data.get("slice", "") or challenge_data.get("fullbg", "")
    if not bg_url or not slice_url:
        return None, "Missing puzzle images in challenge data"
    bg_resp = await client.get(bg_url)
    slice_resp = await client.get(slice_url)
    if bg_resp.status_code != 200 or slice_resp.status_code != 200:
        return None, "Failed to download puzzle images"
    bg_image = cv2.imdecode(
        np.frombuffer(bg_resp.content, np.uint8), cv2.IMREAD_COLOR,
    )
    slice_image = cv2.imdecode(
        np.frombuffer(slice_resp.content, np.uint8), cv2.IMREAD_COLOR,
    )
    if bg_image is None or slice_image is None:
        return None, "Failed to decode puzzle images"
    return bg_image, slice_image


def _resize_slice_for_match(slice_gray: Any) -> Any:
    """Down-scale the slice template if larger than match dimensions."""
    h, w = slice_gray.shape
    if w > 100 or h > 100:
        scale = 50 / max(h, w)
        return cv2.resize(slice_gray, None, fx=scale, fy=scale)
    return slice_gray


def _contour_candidate_xs(bg_gray: Any) -> List[int]:
    """X positions of square-ish puzzle-piece-shaped contours."""
    bg_edges = cv2.Canny(bg_gray, 50, 150)
    contours, _ = cv2.findContours(
        bg_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    positions: List[int] = []
    for contour in contours:
        x, _y, w, h = cv2.boundingRect(contour)
        if not (20 < w < 80 and 20 < h < 80):
            continue
        aspect = w / float(h) if h > 0 else 0
        if not (0.5 < aspect < 2.0):
            continue
        if x > bg_gray.shape[1] * 0.2:
            positions.append(x)
    return positions


def _calculate_slide_distance_cv(
    bg_image: np.ndarray, slice_image: np.ndarray,
) -> int:
    """Calculate slide distance using OpenCV template matching."""
    try:
        bg_gray = cv2.cvtColor(bg_image, cv2.COLOR_BGR2GRAY)
        slice_gray = cv2.cvtColor(slice_image, cv2.COLOR_BGR2GRAY)
        slice_gray = _resize_slice_for_match(slice_gray)
        result = cv2.matchTemplate(bg_gray, slice_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        logger.debug("Template match confidence: %.3f", max_val)
        if max_val > 0.3:
            return max_loc[0]
        candidates = _contour_candidate_xs(bg_gray)
        if candidates:
            return int(np.median(candidates))
        return int(bg_gray.shape[1] * 0.3)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.error("CV calculation error: %s", e)
        return 100


async def solve_with_opencv(
    gt: str, challenge: Optional[str],
) -> TaskResult:
    """A distance estimate is not a server-verified GeeTest response."""
    return _opencv_failure(
        "OpenCV alone cannot submit or verify a GeeTest challenge; "
        "a working browser and verified server response are required",
    )


__all__ = ["solve_with_opencv"]
