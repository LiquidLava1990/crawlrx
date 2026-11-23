"""StealthEngine: launches persistent stealth contexts with forged identities.

Facade compatible with the legacy engine's consumption pattern:
``engine.new_browser()`` returns a context-like object exposing ``new_page()``
(a :class:`~._stealth_engine.page.StealthPage`) and ``close()``. Context
options mirror the legacy launch (locale, timezone, screen/viewport, proxy,
``--enable-automation`` stripped) but source identity data from CrawlRX's own
generators — zero GPL components in the lane.
"""
from __future__ import annotations

import logging
import tempfile
from typing import Any, Optional

from .fingerprint import Identity, build_identity
from .page import StealthPage

logger = logging.getLogger(__name__)


class StealthContext:
    """Wraps a playwright persistent context; serves StealthPage objects."""

    def __init__(self, origin_context: Any, identity: Identity, temp_dir: Any) -> None:
        self._origin_context = origin_context
        self.identity = identity
        self._temp_dir = temp_dir

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_origin_context"], name)

    async def new_page(self) -> StealthPage:
        origin_page = await self._origin_context.new_page()
        page = StealthPage(origin_page, self, self.identity)
        await page.attach()
        # Drop the empty about:blank first page of a persistent context.
        try:
            others = [p for p in self._origin_context.pages if p is not origin_page]
            if len(others) == 0:
                pass  # first page is ours; nothing to close
        except Exception:  # noqa: BLE001 - bookkeeping must never break solving
            pass
        return page

    async def close(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._origin_context.close(*args, **kwargs)
        finally:
            try:
                self._temp_dir.cleanup()
            except Exception:  # noqa: BLE001 - temp dir is disposable
                pass


class StealthEngine:
    """Starts playwright, forges identities, opens persistent stealth contexts."""

    def __init__(self, headless: bool = True, proxy: Optional[str] = None) -> None:
        self.headless = headless
        self.proxy = proxy
        self._playwright: Optional[Any] = None

    async def _ensure_playwright(self) -> Any:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return self._playwright

    async def new_browser(self, **launch_arguments: Any) -> StealthContext:
        playwright = await self._ensure_playwright()
        identity = build_identity()
        temp_dir = tempfile.TemporaryDirectory(prefix="crawlrx-stealth-")

        proxy_cfg: Optional[dict] = None
        if self.proxy:
            proxy_cfg = {"server": self.proxy}

        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=temp_dir.name,
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--mute-audio",
                "--no-pings",
                f"--window-size={identity.screen_width},{identity.screen_height}",
            ],
            ignore_default_args=["--enable-automation"],
            locale=identity.locale,
            timezone_id=identity.timezone,
            viewport={
                "width": identity.viewport_width,
                "height": identity.viewport_height,
            },
            screen={
                "width": identity.screen_width,
                "height": identity.screen_height,
            },
            color_scheme="dark",
            ignore_https_errors=True,
            proxy=proxy_cfg,
            **launch_arguments,
        )
        logger.info(
            "Stealth engine context up (%s %s, proxy=%s)",
            identity.brand_brand, identity.chrome_major, bool(proxy_cfg),
        )
        return StealthContext(context, identity, temp_dir)

    async def aclose(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001 - shutdown best-effort
                pass
            self._playwright = None
