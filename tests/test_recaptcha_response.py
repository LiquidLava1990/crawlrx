"""Exercise response entry against real alternate and prefilled inputs."""
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _recaptcha_audio_browser as audio


class ResponseEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def exercise(self, input_html, scale=1):
        html = input_html + '''<button id="recaptcha-verify-button"
            onclick="window.submitted=[...document.querySelectorAll('input')].find(e=>e.getClientRects().length).value">Verify</button>'''
        await self.page.route('**/recaptcha/api2/bframe', lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.set_content(f'<iframe style="transform:scale({scale});transform-origin:top left" src="https://fixture.invalid/recaptcha/api2/bframe"></iframe>')
        frame = await audio.resolve_challenge_frame(self.page)
        await frame.wait_for_selector('input:visible')
        with patch.object(audio, 'random_delay', AsyncMock()):
            await audio.submit_audio_response(self.page, 'fresh answer')
        self.assertEqual(await frame.evaluate('window.submitted'), 'fresh answer')

    async def test_alternate_input_is_the_actual_input_target(self):
        await self.exercise('<input aria-label="Type your response">')

    async def test_existing_answer_is_replaced(self):
        await self.exercise('<input id="audio-response" value="old answer">')

    async def test_hidden_canonical_input_does_not_hide_visible_alternative(self):
        await self.exercise('<input id="audio-response" hidden><input aria-label="Audio response">')

    async def test_alternate_input_at_different_layout_scales(self):
        for scale in (.8, 1.25):
            with self.subTest(scale=scale):
                await self.exercise('<input title="Audio response" value="old answer">', scale)

    async def test_missing_input_does_not_press_verify(self):
        await self.page.route('**/recaptcha/api2/bframe', lambda route: route.fulfill(
            body='<button id="recaptcha-verify-button" onclick="window.submitted=true">Verify</button>', content_type='text/html'))
        await self.page.set_content('<iframe src="https://fixture.invalid/recaptcha/api2/bframe"></iframe>')
        frame = await audio.resolve_challenge_frame(self.page)
        await frame.wait_for_selector('button')
        with self.assertRaisesRegex(RuntimeError, 'Audio response input not found'):
            await audio.submit_audio_response(self.page, 'fresh answer')
        self.assertIsNone(await frame.evaluate('window.submitted'))
