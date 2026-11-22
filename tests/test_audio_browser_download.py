"""Audio adapters must keep the current browser context and independent files."""
import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _browser_audio_download as shared
from crawlrx.solver_service.solvers import _funcaptcha_browser as fun
from crawlrx.solver_service.solvers import _recaptcha_audio_browser as recap


PAYLOAD = b'audio owned by this fixture'
HTML = '''<base href="/recordings/"><div id=arkose>
<a id=audio-source data-test-id=audio-link href="audio.wav">Recording</a></div>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/recordings/audio.wav':
            authorized = 'audio-fixture=active' in self.headers.get('Cookie', '')
            self.send_response(200 if authorized else 403)
            body = PAYLOAD if authorized else b'no browser session'
            self.send_header('Content-Type', 'audio/wav')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Set-Cookie', 'audio-fixture=active; Path=/; SameSite=Lax')
            body = (('<iframe src="/recaptcha/api2/bframe"></iframe>' if self.path == '/host' else HTML)).encode()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BrowserAudioDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()
        await self.page.goto(self.origin + '/widget')
        self.files = set()

    async def asyncTearDown(self):
        for path in self.files:
            Path(path).unlink(missing_ok=True)
        await self.browser.close()
        await self.playwright.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=2)

    async def assert_fun_download(self):
        path = await fun.download_audio(self.page)
        if path:
            self.files.add(path)
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_bytes(), PAYLOAD)

    async def test_relative_link_uses_document_base(self):
        await self.assert_fun_download()

    async def test_absolute_link_keeps_browser_cookie(self):
        await self.page.locator('a').evaluate('(el, url) => el.href=url', self.origin + '/recordings/audio.wav')
        await self.assert_fun_download()

    async def test_blob_stays_in_owning_document(self):
        await self.page.evaluate('(data) => {document.querySelector("a").href=URL.createObjectURL(new Blob([data]))}', PAYLOAD.decode())
        await self.assert_fun_download()

    async def test_data_audio_is_downloadable(self):
        await self.page.locator('a').evaluate('(el, data) => el.href="data:audio/wav,"+encodeURIComponent(data)', PAYLOAD.decode())
        await self.assert_fun_download()

    async def test_recaptcha_iframe_keeps_base_and_browser_cookie(self):
        await self.page.goto(self.origin + '/host')
        with patch.object(recap, 'random_delay', AsyncMock()):
            path = await recap.get_audio_challenge(self.page)
        self.files.add(path)
        self.assertEqual(Path(path).read_bytes(), PAYLOAD)

    async def test_recaptcha_reads_audio_src_property(self):
        await self.page.set_content('<audio id=audio-source src="https://fixture.invalid/audio.wav"></audio>')
        element = await self.page.query_selector('#audio-source')
        self.assertEqual(await recap._read_audio_url('#audio-source', element), 'https://fixture.invalid/audio.wav')

    async def test_recaptcha_document_owned_recordings(self):
        await self.page.goto(self.origin + '/host')
        frame = await recap.resolve_challenge_frame(self.page)
        for kind in ['blob', 'data']:
            with self.subTest(kind=kind):
                await frame.evaluate('''({kind,data}) => {
                    document.querySelector('a').href=kind==='blob'
                        ? URL.createObjectURL(new Blob([data]))
                        : 'data:audio/wav,'+encodeURIComponent(data);
                }''', {'kind': kind, 'data': PAYLOAD.decode()})
                with patch.object(recap, 'random_delay', AsyncMock()):
                    path = await recap.get_audio_challenge(self.page)
                self.files.add(path)
                self.assertEqual(Path(path).read_bytes(), PAYLOAD)


class AudioFileConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_second_downloads_have_distinct_files(self):
        client_class = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=request.url.path.encode()))
        paths = []
        try:
            with (patch.object(recap, 'time', SimpleNamespace(time=lambda: 123456789), create=True),
                  patch.object(recap.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(transport=transport, **kw))):
                paths = await asyncio.gather(*(recap._download_audio(f'https://fixture.invalid/{n}') for n in range(10)))
            self.assertEqual(len(set(paths)), 10)
            self.assertEqual([Path(p).read_bytes() for p in paths], [f'/{n}'.encode() for n in range(10)])
        finally:
            for path in set(paths):
                Path(path).unlink(missing_ok=True)

    async def test_partial_file_write_preserves_error_and_cleans_file(self):
        original = tempfile.NamedTemporaryFile
        failure = OSError('injected write failure')
        with tempfile.TemporaryDirectory() as directory:
            def create(**kwargs):
                output = original(dir=directory, **kwargs)
                output.write = Mock(side_effect=failure)
                return output

            with patch.object(shared.tempfile, 'NamedTemporaryFile', side_effect=create):
                with self.assertRaises(OSError) as raised:
                    shared.write_audio_temp(PAYLOAD, 'write_failure_')
            self.assertIs(raised.exception, failure)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
