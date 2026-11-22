"""Cancellation must reach the router rather than be treated as a solver failure."""
import asyncio
import unittest
from types import SimpleNamespace

from crawlrx.solver_service.solvers._recaptcha_audio_browser import is_challenge_visible
from crawlrx.solver_service.solvers._solver_helpers import BROAD_EXC


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recaptcha_probe_propagates_cancellation(self):
        async def stalled(_selector):
            await asyncio.Event().wait()
        frame = SimpleNamespace(url='https://example.org/recaptcha/api2/bframe', query_selector=stalled)
        work = asyncio.create_task(is_challenge_visible(SimpleNamespace(frames=[frame])))
        await asyncio.sleep(0)
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertNotIn(asyncio.CancelledError, BROAD_EXC)


if __name__ == '__main__':
    unittest.main(verbosity=2)
