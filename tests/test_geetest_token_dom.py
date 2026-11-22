"""GeeTest must return provider fields, never a success indicator or unrelated token."""
import unittest
from types import SimpleNamespace

from playwright.async_api import async_playwright
from crawlrx.solver_service.solvers._geetest_strategies import _extract_token_once


class GeeTestTokenDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_and_ambiguous_responses(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                host = SimpleNamespace(page=page)
                cases = [
                    ('indicator only', '<div class="geetest_success">Success</div>', None),
                    ('radar only', '<div class="geetest_radar">Loading</div>', None),
                    ('unrelated input', '<input name="captcha_answer" value="unrelated">', None),
                    ('other provider', '<script>window.grecaptcha={getResponse:()=>"other-provider"}</script>', None),
                    ('literal sentinel', '<script>window.geetest_token="success"</script>', None),
                    ('partial object', '<script>window.geetest_token={validate:"object"}</script>', None),
                    ('partial form', '<input name="geetest_validate" value="provider-validation">', None),
                    ('scalar token', '<script>window.geetest_token="provider-token"</script>', None),
                    ('ambiguous forms', '<input name="geetest_challenge" value="c"><input name="geetest_validate" value="v"><input name="geetest_validate" value="v2"><input name="geetest_seccode" value="s">', None),
                    ('v3 form', '<form><input name="geetest_challenge" value="c"><input name="geetest_validate" value="v"><input name="geetest_seccode" value="s"></form>', '{"challenge":"c","validate":"v","seccode":"s"}'),
                    ('v3 object', '<script>window.geetest_token={challenge:"c",validate:"v",seccode:"s"}</script>', '{"challenge":"c","validate":"v","seccode":"s"}'),
                ('partial object and complete form', '<script>window.geetest_token={validate:"v"}</script><form><input name="geetest_challenge" value="c"><input name="geetest_validate" value="v"><input name="geetest_seccode" value="s"></form>', '{"challenge":"c","validate":"v","seccode":"s"}'),
                ]
                for label, html, expected in cases:
                    await page.goto('about:blank')
                    await page.set_content(html)
                    actual = await _extract_token_once(host)
                    with self.subTest(case=label):
                        self.assertEqual(actual, expected)
            finally:
                await browser.close()

if __name__ == '__main__':
    unittest.main()
