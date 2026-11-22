"""Solve the observed two-piece hCaptcha line puzzle by fitting visible endpoints."""
import asyncio
import hashlib
import logging
import re
from urllib.parse import urlsplit
from playwright.async_api import ElementHandle, Frame, Page, Error as PlaywrightError
from .hcaptcha_viewport import capture_frame, click_control, frame_box
from .hcaptcha_line_geometry import line_plan

logger=logging.getLogger(__name__)

async def observe_line(page: Page) -> tuple[Frame, ElementHandle, dict[str, float], dict[str, float]] | None:
    challenges=[]
    for frame in page.frames:
        if frame==page.main_frame: continue
        try:
            host=(urlsplit(frame.url).hostname or '').lower()
            if host!='hcaptcha.com' and not host.endswith('.hcaptcha.com'): continue
            element=await frame.frame_element()
            heading=frame.get_by_role('heading',level=2).first
            if await element.is_visible() and await heading.is_visible(): challenges.append((frame,element,heading))
        except (PlaywrightError,ValueError):
            return None
    if len(challenges)!=1: return None
    frame,element,heading=challenges[0]
    try:
        text=' '.join((await heading.inner_text()).split()).lower().rstrip('.!')
        if text!='please drag the segments on the right to complete the line': return None
        box=await frame_box(frame, element)
        viewport=await page.evaluate('()=>({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,scale:visualViewport?.scale??1,dpr:devicePixelRatio})')
        if (not box or box['width'] <= 0 or box['height'] <= 0
                or box['x'] < viewport['x'] or box['y'] < viewport['y'] or box['x']+box['width']>viewport['x']+viewport['w']
                or box['y']+box['height']>viewport['y']+viewport['h']): return None
        return frame,element,box,viewport
    except (PlaywrightError,ValueError):
        return None

class HcaptchaLineSolver:
    def __init__(self):
        self._started=False
        self._done=False
        self._rounds=0
        self._seen=set()

    async def try_line(self, page: Page) -> bool:
        if self._done or self._rounds>=3: return False
        observed=await observe_line(page)
        if observed is None: return False
        frame,element,box,viewport=observed
        try:
            skip=frame.get_by_role('button',name=re.compile(r'^Skip Challenge'))
            if not self._started and not await skip.first.is_visible(): return False
            image=await capture_frame(page, frame, element, box)
            fingerprint=hashlib.sha256(image).digest()
            if fingerprint in self._seen: return False
            self._seen.add(fingerprint)
            actions=await asyncio.to_thread(line_plan,image)
            current=await observe_line(page)
            if current is None or current[0]!=frame or current[2:]!=(box,viewport): return False
            if hashlib.sha256(await capture_frame(page, frame, current[1], box)).digest()!=fingerprint:
                return False
            self._done=True
            for action in actions:
                current=await observe_line(page)
                if current is None or current[0]!=frame or current[2:]!=(box,viewport): return False
                x,y=action['from']; tx,ty=action['to']
                await page.mouse.move(box['x']-viewport['x']+x*box['width']/520,box['y']-viewport['y']+y*box['height']/570)
                completed = False
                try:
                    await page.mouse.down()
                    await page.mouse.move(box['x']-viewport['x']+tx*box['width']/520,box['y']-viewport['y']+ty*box['height']/570,steps=20)
                    completed = True
                finally:
                    try:
                        await page.mouse.up()
                    except (PlaywrightError, OSError, RuntimeError) as error:
                        if completed:
                            raise
                        logger.warning('Mouse release failed after interrupted hCaptcha line input: %s', type(error).__name__)
                await page.wait_for_timeout(400)
            current=await observe_line(page)
            if current is None or current[0]!=frame or current[2:]!=(box,viewport): return False
            button=frame.get_by_role('button',name=re.compile(r'^(Next Challenge|Verify Answers)'))
            if not await button.first.is_visible(): return False
            is_next=(await button.first.get_attribute('aria-label') or '').startswith('Next Challenge')
            after_drag=await capture_frame(page, frame, element, box)
            await click_control(page, frame, button.first)
            self._started=True
            self._rounds+=1
            await page.wait_for_timeout(1200)
            if is_next:
                current=await observe_line(page)
                if current is not None and current[0]==frame and current[2:]==(box,viewport):
                    fresh=await capture_frame(page, frame, current[1], box)
                    if hashlib.sha256(fresh).digest()!=hashlib.sha256(after_drag).digest(): self._done=False
            return True
        except (PlaywrightError,ValueError,OSError,ImportError) as exc:
            logger.info('hCaptcha line attempt failed: %s',type(exc).__name__)
            return False
