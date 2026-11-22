"""Botright bridge to Crawl RX's current hCaptcha checkbox/token handler."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from ..solvers.hcaptcha_challenge import HcaptchaChallengeHandler

if TYPE_CHECKING:
    from botright.extended_typing import BrowserContext, Page

_LOGGER = logging.getLogger(__name__)

_applied: bool = False


class hCaptcha:
    def __init__(self, browser: BrowserContext, page: Page) -> None:
        self.browser = browser
        self.page = page

    async def solve_hcaptcha(self, rq_data: Optional[str] = None) -> Optional[str]:
        # This handler cannot safely inject provider-specific request data.
        if rq_data:
            return None
        handler = HcaptchaChallengeHandler({'enabled': True, 'timeout': 35})
        token = await handler._extract_token(self.page)
        if token:
            return token
        frame = None
        for _ in range(10):
            frame = await handler._detect_hcaptcha_frame(self.page)
            if frame is not None:
                break
            await asyncio.sleep(.5)
        if frame is None or not await handler._click_checkbox(self.page, frame):
            return None
        return await handler._solve_challenge(self.page)

    async def get_hcaptcha(self, site_key: Optional[str] = None, rq_data: Optional[str] = None) -> Optional[str]:
        # No synthetic sitekey or token: only solve challenges on the caller's page.
        return None


def apply() -> None:
    """Bind the bridge :class:`hCaptcha` over ``botright.modules.hcaptcha``.

    Replaces the installed hcaptcha_challenger-backed ``hCaptcha`` class so
    that the patched ``Page`` (see ``page.py``) delegates to CrawlRX's own
    handler. Idempotent: calls after a successful patch are no-ops. When the
    pip ``botright`` package is not installed, logs at warning level and
    leaves everything untouched instead of raising.
    """
    global _applied
    if _applied:
        return
    try:
        from botright.modules import hcaptcha as hcaptcha_module
    except ImportError as exc:
        _LOGGER.warning("botright is not installed; hCaptcha bridge patch not applied: %s", exc)
        return
    hcaptcha_module.hCaptcha = hCaptcha
    _applied = True
