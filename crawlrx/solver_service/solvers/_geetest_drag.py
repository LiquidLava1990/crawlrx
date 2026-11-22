"""Human-like drag motion for the GeeTest sliding puzzle.

Pulled out of ``_geetest_strategies.py`` so each sibling stays well below
the 500-LOC ceiling. The orchestrator owns ``self.page`` (passed via
``host``); this module is pure motion + timing.
"""
from __future__ import annotations

import asyncio
import math
import logging
import random
from typing import Any, List, Optional, Protocol, TYPE_CHECKING
from playwright.async_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Locator

from ._geetest_helpers import generate_bezier_points

logger = logging.getLogger(__name__)


class _DragHost(Protocol):
    """Subset of ``GeeTestSolver`` the drag helpers reach into."""

    page: Optional[Any]


def _delay_for(i: int, count: int) -> float:
    """Per-step jitter delay — slower at endpoints, faster in the middle."""
    if i < count * 0.2:
        return random.uniform(0.02, 0.08)
    if i > count * 0.8:
        return random.uniform(0.03, 0.06)
    return random.uniform(0.01, 0.05)


async def _drag_along_points(
    host: _DragHost,
    start_x: float, start_y: float,
    points: List[float],
) -> None:
    """Move along ``points`` (deltas from ``start_x``) with jittered timing."""
    count = len(points)
    for i, dx in enumerate(points):
        target_x = start_x + dx
        target_y = start_y + random.uniform(-3, 3)
        if i > count * 0.7:
            target_y += random.uniform(-1, 1)
        await host.page.mouse.move(target_x, target_y)
        await asyncio.sleep(_delay_for(i, count))


async def human_like_drag(
    host: _DragHost,
    slider: Optional["ElementHandle | Locator"],
    distance: int,
) -> None:
    """Simulate human-like drag with bezier curves and wobble."""
    if not slider or not host.page:
        return
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError('Invalid slider distance')
    if not await slider.is_visible() or not await slider.is_enabled():
        raise ValueError('Slider unavailable')
    box = await slider.bounding_box()
    if not box:
        return
    if not all(math.isfinite(value) for value in box.values()) or min(box['width'], box['height']) <= 0:
        raise ValueError('Invalid slider geometry')
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    overshoot = random.uniform(2, 8)
    points = generate_bezier_points(
        0, distance + overshoot, num_points=random.randint(15, 25),
    )
    await host.page.mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.1, 0.3))
    if await slider.bounding_box() != box:
        raise ValueError('Slider moved before drag')
    clear = await slider.evaluate("""el => {const r=el.getBoundingClientRect();
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return !!hit&&(hit===el||el.contains(hit));}""")
    if not clear or not await slider.is_visible() or not await slider.is_enabled():
        raise ValueError('Slider covered or unavailable')
    completed = False
    try:
        await host.page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await _drag_along_points(host, start_x, start_y, points)
        if overshoot > 0:
            await host.page.mouse.move(
                start_x + distance, start_y + random.uniform(-2, 2),
            )
            await asyncio.sleep(random.uniform(0.05, 0.1))
        completed = True
    finally:
        try:
            await host.page.mouse.up()
        except (PlaywrightError, OSError, RuntimeError) as error:
            if completed:
                raise
            logger.warning('Mouse release failed after interrupted GeeTest input: %s', type(error).__name__)


__all__ = ["human_like_drag"]
