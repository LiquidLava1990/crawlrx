"""Fallback selectors and small transformed controls must deliver trusted input."""
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _recaptcha_audio_browser as audio


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def frame(self, html, kind='bframe', scale=1):
        url = 'https://fixture.invalid/recaptcha/api2/' + kind
        await self.page.route(url, lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.set_content(f'<iframe style="transform:scale({scale});transform-origin:top left" src="{url}"></iframe>')
        await self.page.frame_locator('iframe').locator('body').wait_for()
        return next(frame for frame in self.page.frames if frame.url == url)

    async def test_alternate_checkbox(self):
        frame = await self.frame('<button role="checkbox" onclick="window.pressed=event.isTrusted">Check</button>', 'anchor')
        await audio.click_recaptcha_checkbox(self.page)
        self.assertTrue(await frame.evaluate('window.pressed'))

    async def test_alternate_audio_button(self):
        frame = await self.frame('<button title="Get an audio challenge" onclick="window.pressed=event.isTrusted">Audio</button>')
        with patch.object(audio, 'random_delay', AsyncMock()):
            await audio.switch_to_audio_challenge(self.page)
        self.assertTrue(await frame.evaluate('window.pressed'))

    async def test_alternate_verify_button(self):
        frame = await self.frame('<input id="audio-response"><button aria-label="Verify answer" onclick="window.pressed=event.isTrusted">Verify</button>')
        with patch.object(audio, 'random_delay', AsyncMock()):
            await audio.submit_audio_response(self.page, 'answer')
        self.assertTrue(await frame.evaluate('window.pressed'))

    async def test_alternate_recording_link(self):
        frame = await self.frame('<a class="rc-audiochallenge-tdownload" href="/recording.mp3">Download</a>')
        self.assertEqual(await audio._probe_audio_url(frame), 'https://fixture.invalid/recording.mp3')

    async def test_recording_can_arrive_after_empty_primary(self):
        frame = await self.frame('''<audio id="audio-source"></audio><script>
            setTimeout(()=>{const a=document.createElement('a');a.className='rc-audiochallenge-tdownload';
            a.href='/delayed.mp3';document.body.append(a)},100)</script>''')
        self.assertEqual(await audio._probe_audio_url(frame), 'https://fixture.invalid/delayed.mp3')

    async def test_hover_refresh_cannot_press_verify(self):
        frame = await self.frame('''<a id="audio-source" href="/old.mp3">Recording</a><input id="audio-response">
            <button id="recaptcha-verify-button" onmouseenter="document.querySelector('a').href='/new.mp3'"
            onclick="window.pressed=true">Verify</button>''')
        state = await audio.capture_audio_state(self.page)
        with patch.object(audio, 'random_delay', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Audio challenge changed'):
                await audio.submit_audio_response(self.page, 'answer', expected_state=state)
        self.assertIsNone(await frame.evaluate('window.pressed'))

    async def test_small_scaled_controls_are_not_missed_by_jitter(self):
        for scale in (.8, 1, 1.25):
            with self.subTest(scale=scale):
                frame = await self.frame('<button style="position:absolute;left:40px;top:40px;width:6px;height:6px;padding:0;border:0" onclick="window.pressed=event.isTrusted"></button>', scale=scale)
                with (patch.object(audio.random, 'randint', return_value=5),
                      patch.object(audio, '_human_like_mouse_move', AsyncMock()),
                      patch.object(audio, 'random_delay', AsyncMock())):
                    await audio.click_element(self.page, 'button', frame)
                self.assertTrue(await frame.evaluate('window.pressed'))

    async def test_missing_control_returns_false(self):
        frame = await self.frame('<p>No controls</p>')
        self.assertFalse(await audio.click_first_match(self.page, frame, ['#absent', 'button'], timeout_ms=50))
