import unittest
import base64
from types import SimpleNamespace
import cv2
import numpy as np
from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers._geetest_browser import setup_anti_detection
from crawlrx.solver_service.solvers._geetest_strategies import _match_v4_slide_distance, _v4_slide_distance
from crawlrx.solver_service.solvers._geetest_drag import human_like_drag


class SliderMatchingTests(unittest.TestCase):
    def test_finds_darkened_original_at_correct_y_even_with_decoy(self):
        rng = np.random.default_rng(7)
        bg = rng.integers(30, 230, size=(160, 260, 3), dtype=np.uint8)
        y, target, start, size = 40, 150, 0, 50
        piece = np.zeros((size, size, 4), dtype=np.uint8)
        piece[:, :, :3] = bg[y:y+size, target:target+size]
        piece[7:43, 7:43, 3] = 255
        bg[y:y+size, target:target+size] = (bg[y:y+size, target:target+size] * 0.48).astype(np.uint8)
        bg[y:y+size, 75:75+size] = (bg[y:y+size, 75:75+size] * 0.35).astype(np.uint8)
        self.assertEqual(_match_v4_slide_distance(bg, piece, y, start), target)

    def test_rejects_invalid_geometry_and_missing_alpha(self):
        bg = np.zeros((100, 200, 3), dtype=np.uint8)
        piece = np.zeros((40, 40, 4), dtype=np.uint8)
        self.assertIsNone(_match_v4_slide_distance(bg, piece, 70, 0))
        self.assertIsNone(_match_v4_slide_distance(bg, piece, 20, 0))
        self.assertIsNone(_match_v4_slide_distance(bg, piece[:, :, :3], 20, 0))


class SliderBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_existing_response_and_restores_piece_visibility(self):
        await self._capture_fixture(1, 1)

    async def test_scales_distance_for_layout_zoom_and_raster_dpr(self):
        for scale in (.8, 1.25):
            for dpr in (1, 2):
                with self.subTest(scale=scale, dpr=dpr):
                    await self._capture_fixture(scale, dpr)

    async def test_visual_zoom_preserves_measurement_and_trusted_drag(self):
        for zoom in (1.25, 1.5):
            for dpr in (1, 2):
                with self.subTest(zoom=zoom, dpr=dpr):
                    await self._capture_fixture(1, dpr, zoom)

    async def test_panned_visual_viewport_preserves_measurement_and_trusted_drag(self):
        for dpr in (1, 2):
            with self.subTest(dpr=dpr):
                await self._capture_fixture(1, dpr, 1.25, pan=True)

    async def _capture_fixture(self, scale, dpr, zoom=1, pan=False):
        rng = np.random.default_rng(11)
        bg = rng.integers(30, 230, size=(160, 260, 3), dtype=np.uint8)
        piece = np.zeros((50, 50, 4), dtype=np.uint8)
        piece[:, :, :3] = bg[40:90, 150:200]
        piece[7:43, 7:43, 3] = 255
        bg[40:90, 150:200] = (bg[40:90, 150:200] * .48).astype(np.uint8)
        _, bg_png = cv2.imencode('.png', bg)
        _, piece_png = cv2.imencode('.png', piece)
        encoded_bg = base64.b64encode(bg_png.tobytes()).decode()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = await browser.new_context(viewport={'width': 1000, 'height': 800},
                                                device_scale_factor=dpr, is_mobile=pan, has_touch=pan)
            await context.route('https://fixture.test/piece.png',
                lambda route: route.fulfill(status=200, content_type='image/png', body=piece_png.tobytes()))
            page = await context.new_page()
            host = SimpleNamespace(page=page, _geetest_version=4)
            await setup_anti_detection(host)
            try:
                await page.set_content(f'''<meta name="viewport" content="width=device-width,initial-scale=1"><style>
                  body {{zoom:{scale};padding-left:{160 if pan else 0}px;padding-top:{120 if pan else 0}px}}
                  .geetest_bg {{position:relative;width:260px;height:160px;
                    background-image:url(data:image/png;base64,{encoded_bg})}}
                  .geetest_slice {{position:absolute;left:0;top:40px;width:50px;height:50px}}
                  .geetest_slice_bg {{width:50px;height:50px;background-image:url(https://fixture.test/piece.png)}}
                  .geetest_slider {{margin-top:20px;width:40px;height:28px;background:teal}}
                </style><div class="geetest_bg"><div class="geetest_slice" style="visibility:visible">
                  <div class="geetest_slice_bg"></div></div></div><div class="geetest_slider"></div>
                <script>
                  window.dragProof={{downs:0,ups:0,trusted:true,delta:null}};
                  let start=null;
                  document.querySelector('.geetest_slider').onpointerdown=e=>{{
                    start=e.clientX;dragProof.downs++;dragProof.trusted&&=e.isTrusted;
                  }};
                  document.onpointerup=e=>{{if(start!==null){{
                    dragProof.delta=e.clientX-start;dragProof.ups++;dragProof.trusted&&=e.isTrusted;start=null;
                  }}}};
                </script>''', wait_until='load')
                cdp = await context.new_cdp_session(page)
                await cdp.send('Emulation.setPageScaleFactor', {'pageScaleFactor': zoom})
                if pan:
                    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    await cdp.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[{'x':700,'y':500,'id':1}]})
                    for step in range(1, 9):
                        await cdp.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[{'x':700-step*10,'y':500-step*8,'id':1}]})
                        await page.wait_for_timeout(20)
                    await page.wait_for_timeout(150)
                    await cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
                    await page.wait_for_function('visualViewport.offsetLeft>0 && visualViewport.offsetTop>0')
                    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                viewport_script = '()=>({x:visualViewport.offsetLeft,y:visualViewport.offsetTop,scale:visualViewport.scale})'
                viewport = await page.evaluate(viewport_script)
                self.assertAlmostEqual(viewport['scale'], zoom)
                if pan:
                    self.assertGreater(viewport['x'], 0)
                    self.assertGreater(viewport['y'], 0)
                distance = await _v4_slide_distance(host)
                self.assertIsNotNone(distance, {
                    'viewport': viewport,
                    'playwrightBox': await page.locator('.geetest_bg').bounding_box(),
                    'clientRect': await page.locator('.geetest_bg').evaluate('el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}}'),
                })
                self.assertAlmostEqual(distance, 150 * scale, delta=1)
                self.assertEqual(await page.evaluate(viewport_script), viewport)
                await human_like_drag(host, page.locator('.geetest_slider'), distance)
                proof = await page.evaluate('window.dragProof')
                self.assertEqual(proof['downs'], 1)
                self.assertEqual(proof['ups'], 1)
                self.assertTrue(proof['trusted'])
                self.assertAlmostEqual(proof['delta'], distance, delta=.01)
                self.assertEqual(await page.locator('.geetest_slice').evaluate('e => e.style.visibility'), 'visible')
                self.assertTrue(await page.locator('.geetest_slice').is_visible())
            finally:
                await context.close()
                await browser.close()


if __name__ == '__main__':
    unittest.main()
