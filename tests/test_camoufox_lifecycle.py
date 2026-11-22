"""Check the real Camoufox manager, page and cookie lifecycle."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawlrx.solver_service.solvers._camoufox_browser import BrowserInstance
from crawlrx.solver_service.solvers.camoufox_solver import CamoufoxSolver


class CamoufoxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_solves_do_not_share_an_instance_page(self):
        active = 0
        peak = 0
        events = []
        class Instance:
            def __init__(self):
                self._lease_lock = asyncio.Lock()
                self.page = None
            async def new_page(self):
                self.page = SimpleNamespace(close=self.close_page)
                events.append('open')
                return self.page
            async def close_page(self):
                events.append('close')
            async def save_cookies(self):
                events.append('save')
        instance = Instance()
        async def get_instance():
            return instance
        solver = CamoufoxSolver({'pool_size': 1})
        async def get_pool():
            return SimpleNamespace(get_instance=get_instance)
        async def navigate(*args):
            return None
        async def handler(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return 'done'
        with patch.object(solver, '_get_pool', side_effect=get_pool), \
             patch.object(solver, '_navigate_or_failure', side_effect=navigate), \
             patch.object(solver, '_captcha_handler_for', return_value=handler):
            self.assertEqual(await asyncio.gather(
                solver._solve_with_browser(None, 'fixture', 'generic'),
                solver._solve_with_browser(None, 'fixture', 'generic')), ['done', 'done'])
        self.assertEqual(peak, 1)
        self.assertEqual(events, ['open', 'save', 'close', 'open', 'save', 'close'])
        self.assertIsNone(instance.page)

    async def test_real_browser_page_cookies_and_close(self):
        with tempfile.TemporaryDirectory() as root:
            cookie_dir = Path(root)
            instance = BrowserInstance(0, headless=True, cookie_dir=cookie_dir)
            self.assertTrue(await asyncio.wait_for(instance.initialize(), timeout=40))
            try:
                page = await asyncio.wait_for(instance.new_page(), timeout=15)
                await page.goto('data:text/html,<title>Camoufox fixture</title>', wait_until='domcontentloaded')
                self.assertEqual(await page.title(), 'Camoufox fixture')
                await page.context.add_cookies([{'name':'fixture', 'value':'yes', 'domain':'example.com', 'path':'/'}])
                await asyncio.wait_for(instance.save_cookies(), timeout=5)
                saved = json.loads(instance.cookie_file.read_text())
                self.assertTrue(any(c.get('name') == 'fixture' for c in saved))
                await page.close()
                instance.page = None
                next_page = await instance.new_page()
                cookies = await next_page.context.cookies('https://example.com')
                self.assertTrue(any(c.get('name') == 'fixture' and c.get('value') == 'yes' for c in cookies))
            finally:
                await asyncio.wait_for(instance.close(), timeout=15)
            self.assertIsNone(instance.browser)
            self.assertIsNone(instance._manager)
            restored = BrowserInstance(0, headless=True, cookie_dir=cookie_dir)
            self.assertTrue(await asyncio.wait_for(restored.initialize(), timeout=40))
            try:
                page = await asyncio.wait_for(restored.new_page(), timeout=15)
                cookies = await page.context.cookies('https://example.com')
                self.assertTrue(any(c.get('name') == 'fixture' and c.get('value') == 'yes' for c in cookies))
            finally:
                await asyncio.wait_for(restored.close(), timeout=15)


if __name__ == '__main__':
    unittest.main(verbosity=2)
