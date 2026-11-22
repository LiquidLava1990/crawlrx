"""FunCaptcha captures and arrow input must follow the visible viewport."""
import unittest
import time
from types import SimpleNamespace

import cv2
import numpy as np
from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _funcaptcha_browser as dom
from crawlrx.solver_service.solvers import _funcaptcha_strategies as strategies


WIDGET = '''<style>body{margin:0}h2{margin:0;font:20px sans-serif}
canvas{position:absolute;left:0;top:50px}
button{position:absolute;left:230px;top:205px;width:70px;height:28px}</style>
<h2>Rotate the image right</h2><canvas width=300 height=150></canvas>
<button data-direction="right">Right</button><script>
window.resizes=0;window.clicks=0;window.syntheticClicks=0;
addEventListener('resize',()=>window.resizes++);
document.querySelector('button').onclick=e=>{if(e.isTrusted)window.clicks++;else window.syntheticClicks++};
const c=document.querySelector('canvas').getContext('2d');
c.fillStyle='#a7cfef';c.fillRect(0,0,300,150);c.fillStyle='#da3410';c.fillRect(45,30,60,60);
</script>'''


class FunCaptchaViewportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True, args=['--no-sandbox'],
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def fixture(self, scale=1, dpr=1, zoom=1, pan=False, top=130):
        context = await self.browser.new_context(
            viewport={'width': 960, 'height': 720}, device_scale_factor=dpr,
            is_mobile=pan, has_touch=pan,
        )
        page = await context.new_page()
        await page.route('https://client-api.arkoselabs.com/challenge',
                         lambda route: route.fulfill(body=WIDGET, content_type='text/html'))
        await page.set_content(
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{margin:0;height:1600px}</style>'
            f'<iframe src="https://client-api.arkoselabs.com/challenge" '
            f'style="position:absolute;left:140px;top:{top}px;width:320px;height:240px;'
            f'border:4px solid;padding:3px;transform:scale({scale});transform-origin:0 0"></iframe>',
        )
        element = await page.query_selector('iframe')
        frame = await element.content_frame()
        await frame.wait_for_load_state()
        if zoom != 1:
            cdp = await context.new_cdp_session(page)
            await cdp.send('Emulation.setPageScaleFactor', {'pageScaleFactor': zoom})
            await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            if pan:
                await cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 800, 'y': 500, 'id': 1}]})
                for step in range(1, 9):
                    await cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 800-step*10, 'y': 500-step*8, 'id': 1}]})
                    await page.wait_for_timeout(20)
                await page.wait_for_timeout(150)
                await cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                await page.wait_for_function('visualViewport.offsetLeft>0&&visualViewport.offsetTop>0')
                await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
        await page.wait_for_timeout(50)
        return context, page, frame, element

    async def test_full_widget_capture_and_trusted_scaled_arrows(self):
        for scale, dpr, zoom, pan in [
            (1, 1, 1, False), (.8, 1, 1, False), (1.25, 1, 1, False),
            (.8, 2, 1, False), (1.25, 2, 1, False),
            (1, 1, 1.25, False), (1, 2, 1.5, False), (1, 2, 1.25, True),
        ]:
            with self.subTest(scale=scale, dpr=dpr, zoom=zoom, pan=pan):
                context, page, frame, element = await self.fixture(scale, dpr, zoom, pan)
                try:
                    before = await page.evaluate('()=>({w:innerWidth,h:innerHeight,x:scrollX,y:scrollY,vx:visualViewport.offsetLeft,vy:visualViewport.offsetTop})')
                    initial_resizes = await frame.evaluate('resizes')
                    rect = await element.evaluate('el=>({w:el.getBoundingClientRect().width,h:el.getBoundingClientRect().height})')
                    data = await dom.capture_puzzle(frame)
                    self.assertIsNotNone(data)
                    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    self.assertAlmostEqual(image.shape[1], rect['w']*dpr*zoom, delta=1)
                    self.assertAlmostEqual(image.shape[0], rect['h']*dpr*zoom, delta=1)
                    self.assertEqual(await frame.evaluate('resizes'), initial_resizes, 'capture resized the widget')
                    await dom.click_arrow(frame, 'right')
                    self.assertEqual(await frame.evaluate('clicks'), 1)
                    self.assertEqual(await frame.evaluate('syntheticClicks'), 0)
                    self.assertEqual(await frame.evaluate('resizes'), initial_resizes, 'input resized the widget')
                    self.assertEqual(await page.evaluate('()=>({w:innerWidth,h:innerHeight,x:scrollX,y:scrollY,vx:visualViewport.offsetLeft,vy:visualViewport.offsetTop})'), before)
                finally:
                    await context.close()

    async def test_offscreen_widget_refused_without_scrolling(self):
        context, page, frame, _ = await self.fixture(top=900)
        try:
            self.assertIsNone(await dom.capture_puzzle(frame))
            self.assertEqual(await page.evaluate('scrollY'), 0)
        finally:
            await context.close()

    async def test_scaled_arrow_input(self):
        for scale, dpr, zoom, pan in [(.8, 1, 1, False), (1.25, 2, 1, False), (1, 2, 1.25, True)]:
            with self.subTest(scale=scale, dpr=dpr, zoom=zoom, pan=pan):
                context, page, frame, _ = await self.fixture(scale, dpr, zoom, pan)
                try:
                    await dom.click_arrow(frame, 'right')
                    self.assertEqual(await frame.evaluate('clicks'), 1)
                    self.assertEqual(await frame.evaluate('syntheticClicks'), 0)
                finally:
                    await context.close()

    async def test_widget_changed_during_model_analysis_receives_no_input(self):
        context, page, frame, element = await self.fixture(scale=.8, dpr=2)
        try:
            async def analyze(image, **kwargs):
                await frame.evaluate("document.querySelector('canvas').getContext('2d').clearRect(0,0,300,150)")
                return 'right'

            ctx = SimpleNamespace(timeout=30, vision=SimpleNamespace(analyze_async=analyze))
            await strategies._vision_step(ctx, frame, page, time.time(), 0, element)
            self.assertEqual(await frame.evaluate('clicks+syntheticClicks'), 0)
        finally:
            await context.close()


if __name__ == '__main__':
    unittest.main()
