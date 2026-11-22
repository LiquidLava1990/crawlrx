"""Solve observed hCaptcha object-selection puzzles in their owning page."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from urllib.parse import urlsplit

from playwright.async_api import Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_frame, click_control, frame_box
from ..core.vision_router import analyze_bytes
from .hcaptcha_counts import PROMPT as COUNT_PROMPT, is_count_instruction, parse_counts

logger = logging.getLogger(__name__)
PROMPT = ('Identify every object in the captcha puzzle and whether it matches the instruction. '
          'Output only a compact valid JSON array of objects with description (at most two words), '
          'match (boolean), and center [x,y] normalized from 0 to 1000 relative to the entire image. No markdown.')


def parse_objects(text: str | None) -> list[tuple[float, float]] | None:
    if not isinstance(text, str):
        return None
    match = re.search(r'\[[\s\S]*\]', text)
    if not match:
        return None
    try:
        objects = json.loads(match.group())
    except (ValueError, TypeError):
        return None
    if not isinstance(objects, list) or not 2 <= len(objects) <= 20:
        return None
    points = []
    for obj in objects:
        if not isinstance(obj, dict):
            return None
        name, selected, center = obj.get('description'), obj.get('match'), obj.get('center')
        if (not isinstance(name, str) or not name.strip() or type(selected) is not bool
                or not isinstance(center, list) or len(center) != 2
                or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n < 1000 for n in center)):
            return None
        if selected:
            if re.search(r'\b(uncertain|unknown|maybe|possibly|unsure)\b', name, re.I):
                return None
            x, y = (n / 1000 for n in center)
            if not 0.03 <= x <= 0.97 or not 0.22 <= y <= 0.84:
                return None
            if any(math.hypot(x - a, y - b) < 0.02 for a, b in points):
                return None
            points.append((x, y))
    return points if 1 <= len(points) <= 9 else None


def supported_instruction(heading: str) -> bool:
    return is_count_instruction(heading) or bool(re.match(r'^(find|select|click on (all|every|each))\b', heading, re.I)
                and not re.search(r'\b(rings?|drag|rotate|rotation|connect|link|lines?|order|sequence)\b', heading, re.I))


async def _object_page(page):
    challenges = []
    for frame in getattr(page, 'frames', ()):
        if frame == page.main_frame:
            continue
        try:
            element = await frame.frame_element()
            host = (urlsplit(frame.url).hostname or '').lower()
            if not (host == 'hcaptcha.com' or host.endswith('.hcaptcha.com')):
                continue
            if not await element.is_visible():
                continue
            heading = frame.get_by_role('heading', level=2).first
            if not await heading.is_visible():
                continue
            challenges.append((frame, element, heading))
        except (PlaywrightError, ValueError):
            return None
    if len(challenges) != 1:
        return None
    frame, element, heading = challenges[0]
    try:
        refresh = frame.get_by_role('button', name='Refresh Challenge.', exact=True)
        if not await refresh.is_visible():
            return None
        text = await heading.inner_text()
        if not supported_instruction(text):
            return None
        if await frame.get_by_role('button', name='Challenge Image 1', exact=True).is_visible():
            return None
        box = await frame_box(frame, element)
        viewport = await page.evaluate('() => ({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,scale:visualViewport?.scale??1,dpr:devicePixelRatio})')
        if (not box or box['x'] < viewport['x'] or box['y'] < viewport['y'] or box['width'] <= 0 or box['height'] <= 0
                or box['x'] + box['width'] > viewport['x']+viewport['w'] or box['y'] + box['height'] > viewport['y']+viewport['h']):
            return None
        return frame, element, text, box, viewport
    except (PlaywrightError, ValueError):
        return None


class HcaptchaObjectSolver:
    """Attempt a bounded sequence once and leave confirmation to the caller."""

    def __init__(self):
        self._seen: set[str] = set()
        self._started = False
        self._done = False
        self._rounds = 0

    async def try_object(self, page: Page) -> bool:
        if self._done or self._rounds >= 4:
            return False
        observed = await _object_page(page)
        if observed is None:
            return False
        frame, element, heading, box, viewport = observed
        input_started = False
        try:
            skip = frame.get_by_role('button', name=re.compile(r'^Skip Challenge'))
            if not self._started and not await skip.first.is_visible():
                logger.info('hCaptcha object challenge already in progress')
                return False
            image = await capture_frame(page, frame, element, box)
            fingerprint = hashlib.sha256(heading.encode() + image).hexdigest()
            if fingerprint in self._seen:
                return False
            self._seen.add(fingerprint)
            parser, prompt = (parse_counts, COUNT_PROMPT) if is_count_instruction(heading) else (parse_objects, PROMPT)
            points = parser(await analyze_bytes(image, prompt=prompt, timeout=15))
            if points is None:
                logger.info('hCaptcha object answer unavailable or uncertain')
                return False
            current = await _object_page(page)
            if current is None or current[0] != frame or current[2:] != (heading, box, viewport):
                logger.info('hCaptcha object challenge moved during analysis')
                return False
            if hashlib.sha256(await capture_frame(page, frame, current[1], box)).digest() != hashlib.sha256(image).digest():
                logger.info('hCaptcha object image changed during analysis')
                return False
            for x, y in points:
                current = await _object_page(page)
                if current is None or current[0] != frame or current[2:] != (heading, box, viewport):
                    self._done = input_started
                    return False
                input_started = True
                self._done = True  # Cancellation or uncertain input must not replay this puzzle.
                await page.mouse.click(box['x'] - viewport['x'] + box['width'] * x,
                                       box['y'] - viewport['y'] + box['height'] * y)
            next_page = frame.get_by_role('button', name=re.compile(r'^Next Challenge'))
            last_page = frame.get_by_role('button', name=re.compile(r'^Verify Answers'))
            has_next = await next_page.first.is_visible()
            if has_next:
                await click_control(page, frame, next_page.first)
                self._started = True
            elif await last_page.first.is_visible():
                await click_control(page, frame, last_page.first)
                self._done = True
            else:
                self._done = True
                logger.info('hCaptcha object verification control not found')
                return False
            self._rounds += 1
            await page.wait_for_timeout(1200)
            if has_next:
                current = await _object_page(page)
                if current is not None and await current[0].get_by_role('button', name=re.compile(r'^Skip Challenge')).first.is_visible():
                    fresh = await capture_frame(page, frame, current[1], box)
                    fresh_fingerprint = hashlib.sha256(current[2].encode() + fresh).hexdigest()
                    self._done = fresh_fingerprint in self._seen
            return True
        except (PlaywrightError, OSError, ValueError) as exc:
            if input_started:
                self._done = True
            logger.info('hCaptcha object attempt failed: %s', type(exc).__name__)
            return False
