"""Regression checks for late hCaptcha widget detection."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from crawlrx.solver_service.solvers.hcaptcha_challenge import HcaptchaChallengeHandler


class FakePage:
    def __init__(self):
        self.widget_visible = False
        self.closed = False
        self.frames = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def is_closed(self):
        return self.closed


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    async def is_visible(self):
        return (self.page.widget_visible and self.selector.endswith(':visible')
                and 'data-hcaptcha-widget-id' in self.selector)


class CheckboxFrame:
    def __init__(self):
        self.visible = False
        self.clicked = False
        self.page = None

    def locator(self, selector):
        return CheckboxLocator(self, selector)

    def is_detached(self):
        return False


class CheckboxLocator:
    def __init__(self, frame, selector):
        self.frame, self.selector, self.first = frame, selector, self

    async def is_visible(self):
        return self.frame.visible and self.selector == '#checkbox:visible'

    async def click(self, timeout):
        self.frame.clicked = True


class HcaptchaWidgetDelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = HcaptchaChallengeHandler.__new__(HcaptchaChallengeHandler)
        self.handler.timeout_ms = 700
        self.page = FakePage()
        async def click(page, frame, checkbox):
            await checkbox.click(timeout=100)
        control = patch('crawlrx.solver_service.solvers.hcaptcha_viewport.click_control', side_effect=click)
        control.start()
        self.addCleanup(control.stop)

    async def test_finds_later_visible_widget(self):
        pending = asyncio.create_task(self.handler._detect_hcaptcha_frame(self.page))
        await asyncio.sleep(.1)
        self.page.widget_visible = True
        frame = await pending
        self.assertIsNotNone(frame)
        self.assertTrue(frame.selector.endswith(':visible'))

    async def test_no_widget_times_out_and_cancellation_propagates(self):
        self.handler.timeout_ms = 100
        self.assertIsNone(await self.handler._detect_hcaptcha_frame(self.page))
        self.handler.timeout_ms = 1000
        pending = asyncio.create_task(self.handler._detect_hcaptcha_frame(self.page))
        await asyncio.sleep(.05)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending

    async def test_legacy_model_check_does_not_claim_image_solving(self):
        with tempfile.TemporaryDirectory() as temp:
            self.handler.model_dir = Path(temp)
            (self.handler.model_dir / 'yolov8n.onnx').write_bytes(b'fixture')
            (self.handler.model_dir / 'resnet.onnx').write_bytes(b'fixture')
            self.assertFalse(self.handler._ensure_models())
            self.assertFalse(self.handler._ensure_hc_models())

    async def test_late_checkbox_is_clicked(self):
        frame = CheckboxFrame()
        pending = asyncio.create_task(self.handler._click_first_checkbox(frame))
        await asyncio.sleep(.1)
        frame.visible = True
        self.assertTrue(await pending)
        self.assertTrue(frame.clicked)

    async def test_delayed_token_is_polled_without_a_challenge_frame(self):
        token = 'fixture-token-1234567890'
        with patch.object(self.handler, '_extract_token', AsyncMock(side_effect=[None, token])):
            self.assertEqual(await self.handler._solve_challenge(self.page), token)


if __name__ == '__main__':
    unittest.main()
