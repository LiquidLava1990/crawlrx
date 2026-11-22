"""Bounded handler for observed hCaptcha ring-selection challenges."""
import asyncio
import hashlib
import logging
import math
import re
from urllib.parse import urlsplit
from playwright.async_api import Frame, Locator, Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_view, click_control, measured_box

logger = logging.getLogger(__name__)

def find_unlinked(screenshot: bytes) -> list[tuple[int, int]]:
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(screenshot, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Invalid ring image')
    height, width = image.shape[:2]
    if min(height, width) < 300 or max(height, width) > 2048 or abs(width/height-500/470) > .004:
        raise ValueError('Unsupported ring image dimensions')
    if (height, width) != (470, 500):
        image = cv2.resize(image, (500, 470), interpolation=cv2.INTER_AREA)
    top = 125
    image = image[top:, :]
    gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 1.1)
    small = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1, minDist=42,
                             param1=100, param2=35, minRadius=23, maxRadius=48)
    big = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1, minDist=90,
                           param1=100, param2=35, minRadius=85, maxRadius=130)
    if small is None or len(small[0]) != 6 or big is None or len(big[0]) != 1:
        raise ValueError('Ring geometry was not identified unambiguously')
    x0, y0, outer = map(float, big[0][0])
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    yy_grid, xx_grid = np.indices(hsv.shape[:2])
    blue_mask = ((hsv[:, :, 0] >= 75) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] >= 60))
    distances = np.hypot(xx_grid-x0, yy_grid-y0)
    hist, _ = np.histogram(distances[blue_mask], bins=np.arange(75, 141))
    if hist.max() < 150:
        raise ValueError('Blue ring centerline is unclear')
    big_r = float(np.argmax(hist) + 75.5)
    if abs(big_r - outer) > 12:
        raise ValueError('Blue ring radius conflicts with geometry')
    red_mask = (((hsv[:, :, 0] <= 20) | (hsv[:, :, 0] >= 170))
                & (hsv[:, :, 1] >= 35) & (hsv[:, :, 2] >= 100))
    median_radius = float(np.median(small[0][:, 2]))
    matches = []
    for x, y, outer_r in small[0]:
        x, y = float(x), float(y)
        ring_distances = np.hypot(xx_grid-x, yy_grid-y)
        ring_hist, _ = np.histogram(ring_distances[red_mask], bins=np.arange(20, 51))
        radii = np.arange(20, 50) + .5
        ring_hist[np.abs(radii - median_radius) > 3] = 0
        if ring_hist.max() < 20:
            raise ValueError('Small ring centerline is unclear')
        r = float(np.argmax(ring_hist) + 20.5)

        if abs(r - float(outer_r)) > 15:
            raise ValueError('Small ring radius conflicts with geometry')
        distance = math.hypot(x-x0, y-y0)
        if distance == 0:
            raise ValueError('Invalid ring centers')
        a = (big_r**2-r**2+distance**2)/(2*distance)
        if big_r**2 <= a**2:
            raise ValueError('Rings do not have two visible crossings')
        h = math.sqrt(big_r**2-a**2)
        ex, ey = (x-x0)/distance, (y-y0)/distance
        crossings = [(x0+a*ex-h*ey, y0+a*ey+h*ex),
                     (x0+a*ex+h*ey, y0+a*ey-h*ex)]
        colors = []
        for px, py in crossings:
            xx, yy = round(px), round(py)
            patch = hsv[max(0, yy-5):yy+6, max(0, xx-5):xx+6]
            blue = int(((patch[:, :, 0] >= 75) & (patch[:, :, 0] <= 115)
                        & (patch[:, :, 1] >= 60)).sum())
            red = int((((patch[:, :, 0] <= 20) | (patch[:, :, 0] >= 170))
                       & (patch[:, :, 1] >= 60)).sum())
            if blue + red < 40:
                colors.append(None)
                continue
            ratio = blue / (blue + red)
            colors.append('blue' if ratio >= .65 else 'red' if ratio <= .25 else None)

        if colors[0] is not None and colors[0] == colors[1]:
            matches.append((round(x), round(y + top)))
    if len(matches) != 2:
        raise ValueError(f'Expected two clear unlinked rings, found {len(matches)}')
    return matches


