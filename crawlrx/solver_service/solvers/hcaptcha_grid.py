"""Solve hCaptcha nine-tile challenges in the browser page that owns them."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from urllib.parse import urlsplit

from playwright.async_api import Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_frame, click_control, frame_box

from ..core.vision_router import analyze_bytes

logger = logging.getLogger(__name__)
PROMPT = ('Inspect the 3 by 3 image grid. Number by rows explicitly: top row 1,2,3; '
          'middle row 4,5,6; bottom row 7,8,9. First identify the subject of each tile, '
          'then select all tiles satisfying the instruction. Respond only as JSON with '
          'subjects (a list of nine short descriptions), tiles (the selected tile numbers), '
          'and confidence (a number between 0 and 1). If uncertain return an empty tiles list.')


def parse_answer(text: str | None) -> list[int] | None:
    if not isinstance(text, str):
        return None
    match = re.search(r'\{[^{}]*\}', text)
    if not match:
        return None
    try:
        answer = json.loads(match.group())
    except (ValueError, TypeError):
        return None
    if not isinstance(answer, dict):
        return None
    tiles, confidence, subjects = answer.get('tiles'), answer.get('confidence'), answer.get('subjects')
    if (not isinstance(subjects, list) or len(subjects) != 9
            or any(not isinstance(s, str) or not s.strip() for s in subjects)
            or not isinstance(tiles, list) or not tiles or len(tiles) > 9
            or any(type(n) is not int or n < 1 or n > 9 for n in tiles)
            or len(set(tiles)) != len(tiles)
            or type(confidence) not in (float, int) or not 0.8 <= confidence <= 1):
        return None
    return tiles


async def _grid(page):
    """Return an observed visible hCaptcha grid and a stable DOM fingerprint."""
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
            continue
    if len(challenges) != 1:
        return None
    frame, element, heading = challenges[0]
    try:
        cards = [frame.get_by_role('button', name=f'Challenge Image {i}', exact=True)
                 for i in range(1, 10)]
        if not all([await card.is_visible() for card in cards]):
            return None
        if any([await card.get_attribute('aria-pressed') == 'true' for card in cards]):
            return None
        parts = [await heading.inner_text()]
        parts.extend([await card.evaluate('(node) => node.outerHTML') for card in cards])
        fingerprint = hashlib.sha256('\n'.join(parts).encode()).hexdigest()
        return frame, element, cards, fingerprint
    except (PlaywrightError, ValueError):
        return None


class HcaptchaGridSolver:
    """Try each observed grid once; unsupported or uncertain puzzles stay unsolved."""

    def __init__(self):
        self._seen: set[str] = set()

    async def try_grid(self, page: Page) -> bool:
        grid = await _grid(page)
        if grid is None:
            return False
        frame, element, cards, fingerprint = grid
        if fingerprint in self._seen:
            return False
        self._seen.add(fingerprint)
        try:
            box = await frame_box(frame, element)
            image = await capture_frame(page, frame, element, box)
            response = await analyze_bytes(image, prompt=PROMPT, timeout=15)
            tiles = parse_answer(response)
            if tiles is None:
                logger.info('hCaptcha grid model answer unavailable or uncertain')
                return False
            current = await _grid(page)
            if current is None or current[0] != frame or current[3] != fingerprint:
                logger.info('hCaptcha grid changed during image analysis')
                return False
            current_image = await capture_frame(page, frame, current[1], box)
            if hashlib.sha256(current_image).digest() != hashlib.sha256(image).digest():
                logger.info('hCaptcha grid image changed during image analysis')
                return False
            for index in tiles:
                await click_control(page, frame, cards[index - 1])
            verify = frame.get_by_role('button', name=re.compile(r'^(Verify Answers|Next Challenge)'))
            if not await verify.first.is_visible():
                logger.info('hCaptcha grid verify control not found')
                return False
            await click_control(page, frame, verify.first)
            return True
        except (PlaywrightError, OSError, ValueError) as exc:
            logger.info('hCaptcha grid attempt failed: %s', type(exc).__name__)
            return False
