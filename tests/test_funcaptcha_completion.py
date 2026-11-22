"""Real DOM checks for FunCaptcha completion and provider token extraction."""
import time
import unittest

from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers import _funcaptcha_browser as dom
from crawlrx.solver_service.solvers import _funcaptcha_strategies as strategies


class FunCaptchaCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True, args=['--no-sandbox'],
        )
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_incomplete_markup_does_not_mean_solved(self):
        cases = [
            ('empty field', '<input name="fc-token" value="">'),
            ('whitespace field', '<input name="fc-token" value="                 ">'),
            ('cleared field', '<input name="fc-token" value="old-provider-token"><script>document.querySelector("input").value=""</script>'),
            ('script text', '<script>window.statusText="verification complete"</script>'),
            ('hidden status', '<div id="arkose"><p hidden>Verification complete</p></div>'),
            ('negative status', '<div id="arkose">Not verified. Please complete the puzzle.</div>'),
            ('unrelated host status', '<p>Verification complete</p><div id="arkose">Rotate left</div>'),
        ]
        for label, html in cases:
            with self.subTest(case=label):
                await self.page.set_content(html)
                self.assertFalse(await dom.is_solved(self.page))

    async def test_visible_widget_status_and_populated_provider_fields(self):
        for html in [
            '<div id="arkose"><p>Verification complete</p></div>',
            '<input name="fc-token" value="provider-response-token">',
            '<div data-fc-token="provider-response-token"></div>',
            '<input id="arkose-token" value="provider-response-token">',
            '<input name="fc-token"><script>document.querySelector("input").value="provider-response-token"</script>',
        ]:
            with self.subTest(html=html):
                await self.page.set_content(html)
                self.assertTrue(await dom.is_solved(self.page))

    async def test_extracts_only_live_provider_values(self):
        cases = [
            ('unrelated token', '<input type="hidden" name="csrf-token" value="unrelated-token-over-twenty-characters">', None),
            ('data attribute', '<div data-fc-token="provider-response-token"></div>', 'provider-response-token'),
            ('whitespace', '<input name="fc-token" value="                 ">', None),
            ('live property', '<input name="fc-token" value="old-provider-token"><script>document.querySelector("input").value="new-provider-token"</script>', 'new-provider-token'),
            ('cleared property', '<input name="fc-token" value="old-provider-token"><script>document.querySelector("input").value=""</script>', None),
        ]
        for label, html, expected in cases:
            with self.subTest(case=label):
                await self.page.set_content(html)
                self.assertEqual(await dom.extract_token(self.page), expected)

    async def test_visual_status_without_token_is_not_a_token_result(self):
        await self.page.set_content('<div id="arkose">Verification complete</div>')
        result = await strategies._solve_check(
            self.page, self.page, time.time(), 'vision_ai',
        )
        self.assertIsNone(result)

    async def test_completion_message_in_provider_frame_only(self):
        for url, expected in [
            ('https://client-api.arkoselabs.com/challenge', True),
            ('https://example.test/challenge?arkoselabs.com', False),
            ('https://arkoselabs.com.example.test/challenge', False),
        ]:
            with self.subTest(url=url):
                await self.page.route(url, lambda route: route.fulfill(
                    body='<p>Verification complete</p>', content_type='text/html',
                ))
                await self.page.set_content(f'<iframe src="{url}"></iframe>')
                await self.page.locator('iframe').wait_for()
                frame = await self.page.query_selector('iframe')
                scope = await frame.content_frame()
                await scope.wait_for_load_state()
                self.assertEqual(await dom.is_solved(scope), expected)

    async def test_token_result_contains_live_response(self):
        await self.page.set_content(
            '<input name="fc-token" value="provider-response-token">',
        )
        result = await strategies._solve_check(
            self.page, self.page, time.time(), 'vision_ai',
        )
        self.assertTrue(result.success)
        self.assertEqual(result.token, 'provider-response-token')


if __name__ == '__main__':
    unittest.main()
