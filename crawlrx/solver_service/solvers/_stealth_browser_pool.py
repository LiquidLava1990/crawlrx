"""Stealth-browser instance + pool. Lifecycle coordinator only.

Concrete launch / cookie / stealth-script work lives in
``_stealth_browser_launch.py`` so this file stays a thin coordinator.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from ._solver_helpers import BROAD_EXC
from ._stealth_browser_bootstrap import ErrCode, emit
from ._stealth_browser_fingerprint import BrowserFingerprint
from ._stealth_browser_launch import (
    launch_instance,
    save_cookies,
)

logger = logging.getLogger("stealth_browser")


@dataclass
class BrowserInstance:
    """Managed browser instance with health tracking."""

    id: int
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    fingerprint: Optional[BrowserFingerprint] = None
    last_used: Optional[datetime] = None
    health_failures: int = 0
    is_ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StealthBrowserPool:
    """Pool of stealth browser instances with round-robin assignment."""

    POOL_SIZE = 3
    MAX_HEALTH_FAILURES = 3
    COOKIE_FILE = Path("stealth_cookies.json")

    def __init__(self, proxy: Optional[str] = None) -> None:
        self.proxy = proxy
        self.instances: List[BrowserInstance] = []
        self._round_robin_index = 0
        self._playwright: Any = None
        self._initialized = False
        self._background_tasks: set = set()

    async def initialize(self) -> None:
        """Initialize the browser pool."""
        if self._initialized:
            return
        self._playwright = await async_playwright().start()
        for i in range(self.POOL_SIZE):
            instance = BrowserInstance(id=i)
            await self._start_instance(instance)
            self.instances.append(instance)
        self._initialized = True
        logger.info(
            "Stealth browser pool initialized with %s instances", self.POOL_SIZE,
        )

    async def _start_instance(self, instance: BrowserInstance) -> None:
        """Start a browser instance with stealth configuration."""
        async with instance.lock:
            try:
                fingerprint = await launch_instance(
                    instance,
                    playwright=self._playwright,
                    proxy=self.proxy,
                    background_tasks=self._background_tasks,
                )
                logger.info(
                    "Browser instance %s started with viewport %sx%s",
                    instance.id,
                    fingerprint.viewport_width,
                    fingerprint.viewport_height,
                )
            except BROAD_EXC as e:
                emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
                logger.error(
                    "Failed to start browser instance %s: %s", instance.id, e,
                )
                instance.is_ready = False
                instance.health_failures += 1
                raise e

    async def get_instance(self) -> BrowserInstance:
        """Get a browser instance using round-robin."""
        if not self._initialized:
            await self.initialize()
        for _ in range(self.POOL_SIZE):
            idx = self._round_robin_index % self.POOL_SIZE
            self._round_robin_index += 1
            instance = self.instances[idx]
            async with instance.lock:
                if (
                    instance.health_failures >= self.MAX_HEALTH_FAILURES
                    or not instance.is_ready
                ):
                    logger.warning("Restarting unhealthy instance %s", instance.id)
                    await self._start_instance(instance)
                if instance.is_ready:
                    instance.last_used = datetime.now(tz=timezone.utc)
                    return instance
        raise RuntimeError("No healthy browser instances available")

    async def release_instance(
        self, instance: BrowserInstance, healthy: bool = True,
    ) -> None:
        """Release instance back to pool."""
        async with instance.lock:
            if healthy:
                instance.health_failures = 0
                await save_cookies(instance, self.COOKIE_FILE)
            else:
                instance.health_failures += 1
                logger.warning(
                    "Instance %s health failure count: %s",
                    instance.id,
                    instance.health_failures,
                )

    async def human_like_delay(self, min_ms: int = 100, max_ms: int = 500) -> None:
        """Random delay to mimic human behavior."""
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)

    async def human_like_scroll(self, page: Page, amount: Optional[int] = None) -> None:
        """Human-like scrolling with variable speed."""
        if amount is None:
            amount = random.randint(300, 800)
        steps = random.randint(3, 8)
        step_size = amount // steps
        for _ in range(steps):
            await page.mouse.wheel(0, step_size)
            await self.human_like_delay(50, 200)

    async def human_like_mouse_move(self, page: Page, selector: str) -> object:
        """Human-like mouse movement to element."""
        element = await page.query_selector(selector)
        if not element:
            return False
        box = await element.bounding_box()
        if not box:
            return False
        target_x = box["x"] + box["width"] / 2 + random.randint(-10, 10)
        target_y = box["y"] + box["height"] / 2 + random.randint(-5, 5)
        current_x = random.randint(100, 500)
        current_y = random.randint(100, 500)
        steps = random.randint(5, 15)
        for i in range(steps):
            t = (i + 1) / steps
            x = current_x + (target_x - current_x) * t + random.randint(-20, 20)
            y = current_y + (target_y - current_y) * t + random.randint(-10, 10)
            await page.mouse.move(x, y)
            await self.human_like_delay(20, 80)
        return True

    async def _close_one_instance(self, instance: BrowserInstance) -> None:
        """Close one stealth browser instance, swallowing recoverable shutdown errors."""
        async with instance.lock:
            try:
                if instance.context:
                    await save_cookies(instance, self.COOKIE_FILE)
                    await instance.context.close()
                if instance.browser:
                    await instance.browser.close()
            except BROAD_EXC as e:
                emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
                logger.warning(
                    "Error closing instance %s: %s", instance.id, e,
                )
                return None  # explicit recovery: cleanup non-fatal; remaining instances still drained

    async def close(self) -> None:
        """Close all browser instances."""
        for instance in self.instances:
            await self._close_one_instance(instance)
        if self._playwright:
            await self._playwright.stop()
        self._initialized = False
        logger.info("Stealth browser pool closed")
