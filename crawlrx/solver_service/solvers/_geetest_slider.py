"""GeeTest slider image measurement and visible slider discovery."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Protocol

import cv2
import numpy as np
from playwright.async_api import Error as PlaywrightError

try:
    from ._geetest_helpers import GEETEST_SELECTORS
    from ._solver_helpers import BROAD_EXC
    from hcaptcha_viewport import capture_view, measured_box
    from ._sentinel_shim import ErrCode, emit
except ImportError:
    from ._geetest_helpers import GEETEST_SELECTORS
    from ._solver_helpers import BROAD_EXC
    from .hcaptcha_viewport import capture_view, measured_box
    from ._sentinel_shim import ErrCode, emit

_BROAD_EXC = tuple(error for error in BROAD_EXC if error is not asyncio.CancelledError)

logger = logging.getLogger(__name__)


class _StrategyHost(Protocol):
    page: Optional[Any]


def _shape_candidates(edges: Any) -> List[int]:
    """X positions of square-ish puzzle-piece-shaped contours in ``edges``."""
    contours, _ = cv2.findContours(
        edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    candidates: List[int] = []
    for contour in contours:
        x, _y, w, h = cv2.boundingRect(contour)
        aspect = w / float(h) if h > 0 else 0
        if 30 < w < 100 and 30 < h < 100 and 0.5 < aspect < 2.0:
            candidates.append(x)
    return candidates


def calculate_slide_distance(
    bg_image: np.ndarray, slice_image: Optional[np.ndarray],
) -> int:
    """Calculate the slide distance using OpenCV."""
    try:
        gray = cv2.cvtColor(bg_image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        if slice_image is not None:
            slice_gray = cv2.cvtColor(slice_image, cv2.COLOR_BGR2GRAY)
            slice_edges = cv2.Canny(slice_gray, 50, 150)
            result = cv2.matchTemplate(edges, slice_edges, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > 0.3:
                return max_loc[0]
        candidates = _shape_candidates(edges)
        if candidates:
            return int(np.median(candidates))
        return 100
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.error("Image processing error: %s", e)
        return 100


# ---------------------------------------------------------------------------
# Sliding-puzzle strategy
# ---------------------------------------------------------------------------
def _match_v4_slide_distance(
    bg: np.ndarray, piece: np.ndarray, y: int, start_x: int,
) -> Optional[int]:
    """Match the opaque piece pixels to a darkened cutout on its drag axis."""
    if (bg is None or piece is None or bg.ndim != 3 or bg.shape[2] != 3
            or piece.ndim != 3 or piece.shape[2] != 4):
        return None
    height, width = piece.shape[:2]
    if (width < 10 or height < 10 or height > bg.shape[0] or width > bg.shape[1]
            or y < 0 or y + height > bg.shape[0] or start_x < 0
            or start_x + width > bg.shape[1]):
        return None
    mask = cv2.erode((piece[:, :, 3] > 240).astype(np.uint8) * 255,
                     np.ones((5, 5), np.uint8))
    if np.count_nonzero(mask) < 64:
        return None
    scores = cv2.matchTemplate(bg, piece[:, :, :3], cv2.TM_CCORR_NORMED, mask=mask)
    row = np.nan_to_num(scores[y], nan=-1.0, posinf=-1.0, neginf=-1.0)
    row[:min(len(row), start_x + 15)] = -1
    x = int(np.argmax(row))
    if row[x] < 0.90 or x <= start_x:
        return None
    logger.info("GeeTest v4 image match confidence %.3f", row[x])
    return x - start_x


async def _v4_slide_distance(host: _StrategyHost) -> Optional[int]:
    """Read the existing browser image response and capture the unobscured background."""
    page = host.page
    try:
        bg_el = page.locator('.geetest_bg:visible').first
        slice_el = page.locator('.geetest_slice:visible').first
        if not await bg_el.count() or not await slice_el.count():
            return None
        bg_box = await measured_box(page.main_frame, bg_el)
        slice_box = await measured_box(page.main_frame, slice_el)
        if not bg_box or not slice_box:
            return None
        image_url = await page.locator('.geetest_slice_bg:visible').first.evaluate(
            """el => { const v=getComputedStyle(el).backgroundImage;
                const m=v.match(/^url\\([\"']?(.*?)[\"']?\\)$/);
                return m ? new URL(m[1], document.baseURI).href : null; }"""
        )
        responses = getattr(host, '_geetest_image_responses', [])
        response = next((r for r in reversed(responses) if r.url == image_url and r.ok), None)
        if response is None:
            logger.warning("GeeTest v4 piece image was not captured by the browser")
            return None
        if int(response.headers.get('content-length', '0') or '0') > 8 * 1024 * 1024:
            return None
        payload = await response.body()
        if len(payload) > 8 * 1024 * 1024:
            return None
        piece = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_UNCHANGED)
        if piece is None or piece.ndim != 3 or piece.shape[2] != 4:
            return None
        slice_handle = await slice_el.element_handle(timeout=5000)
        if not slice_handle:
            return None
        previous = await slice_handle.evaluate(
            "el => {const p=[el.style.getPropertyValue('visibility'),el.style.getPropertyPriority('visibility')];"
            "el.style.setProperty('visibility','hidden','important');return p;}"
        )
        try:
            screenshot = await capture_view(page, page.main_frame, bg_el, bg_box)
        finally:
            await slice_handle.evaluate(
                "(el,p) => {if(p[0]) el.style.setProperty('visibility',p[0],p[1]);"
                "else el.style.removeProperty('visibility');}", previous,
            )
        bg = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)
        if bg is None:
            return None
        bg_size = (round(bg_box['width']), round(bg_box['height']))
        piece_size = (round(slice_box['width']), round(slice_box['height']))
        if min(*bg_size, *piece_size) < 1 or max(*bg_size, *piece_size) > 4096:
            return None
        bg = cv2.resize(bg, bg_size)
        piece = cv2.resize(piece, piece_size)
        return _match_v4_slide_distance(
            bg, piece, round(slice_box['y'] - bg_box['y']),
            round(slice_box['x'] - bg_box['x']),
        )
    except (PlaywrightError, *_BROAD_EXC) as error:
        logger.warning("Could not measure GeeTest v4 slider: %s", error)
        return None


async def _find_slider(host: _StrategyHost) -> Optional[Any]:
    """Find the slider element."""
    if not host.page:
        return None
    selectors = ((GEETEST_SELECTORS["v4_slider"], GEETEST_SELECTORS["v3_slider"])
                 if getattr(host, '_geetest_version', 3) == 4 else
                 (GEETEST_SELECTORS["v3_slider"], GEETEST_SELECTORS["v4_slider"]))
    for selector in selectors:
        try:
            loc = host.page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            continue
    return None


