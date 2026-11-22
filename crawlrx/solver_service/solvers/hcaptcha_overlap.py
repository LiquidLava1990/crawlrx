"""Bounded, same-page handler for the observed square/circle X drag puzzle."""
import asyncio
import hashlib
import logging
import re
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_frame, click_control, frame_box
from .hcaptcha_overlap_geometry import overlap_plan, verify_placement

logger = logging.getLogger(__name__)
INSTRUCTION = 'please drag the x so it sits inside both shapes'


async def observe_overlap(page: Page) -> tuple[Frame, ElementHandle, dict[str, float], dict[str, float]] | None:
    candidates = []
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            host = (urlsplit(frame.url).hostname or '').lower()
            if host != 'hcaptcha.com' and not host.endswith('.hcaptcha.com'):
                continue
            element = await frame.frame_element()
            heading = frame.get_by_role('heading', level=2)
            if await element.is_visible() and await heading.count() == 1 and await heading.is_visible():
                candidates.append((frame, element, heading))
        except (PlaywrightError, ValueError):
            return None
    if len(candidates) != 1:
        return None
    frame, element, heading = candidates[0]
    text = ' '.join((await heading.inner_text()).split()).lower().rstrip('.!')
    if text != INSTRUCTION:
        return None
    box = await frame_box(frame, element)
    viewport = await page.evaluate('()=>({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,scale:visualViewport?.scale??1,dpr:devicePixelRatio})')
    if (not box or box['width'] <= 0 or box['height'] <= 0
            or box['x'] < viewport['x'] or box['y'] < viewport['y'] or box['x']+box['width'] > viewport['x']+viewport['w']
            or box['y']+box['height'] > viewport['y']+viewport['h']):
        return None
    return frame, element, box, viewport


class HcaptchaOverlapSolver:
    def __init__(self):
        self._done = False
        self._started = False
        self._rounds = 0
        self._seen = set()

    async def try_overlap(self, page: Page) -> bool:
        if self._done or self._rounds >= 3:
            return False
        try:
            observed = await observe_overlap(page)
            if observed is None:
                return False
            frame, element, box, viewport = observed
            if not self._started and not await frame.get_by_role('button', name='Skip Challenge', exact=True).is_visible():
                return False
            image = await capture_frame(page, frame, element, box)
            fingerprint = hashlib.sha256(image).digest()
            if fingerprint in self._seen:
                return False
            self._seen.add(fingerprint)
            plan = await asyncio.to_thread(overlap_plan, image)
            current = await observe_overlap(page)
            if current is None or current[0] != frame or current[2:] != (box, viewport):
                return False
            if hashlib.sha256(await capture_frame(page, frame, element, box)).digest() != fingerprint:
                return False
            self._done = True  # Never replay an interrupted drag or verification.
            x, y = plan['from']
            tx, ty = plan['to']
            await page.mouse.move(box['x']-viewport['x']+x*box['width']/520, box['y']-viewport['y']+y*box['height']/570)
            completed = False
            try:
                await page.mouse.down()
                await page.mouse.move(box['x']-viewport['x']+tx*box['width']/520, box['y']-viewport['y']+ty*box['height']/570, steps=20)
                completed = True
            finally:
                try:
                    await page.mouse.up()
                except (PlaywrightError, OSError, RuntimeError) as error:
                    if completed:
                        raise
                    logger.warning('Mouse release failed after interrupted hCaptcha overlap input: %s', type(error).__name__)
            await page.wait_for_timeout(400)
            current = await observe_overlap(page)
            if current is None or current[0] != frame or current[2:] != (box, viewport):
                return False
            after = await capture_frame(page, frame, element, box)
            await asyncio.to_thread(verify_placement, image, after)
            button = frame.get_by_role('button', name=re.compile(r'^(Next Challenge|Verify Answers)$'))
            if await button.count() != 1 or not await button.is_visible():
                return False
            is_next = (await button.get_attribute('aria-label')) == 'Next Challenge'
            await click_control(page, frame, button)
            self._started = True
            self._rounds += 1
            await page.wait_for_timeout(1200)
            if is_next:
                current = await observe_overlap(page)
                if current is not None and current[0] == frame and current[2:] == (box, viewport):
                    fresh = await capture_frame(page, frame, current[1], box)
                    if hashlib.sha256(fresh).digest() != hashlib.sha256(after).digest():
                        self._done = False
            return True
        except (PlaywrightError, ValueError, OSError, ImportError) as exc:
            logger.info('hCaptcha overlap attempt stopped: %s', type(exc).__name__)
            return False
