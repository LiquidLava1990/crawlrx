"""Old transcripts must not reach a refreshed or replaced browser challenge."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from crawlrx.solver_service.botright_patches import apply_all_patches

apply_all_patches()

from botright.modules import recaptcha_bridge as bridge  # noqa: E402
from crawlrx.solver_service.solvers import recaptcha_audio_solver as direct
from crawlrx.solver_service.solvers import _recaptcha_audio_browser as audio


HOST = 'https://fixture.invalid/host'
FRAME = 'https://fixture.invalid/recaptcha/api2/bframe'
HTML = '''<a id="audio-source" href="https://fixture.invalid/audio/first.mp3">Recording</a>
<input id="audio-response"><button id="recaptcha-verify-button"
onclick="parent.verifies++; parent.document.querySelector('textarea').value='fixture-token'">Verify</button>'''


class ChallengeStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()
        await self.page.route(FRAME + '**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
        await self.page.route(HOST, lambda route: route.fulfill(body=(
            '<textarea name="g-recaptcha-response"></textarea><script>window.verifies=0</script>'
            f'<iframe src="{FRAME}"></iframe>'), content_type='text/html'))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def run_case(self, module, change):
        await self.page.goto(HOST)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.mp3'
            path.write_bytes(b'fixture recording')

            async def transcribe(recording):
                try:
                    frame = await audio.resolve_challenge_frame(self.page)
                    if change == 'source':
                        await frame.eval_on_selector('#audio-source', "e=>e.href='/audio/second.mp3'")
                    elif change == 'navigation':
                        await frame.goto(FRAME + '?new-document')
                    elif change == 'replacement':
                        await self.page.eval_on_selector('iframe', "e=>{const n=e.cloneNode();e.replaceWith(n)}")
                        await self.page.frame_locator('iframe').locator('#audio-response').wait_for()
                    return 'old transcript'
                finally:
                    Path(recording).unlink(missing_ok=True)

            with ExitStack() as stack:
                for name, result in [('click_recaptcha_checkbox', None), ('is_challenge_visible', True),
                                     ('switch_to_audio_challenge', None), ('get_audio_challenge', str(path))]:
                    stack.enter_context(patch.object(module, name, AsyncMock(return_value=result)))
                stack.enter_context(patch.object(module, 'transcribe_audio_async', side_effect=transcribe))
                stack.enter_context(patch.object(audio, 'random_delay', AsyncMock()))
                if module is direct:
                    stack.enter_context(patch.object(module, 'random_delay', AsyncMock()))
                    call = direct.ReCaptchaAudioSolver._solve_once(None, self.page, HOST)
                else:
                    stack.enter_context(patch.object(module.asyncio, 'sleep', AsyncMock()))
                    call = module.solve_recaptcha(self.page)
                if change:
                    with self.assertRaisesRegex(RuntimeError, 'Audio challenge changed'):
                        await call
                else:
                    self.assertEqual(await call, 'fixture-token')
            self.assertFalse(path.exists())
        frame = await audio.resolve_challenge_frame(self.page)
        self.assertEqual(await self.page.evaluate('window.verifies'), 0 if change else 1)
        self.assertEqual(await frame.input_value('#audio-response'), '' if change else 'old transcript')

    async def test_callers_reject_changed_challenges(self):
        for module in (direct, bridge):
            for change in ('source', 'navigation', 'replacement'):
                with self.subTest(caller=module.__name__, change=change):
                    await self.run_case(module, change)

    async def test_callers_allow_unchanged_challenges(self):
        for module in (direct, bridge):
            with self.subTest(caller=module.__name__):
                await self.run_case(module, None)

    async def test_refresh_during_response_entry_cannot_press_verify(self):
        await self.page.goto(HOST)
        state = await audio.capture_audio_state(self.page)
        frame = state[0]
        await frame.eval_on_selector('#audio-response', "e=>e.oninput=()=>{document.querySelector('#audio-source').href='/audio/new.mp3'}")
        with patch.object(audio, 'random_delay', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Audio challenge changed'):
                await audio.submit_audio_response(self.page, 'answer', expected_state=state)
        self.assertEqual(await self.page.evaluate('window.verifies'), 0)
