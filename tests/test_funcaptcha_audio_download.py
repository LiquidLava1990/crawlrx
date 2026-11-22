"""Failed audio downloads must release their temporary files."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from crawlrx.solver_service.solvers import _funcaptcha_browser as dom


class AudioDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_failure_does_not_leave_a_file(self):
        original = tempfile.NamedTemporaryFile
        failure = OSError('session unavailable')
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(dom.tempfile, 'NamedTemporaryFile', side_effect=lambda **kw: original(dir=directory, **kw)),
                  patch('aiohttp.ClientSession', side_effect=failure)):
                with self.assertRaises(OSError) as raised:
                    await dom._save_audio_to_temp('https://fixture.invalid/audio')
            self.assertIs(raised.exception, failure)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_read_cancellation_does_not_leave_a_file(self):
        original = tempfile.NamedTemporaryFile
        response = SimpleNamespace(status=200, read=AsyncMock(side_effect=asyncio.CancelledError))
        request = MagicMock()
        request.__aenter__.return_value = response
        session = MagicMock()
        session.get.return_value = request
        client = MagicMock()
        client.__aenter__.return_value = session
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(dom.tempfile, 'NamedTemporaryFile', side_effect=lambda **kw: original(dir=directory, **kw)),
                  patch('aiohttp.ClientSession', return_value=client)):
                with self.assertRaises(asyncio.CancelledError):
                    await dom._save_audio_to_temp('https://fixture.invalid/audio')
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_successful_download_keeps_file_for_decoder(self):
        response = SimpleNamespace(status=200, read=AsyncMock(return_value=b'recorded audio'))
        request = MagicMock()
        request.__aenter__.return_value = response
        session = MagicMock()
        session.get.return_value = request
        client = MagicMock()
        client.__aenter__.return_value = session
        with patch('aiohttp.ClientSession', return_value=client):
            path = await dom._save_audio_to_temp('https://fixture.invalid/audio')
        try:
            self.assertEqual(Path(path).read_bytes(), b'recorded audio')
        finally:
            Path(path).unlink()


if __name__ == '__main__':
    unittest.main()
