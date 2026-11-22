"""Both reCAPTCHA callers must keep inference off the loop and release audio."""
import asyncio
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from crawlrx.solver_service.botright_patches import apply_all_patches

apply_all_patches()

from botright.modules import recaptcha_bridge as bridge  # noqa: E402
from crawlrx.solver_service.solvers import _recaptcha_audio_transcribe as audio
from crawlrx.solver_service.solvers import recaptcha_audio_solver as direct


class SpeechOwnershipTests(unittest.TestCase):
    def test_direct_and_bridge_share_one_cached_model(self):
        backend = sys.modules[direct.transcribe_audio.__module__]
        marker = object()
        with patch.object(audio, '_WHISPER_MODEL', marker):
            self.assertIs(backend._WHISPER_MODEL, marker)

    def test_model_initialization_failure_releases_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            path.write_bytes(b'fixture')
            failure = RuntimeError('model initialization failed')
            with (patch.object(audio, '_WHISPER_MODEL', None),
                  patch.object(audio, 'init_whisper', side_effect=failure)):
                with self.assertRaises(RuntimeError) as raised:
                    audio.transcribe_audio(str(path))
            self.assertIs(raised.exception, failure)
            self.assertFalse(path.exists())

    def test_decoder_failure_releases_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            path.write_bytes(b'fixture')
            failure = RuntimeError('decoding failed')
            with (patch.object(audio, '_WHISPER_MODEL', object()),
                  patch.object(audio, '_WHISPER_TYPE', 'faster'),
                  patch.object(audio, '_transcribe_with_faster', side_effect=failure)):
                with self.assertRaises(RuntimeError) as raised:
                    audio.transcribe_audio(str(path))
            self.assertIs(raised.exception, failure)
            self.assertFalse(path.exists())

    def test_ten_concurrent_initializations_share_one_model(self):
        barrier = threading.Barrier(10)

        def initialize():
            time.sleep(.05)
            audio._WHISPER_MODEL = object()
            audio._WHISPER_TYPE = 'faster'
            return True

        def load(index):
            barrier.wait(timeout=2)
            audio.init_whisper()

        with (patch.object(audio, '_WHISPER_MODEL', None),
              patch.object(audio, '_WHISPER_TYPE', None),
              patch.object(audio, '_try_faster_whisper', side_effect=initialize) as initializer,
              ThreadPoolExecutor(max_workers=10) as workers):
            list(workers.map(load, range(10)))
        initializer.assert_called_once()


class CallerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'recording.wav'
        self.path.write_bytes(b'fixture')
        self.page = SimpleNamespace(goto=AsyncMock())
        self.submit = AsyncMock()
        self.audio_state = object()

    async def asyncTearDown(self):
        self.directory.cleanup()

    def patches(self, module, transcribe):
        stack = ExitStack()
        for name, result in [('click_recaptcha_checkbox', None), ('is_challenge_visible', True),
                             ('capture_audio_state', self.audio_state),
                             ('switch_to_audio_challenge', None), ('get_audio_challenge', str(self.path))]:
            stack.enter_context(patch.object(module, name, AsyncMock(return_value=result)))
        stack.enter_context(patch.object(module, 'submit_audio_response', self.submit))
        stack.enter_context(patch.object(module, 'extract_token', AsyncMock(side_effect=[None, 'verified-token']) if module is bridge else AsyncMock(return_value='verified-token')))
        stack.enter_context(patch.object(module.asyncio, 'sleep', AsyncMock()))
        if module is direct:
            stack.enter_context(patch.object(module, 'random_delay', AsyncMock()))
        backend = sys.modules[module.transcribe_audio.__module__]
        stack.enter_context(patch.object(backend, '_WHISPER_MODEL', object()))
        stack.enter_context(patch.object(backend, '_WHISPER_TYPE', 'faster'))
        stack.enter_context(patch.object(backend, '_transcribe_with_faster', side_effect=transcribe))
        return stack

    async def run_caller(self, module):
        if module is bridge:
            return await bridge.solve_recaptcha(self.page)
        return await direct.ReCaptchaAudioSolver._solve_once(None, self.page, 'https://fixture.invalid')

    async def test_direct_solver_keeps_loop_responsive(self):
        loop = asyncio.get_running_loop()
        heartbeat = threading.Event()
        observed = []

        def transcribe(path):
            loop.call_soon_threadsafe(lambda: loop.call_later(.01, heartbeat.set))
            time.sleep(.15)
            observed.append(heartbeat.is_set())
            return 'answer'

        with self.patches(direct, transcribe):
            self.assertEqual(await self.run_caller(direct), 'verified-token')
        self.assertEqual(observed, [True])
        self.submit.assert_awaited_once_with(self.page, 'answer', expected_state=self.audio_state)
        self.assertFalse(self.path.exists())

    async def test_direct_cancellation_cannot_submit_late_answer(self):
        await self.check_cancellation(direct)

    async def test_bridge_cancellation_cannot_submit_late_answer(self):
        await self.check_cancellation(bridge)

    async def test_direct_queued_cancellation_still_releases_audio(self):
        await self.check_queued_cancellation(direct)

    async def test_bridge_queued_cancellation_still_releases_audio(self):
        await self.check_queued_cancellation(bridge)

    async def check_queued_cancellation(self, module):
        release = threading.Event()
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            loop.set_default_executor(executor)
            occupant = loop.run_in_executor(None, release.wait, 2)
            with self.patches(module, lambda path: 'answer'):
                task = asyncio.create_task(self.run_caller(module))
                ready = loop.create_future()
                handle = loop.call_later(.01, ready.set_result, None)
                try:
                    await ready
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(self.path.exists())
                    self.submit.assert_not_awaited()
                finally:
                    handle.cancel()
                    release.set()
                    await occupant
                    await asyncio.to_thread(time.sleep, .03)
                    await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(self.path.exists())
            self.submit.assert_not_awaited()

    async def check_cancellation(self, module):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()

        def transcribe(path):
            loop.call_soon_threadsafe(started.set)
            release.wait(1)
            finished.set()
            return 'answer'

        timer = threading.Timer(.3, release.set)
        timer.start()
        with self.patches(module, transcribe):
            task = asyncio.create_task(self.run_caller(module))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(self.path.exists(), 'A running decoder still owns the recording')
                self.submit.assert_not_awaited()
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 2)
                await asyncio.to_thread(time.sleep, .03)
                await asyncio.gather(task, return_exceptions=True)
                timer.cancel()
        self.assertFalse(self.path.exists())
        self.submit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
