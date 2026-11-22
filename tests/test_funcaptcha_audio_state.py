"""Browser changes during audio inference must invalidate its answer."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _funcaptcha_strategies as strategies


HTML = '''<div id=arkose style="width:400px;height:300px;background:white">
<h2>Rotate right</h2><canvas width=100 height=100></canvas>
<audio src="https://fixture.invalid/one.wav"></audio>
<a data-test-id=audio-link href="https://fixture.invalid/one.wav" hidden>Recording</a>
<button data-direction=right onclick="arrows++">Right</button>
<button data-test-id=next-audio onclick="advances++">Next</button></div>
<script>window.arrows=0;window.advances=0;</script>'''


class AudioStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page(viewport={'width': 800, 'height': 600})
        await self.page.route('https://fixture.invalid/**', lambda r: r.abort())
        await self.page.set_content(HTML)
        self.directory = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.directory.cleanup()
        await self.browser.close()
        await self.playwright.stop()

    async def attempt(self, change=None):
        audio = Path(self.directory.name) / 'audio.wav'
        audio.write_bytes(b'fixture audio')
        loop = asyncio.get_running_loop()

        def transcribe(path):
            if change:
                asyncio.run_coroutine_threadsafe(self.page.evaluate(change), loop).result(timeout=5)
            return 'right'

        ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
        with (patch.object(strategies, 'download_audio', AsyncMock(return_value=str(audio))),
              patch.object(strategies, '_solve_check', AsyncMock(return_value=None))):
            result = await strategies._audio_attempt(ctx, self.page, self.page, time.time())
        return audio, result

    async def test_source_changed_without_visual_change_sends_no_input(self):
        audio, _ = await self.attempt("document.querySelector('audio').src='https://fixture.invalid/two.wav'")
        self.assertEqual(await self.page.evaluate('arrows+advances'), 0)
        self.assertFalse(audio.exists())

    async def test_visible_puzzle_change_sends_no_input(self):
        audio, _ = await self.attempt("document.querySelector('canvas').getContext('2d').fillRect(0,0,100,100)")
        self.assertEqual(await self.page.evaluate('arrows+advances'), 0)
        self.assertFalse(audio.exists())

    async def test_audio_removed_sends_no_input(self):
        audio, _ = await self.attempt("document.querySelector('audio').remove()")
        self.assertEqual(await self.page.evaluate('arrows+advances'), 0)
        self.assertFalse(audio.exists())

    async def test_unchanged_challenge_still_receives_real_arrow_and_next(self):
        audio, _ = await self.attempt()
        self.assertEqual(await self.page.evaluate('({arrows,advances})'), {'arrows': 1, 'advances': 1})
        self.assertFalse(audio.exists())

    async def test_change_after_arrow_does_not_advance_new_challenge(self):
        await self.page.evaluate('''() => {document.querySelector('[data-direction=right]').onclick=()=>{
            arrows++;setTimeout(()=>document.querySelector('canvas').getContext('2d').fillRect(0,0,100,100),250)
        }}''')
        audio, _ = await self.attempt()
        self.assertEqual(await self.page.evaluate('({arrows,advances})'), {'arrows': 1, 'advances': 0})
        self.assertFalse(audio.exists())

    async def test_new_recording_after_arrow_does_not_advance(self):
        await self.page.evaluate('''() => {document.querySelector('[data-direction=right]').onclick=()=>{
            arrows++;document.querySelector('a').href='https://fixture.invalid/two.wav'
        }}''')
        audio, _ = await self.attempt()
        self.assertEqual(await self.page.evaluate('({arrows,advances})'), {'arrows': 1, 'advances': 0})
        self.assertFalse(audio.exists())


if __name__ == '__main__':
    unittest.main()
