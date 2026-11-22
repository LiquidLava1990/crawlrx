"""Measure and capture visible challenge content through scaled iframe borders."""
import math

import cv2
import numpy as np
from playwright.async_api import ElementHandle, Frame, Locator, Page

_VIEWPORT = """() => ({x:visualViewport?.offsetLeft??0,y:visualViewport?.offsetTop??0,
 w:visualViewport?.width??innerWidth,h:visualViewport?.height??innerHeight,
 scale:visualViewport?.scale??1,dpr:devicePixelRatio})"""

_RECT = """el => {
 for(let node=el;node;node=node.parentElement){
  const t=getComputedStyle(node).transform;
  if(t!=='none'){const m=new DOMMatrix(t);
   if(!m.is2D||m.a<=0||m.d<=0||Math.abs(m.b)>1e-8||Math.abs(m.c)>1e-8)
    throw Error('Unsupported challenge transform');}
 }
 const r=el.getBoundingClientRect(),s=getComputedStyle(el);
 return {x:r.x,y:r.y,width:r.width,height:r.height,
  outerWidth:el.offsetWidth,outerHeight:el.offsetHeight,
  left:el.clientLeft+parseFloat(s.paddingLeft),top:el.clientTop+parseFloat(s.paddingTop)};
}"""


async def measured_box(frame: Frame, locator: Locator | ElementHandle, *, unobscured: bool = False) -> dict[str, float]:
    """Return a top-level viewport rectangle, including each ancestor scale."""
    local = await locator.evaluate(_RECT)
    box = {key: local[key] for key in ('x', 'y', 'width', 'height')}
    current = frame
    while current.parent_frame is not None:
        element = await current.frame_element()
        try:
            outer = await element.evaluate(_RECT)
            if outer['outerWidth'] <= 0 or outer['outerHeight'] <= 0:
                raise ValueError('Challenge frame has no layout size')
            sx, sy = outer['width'] / outer['outerWidth'], outer['height'] / outer['outerHeight']
            box = {'x': outer['x'] + (outer['left'] + box['x']) * sx,
                   'y': outer['y'] + (outer['top'] + box['y']) * sy,
                   'width': box['width'] * sx, 'height': box['height'] * sy}
            if unobscured and not await element.evaluate("""(el,p) => {
              const hit=document.elementFromPoint(p.x+p.width/2,p.y+p.height/2);
              return !!hit&&(hit===el||el.contains(hit));}""", box):
                raise ValueError('Challenge frame covered')
        finally:
            await element.dispose()
        current = current.parent_frame
    if not all(math.isfinite(value) for value in box.values()) or min(box['width'], box['height']) <= 0:
        raise ValueError('Invalid challenge rectangle')
    return box


async def frame_box(frame: Frame, element: ElementHandle) -> dict[str, float]:
    """Measure an iframe element in the parent frame that owns it."""
    if frame.parent_frame is None:
        raise ValueError('Challenge must belong to a child frame')
    return await measured_box(frame.parent_frame, element)


async def capture_frame(page: Page, frame: Frame, element: ElementHandle,
                        expected: dict[str, float]) -> bytes:
    """Capture a whole provider frame using its parent's coordinate system."""
    if frame.parent_frame is None:
        raise ValueError('Challenge must belong to a child frame')
    return await capture_view(page, frame.parent_frame, element, expected)


async def capture_view(page: Page, frame: Frame, locator: Locator | ElementHandle,
                       expected: dict[str, float]) -> bytes:
    """Crop viewport pixels without an element capture resizing the challenge."""
    if await measured_box(frame, locator) != expected:
        raise ValueError('Challenge moved before capture')
    viewport = await page.evaluate(_VIEWPORT)
    data = await page.screenshot(type='png', timeout=3000)
    if await measured_box(frame, locator) != expected or viewport != await page.evaluate(_VIEWPORT):
        raise ValueError('Challenge geometry changed during capture')
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if (image is None or not all(math.isfinite(value) for value in viewport.values())
            or min(viewport['w'], viewport['h'], viewport['scale'], viewport['dpr']) <= 0):
        raise ValueError('Invalid viewport image')
    height, width = image.shape[:2]
    sx, sy = width / viewport['w'], height / viewport['h']
    if not math.isclose(sx, sy, rel_tol=.005):
        raise ValueError('Inconsistent viewport image scale')
    left, top = round((expected['x'] - viewport['x']) * sx), round((expected['y'] - viewport['y']) * sy)
    right = round((expected['x'] - viewport['x'] + expected['width']) * sx)
    bottom = round((expected['y'] - viewport['y'] + expected['height']) * sy)
    if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
        raise ValueError('Challenge outside viewport')
    ok, encoded = cv2.imencode('.png', image[top:bottom, left:right])
    if not ok:
        raise ValueError('Challenge crop failed')
    return encoded.tobytes()


async def click_control(page: Page, frame: Frame, locator: Locator) -> None:
    """Deliver one trusted click at the measured, visible control center."""
    if not await locator.is_visible() or not await locator.is_enabled():
        raise ValueError('Challenge control unavailable')
    clear = await locator.evaluate("""el => {const r=el.getBoundingClientRect();
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return !!hit&&(hit===el||el.contains(hit));}""")
    if not clear:
        raise ValueError('Challenge control covered')
    viewport = await page.evaluate(_VIEWPORT)
    box = await measured_box(frame, locator, unobscured=True)
    if (await measured_box(frame, locator, unobscured=True) != box
            or viewport != await page.evaluate(_VIEWPORT)):
        raise ValueError('Challenge control moved')
    await page.mouse.click(box['x'] - viewport['x'] + box['width'] / 2,
                           box['y'] - viewport['y'] + box['height'] / 2)