async def observe_ring(page: Page) -> tuple[Frame, Locator, dict[str, float], dict[str, float]] | None:
    """Find exactly the supported provider-hosted challenge in a visible frame."""
    challenges = []
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            host = (urlsplit(frame.url).hostname or '').lower()
            if host != 'hcaptcha.com' and not host.endswith('.hcaptcha.com'):
                continue
            element = await frame.frame_element()
            if not await element.is_visible():
                continue
            if await frame.get_by_role('heading', level=2).first.is_visible():
                challenges.append(frame)
        except (PlaywrightError, ValueError):
            return None
    if len(challenges) != 1:
        return None
    frame = challenges[0]
    try:
        view = frame.locator('.challenge-view').first
        if not await view.is_visible():
            return None
        text = ' '.join((await view.inner_text()).split()).lower().rstrip('.!')
        if text != 'click the two rings that are not linked to the large circle':
            return None
        box = await measured_box(frame, view)
        viewport = await page.evaluate('() => ({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,scale:visualViewport?.scale??1,dpr:devicePixelRatio})')
        if (not box or box['x'] < viewport['x'] or box['y'] < viewport['y'] or box['width'] <= 0 or box['height'] <= 0
                or box['x'] + box['width'] > viewport['x']+viewport['w']
                or box['y'] + box['height'] > viewport['y']+viewport['h']):
            return None
        return frame, view, box, viewport
    except (PlaywrightError, ValueError):
        return None


class HcaptchaRingSolver:
    """Make one bounded attempt; the caller must check the provider's token."""

    def __init__(self):
        self._seen = set()
        self._started = False
        self._done = False
        self._rounds = 0

    async def try_ring(self, page: Page) -> bool:
        if self._done or self._rounds >= 4:
            return False
        observed = await observe_ring(page)
        if observed is None:
            return False
        frame, view, box, viewport = observed
        input_started = False
        try:
            skip = frame.get_by_role('button', name=re.compile(r'^Skip Challenge'))
            if not self._started and not await skip.first.is_visible():
                return False
            image = await capture_view(page, frame, view, box)
            fingerprint = hashlib.sha256(image).digest()
            if fingerprint in self._seen:
                return False
            self._seen.add(fingerprint)
            points = await asyncio.to_thread(find_unlinked, image)
            current = await observe_ring(page)
            if current is None or current[0] != frame or current[2:] != (box, viewport):
                return False
            if hashlib.sha256(await capture_view(page, frame, current[1], box)).digest() != fingerprint:
                return False
            for x, y in points:
                current = await observe_ring(page)
                if current is None or current[0] != frame or current[2:] != (box, viewport):
                    return False
                input_started = True
                self._done = True
                await page.mouse.click(box['x'] - viewport['x'] + x*box['width']/500,
                                       box['y'] - viewport['y'] + y*box['height']/470)
            next_page = frame.get_by_role('button', name=re.compile(r'^Next Challenge'))
            last_page = frame.get_by_role('button', name=re.compile(r'^Verify Answers'))
            if await next_page.first.is_visible():
                await click_control(page, frame, next_page.first)
                self._started = True
                self._done = False
            elif await last_page.first.is_visible():
                await click_control(page, frame, last_page.first)
                self._done = True
            else:
                self._done = True
                return False
            self._rounds += 1
            await page.wait_for_timeout(1200)
            return True
        except (PlaywrightError, ValueError, OSError, ImportError) as exc:
            if input_started:
                self._done = True
            logger.info('hCaptcha ring attempt failed: %s', type(exc).__name__)
            return False


