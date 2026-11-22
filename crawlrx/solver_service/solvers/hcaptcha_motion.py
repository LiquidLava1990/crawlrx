"""Bounded local solver for the observed four-island hCaptcha animation."""
import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_frame, click_control, frame_box
from .hcaptcha_motion_detector import detect, scene_changed

logger = logging.getLogger(__name__)
INSTRUCTIONS = {
    'click the island the moving animal did not visit',
    'click the island the moving animal never visited',
    'select the island the moving animal never went to',
    'select the island the hopping animal did not land on',
}


async def observe_motion(page: Page) -> tuple[Frame, ElementHandle, dict[str, float], dict[str, float], str] | None:
    """Accept exactly one visible provider frame with a supported heading."""
    challenges = []
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            host = (urlsplit(frame.url).hostname or '').lower()
            if host != 'hcaptcha.com' and not host.endswith('.hcaptcha.com'):
                continue
            element = await frame.frame_element()
            heading = frame.get_by_role('heading', level=2).first
            if await element.is_visible() and await heading.is_visible():
                challenges.append((frame, element, heading))
        except (PlaywrightError, ValueError):
            return None
    if len(challenges) != 1:
        return None
    frame, element, heading = challenges[0]
    try:
        text = ' '.join((await heading.inner_text()).split()).lower().rstrip('.!')
        if text not in INSTRUCTIONS:
            return None
        box = await frame_box(frame, element)
        viewport = await page.evaluate('() => ({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,scale:visualViewport?.scale??1,dpr:devicePixelRatio})')
        if (not box or box['width'] <= 0 or box['height'] <= 0
                or box['x'] < viewport['x'] or box['y'] < viewport['y'] or box['x'] + box['width'] > viewport['x']+viewport['w']
                or box['y'] + box['height'] > viewport['y']+viewport['h']):
            return None
        return frame, element, box, viewport, text
    except (PlaywrightError, ValueError):
        return None


class HcaptchaMotionSolver:
    def __init__(self):
        self._started = False
        self._done = False
        self._rounds = 0

    async def try_motion(self, page: Page) -> bool:
        if self._done or self._rounds >= 3:
            return False
        observed = await observe_motion(page)
        if observed is None:
            return False
        frame, element, box, viewport, text = observed
        try:
            skip = frame.get_by_role('button', name=re.compile(r'^Skip Challenge'))
            if not self._started and not await skip.first.is_visible():
                return False
            images, timestamps = [], []
            for _ in range(48):
                started = time.monotonic() * 1000
                timestamps.append(started)
                images.append(await capture_frame(page, frame, element, box))
                remaining = 250 - (time.monotonic() * 1000 - started)
                if remaining > 0:
                    await page.wait_for_timeout(remaining)
            current = await observe_motion(page)
            if current is None or current[0] != frame or current[2:] != (box, viewport, text):
                return False
            verification = await capture_frame(page, frame, current[1], box)
            result = await asyncio.to_thread(detect, images, timestamps, verification)
            x, y = result['point']
            if not (0 < x < 1 and 0 < y < 1):
                return False
            current = await observe_motion(page)
            if current is None or current[0] != frame or current[2:] != (box, viewport, text):
                return False
            # Once input begins, an interrupted attempt must never be replayed.
            self._done = True
            await page.mouse.click(box['x'] - viewport['x'] + x * box['width'],
                                   box['y'] - viewport['y'] + y * box['height'])
            next_page = frame.get_by_role('button', name=re.compile(r'^Next Challenge'))
            last_page = frame.get_by_role('button', name=re.compile(r'^Verify Answers'))
            if await next_page.first.is_visible():
                await click_control(page, frame, next_page.first)
                self._started = True
                has_next = True
            elif await last_page.first.is_visible():
                await click_control(page, frame, last_page.first)
                has_next = False
            else:
                return False
            self._rounds += 1
            await page.wait_for_timeout(1200)
            if has_next:
                current = await observe_motion(page)
                if current is not None and current[0] == frame and current[2:4] == (box, viewport):
                    image = await capture_frame(page, frame, current[1], box)
                    if await asyncio.to_thread(scene_changed, images, image):
                        self._done = False
            return True
        except (PlaywrightError, ValueError, OSError, ImportError) as exc:
            logger.info('hCaptcha motion attempt failed: %s', type(exc).__name__)
            return False
