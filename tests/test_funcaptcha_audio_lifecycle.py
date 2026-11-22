"""Audio inference must not stall other sessions or act after interruption."""
import asyncio
import contextvars
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from crawlrx.solver_service.solvers import _funcaptcha_rotation as rotation
from crawlrx.solver_service.solvers import _funcaptcha_strategies as strategies


class AudioLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.audio = Path(self.directory.name) / 'challenge.wav'
        self.audio.write_bytes(b'owned test audio')
        self.click = AsyncMock()
        self.next_audio = AsyncMock()

    async def asyncTearDown(self):
        self.directory.cleanup()

    def interaction_patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(strategies, 'capture_audio_state', AsyncMock(return_value=(b'widget', 'audio'))))
        stack.enter_context(patch.object(strategies, 'download_audio', AsyncMock(return_value=str(self.audio))))
        stack.enter_context(patch.object(strategies, 'click_arrow', self.click))
        stack.enter_context(patch.object(strategies, 'click_next_audio', self.next_audio))
        stack.enter_context(patch.object(strategies, '_solve_check', AsyncMock(return_value=None)))
        return stack

    async def test_transcription_allows_other_event_loop_work(self):
        heartbeat = threading.Event()
        release = threading.Event()
        observed = []

        def transcribe(path):
            release.wait(1)
            observed.append(heartbeat.is_set())
            return 'right'

        timer = threading.Timer(.3, release.set)
        timer.start()
        handle = asyncio.get_running_loop().call_later(.02, heartbeat.set)
        try:
            ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
            with self.interaction_patches(), patch.object(strategies.asyncio, 'sleep', AsyncMock()):
                await strategies._audio_attempt(ctx, None, None, time.time())
            self.assertEqual(observed, [True])
            self.assertFalse(self.audio.exists())
            self.click.assert_awaited_once_with(None, 'right')
        finally:
            release.set()
            timer.cancel()
            handle.cancel()

    async def test_failure_cleans_file_and_preserves_original_exception(self):
        failure = RuntimeError('injected transcription failure')

        def transcribe(path):
            raise failure

        ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
        with self.interaction_patches():
            with self.assertRaises(RuntimeError) as raised:
                await strategies._audio_attempt(ctx, None, None, time.time())
        self.assertIs(raised.exception, failure)
        self.assertFalse(self.audio.exists())
        self.click.assert_not_awaited()
        self.next_audio.assert_not_awaited()

    async def test_cancellation_keeps_file_until_worker_finishes_and_never_clicks(self):
        started = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def transcribe(path):
            loop.call_soon_threadsafe(started.set)
            release.wait(1)
            loop.call_soon_threadsafe(finished.set)
            return 'right'

        ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
        with self.interaction_patches():
            task = asyncio.create_task(strategies._audio_attempt(ctx, None, None, time.time()))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(self.audio.exists(), 'A running decoder still owns this file')
                self.click.assert_not_awaited()
                self.next_audio.assert_not_awaited()
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
                await asyncio.sleep(.05)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.audio.exists())

    async def test_expired_transcription_cannot_send_input(self):
        def transcribe(path):
            time.sleep(.15)
            return 'right'

        ctx = SimpleNamespace(timeout=.03, whisper=SimpleNamespace(transcribe=transcribe))
        with self.interaction_patches(), patch.object(strategies.asyncio, 'sleep', AsyncMock()):
            result = await strategies._audio_attempt(ctx, None, None, time.time())
        await asyncio.to_thread(time.sleep, .2)
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.click.assert_not_awaited()
        self.next_audio.assert_not_awaited()
        self.assertFalse(self.audio.exists())

    async def test_queued_decoder_survives_caller_cancellation_only_to_clean_up(self):
        release = threading.Event()
        decoded = asyncio.Event()
        loop = asyncio.get_running_loop()

        def transcribe(path):
            self.assertTrue(Path(path).exists())
            loop.call_soon_threadsafe(decoded.set)
            return 'right'

        ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
        with ThreadPoolExecutor(max_workers=1) as executor:
            loop.set_default_executor(executor)
            occupant = loop.run_in_executor(None, release.wait, 2)
            with self.interaction_patches():
                task = asyncio.create_task(strategies._audio_attempt(ctx, None, None, time.time()))
                try:
                    await asyncio.sleep(.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(self.audio.exists())
                finally:
                    release.set()
                    await occupant
                    await asyncio.wait_for(decoded.wait(), 2)
                    await asyncio.sleep(.05)
            self.assertFalse(self.audio.exists())
            self.click.assert_not_awaited()
            self.next_audio.assert_not_awaited()

    async def test_download_exhausts_budget_before_decoder_starts(self):
        transcribe = unittest.mock.Mock(return_value='right')

        async def download(frame):
            await asyncio.sleep(.05)
            return str(self.audio)

        ctx = SimpleNamespace(timeout=.01, whisper=SimpleNamespace(transcribe=transcribe))
        with self.interaction_patches(), patch.object(strategies, 'download_audio', download):
            result = await strategies._audio_attempt(ctx, None, None, time.time())
        self.assertFalse(result.success)
        transcribe.assert_not_called()
        self.click.assert_not_awaited()
        self.assertFalse(self.audio.exists())

    async def test_worker_keeps_the_callers_context(self):
        correlation = contextvars.ContextVar('audio_correlation', default='missing')
        correlation.set('this-solve')
        observed = []

        def transcribe(path):
            observed.append(correlation.get())
            return None

        ctx = SimpleNamespace(timeout=30, whisper=SimpleNamespace(transcribe=transcribe))
        with self.interaction_patches(), patch.object(strategies.asyncio, 'sleep', AsyncMock()):
            await strategies._audio_attempt(ctx, None, None, time.time())
        self.assertEqual(observed, ['this-solve'])
        self.assertFalse(self.audio.exists())


class AudioDirectionTests(unittest.TestCase):
    def test_legacy_vision_requires_one_direction(self):
        for answer, expected in [('LEFT', 'left'), ('Right.', 'right'), ('left or right', None),
                                 ('not up', None), ('a bright cup', None), ('NONE', None)]:
            with self.subTest(answer=answer):
                self.assertEqual(rotation._direction_from_answer(answer), expected)

    def test_only_unambiguous_whole_word_directions(self):
        for text, expected in [
            ('Rotate the image to the right.', 'right'), ('TURN LEFT', 'left'),
            ('counterclockwise', 'left'), ('counter-clockwise', 'left'),
            ('anti clockwise', 'left'), ('clockwise', 'right'),
            ('Move up', 'up'), ('move below', 'down'), ('backward', 'down'),
            ('a bright cup', None), ('leftover supplies', None),
            ('a counter with antiques', None), ('left or right', None),
            ("don't rotate left", None), ('do not turn right', None),
            ('never go up', None), ('not down', None), ('', None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(rotation.parse_audio_direction(text), expected)


if __name__ == '__main__':
    unittest.main()
