"""Stealth Browser Solver - thin orchestrator over the split siblings.

Concrete pieces live in:
    * ``_stealth_browser_fingerprint`` -- BrowserFingerprint + UA/locale pools
    * ``_stealth_browser_launch``      -- launch / stealth scripts / cookie I/O
    * ``_stealth_browser_pool``        -- BrowserInstance + StealthBrowserPool

Public surface preserved:
    * :class:`BrowserFingerprint`
    * :class:`BrowserInstance`
    * :class:`StealthBrowserPool`
    * :class:`StealthBrowserSolver`
    * :func:`create_stealth_solver`
"""
from __future__ import annotations




import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from playwright.async_api import Page

from ._solver_helpers import BROAD_EXC
from ._stealth_browser_bootstrap import ErrCode, emit
from ._stealth_browser_fingerprint import BrowserFingerprint  # noqa: F401
from ._stealth_browser_pool import BrowserInstance, StealthBrowserPool
from ..solver_base import BaseSolver

logger = logging.getLogger("stealth_browser")


@dataclass(frozen=True)
class _NavSpec:
    """Bundled navigation parameters for the stealth-browser nav modes."""

    url: str
    wait_for: Optional[str] = None
    timeout: int = 30
    cookies: list = field(default_factory=list)
    max_retries: int = 3


@dataclass(frozen=True)
class _NavTiming:
    """Per-mode delay/scroll knobs for :py:meth:`_navigate_and_settle`."""

    wait_until: str
    scroll_a: tuple
    delay_a: tuple


_CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"]',
    '.g-recaptcha',
    'iframe[src*="hcaptcha.com"], .h-captcha',
    'iframe[src*="turnstile"], .cf-turnstile',
    'iframe[src*="challenge"], [data-sitekey]',
    '.captcha', '#captcha', '[class*="captcha"]',
)

_CAPTCHA_KEYWORDS = (
    'recaptcha', 'hcaptcha', 'turnstile', "i'm not a robot", 'captcha',
)


class StealthBrowserSolver(BaseSolver):
    """Stealth browser solver implementing BaseSolver interface."""

    _STEALTH_TIMING = _NavTiming(
        wait_until="domcontentloaded", scroll_a=(200, 600), delay_a=(500, 1500),
    )
    _AVOIDANCE_TIMING = _NavTiming(
        wait_until="networkidle", scroll_a=(300, 700), delay_a=(800, 2000),
    )

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        if config is None:
            config = {}
        super().__init__(config)
        proxy = config.get('proxy')
        self.pool = StealthBrowserPool(proxy=proxy)
        self._closed = False

    @property
    def solver_type(self) -> str:
        return "stealth_browser"

    async def _detect_captcha(self, page: Page) -> bool:
        """Detect if page contains CAPTCHA elements."""
        for selector in _CAPTCHA_SELECTORS:
            try:
                element = await page.query_selector(selector)
                if element:
                    return True
            except BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        content_lower = (await page.content()).lower()
        return any(keyword in content_lower for keyword in _CAPTCHA_KEYWORDS)

    @staticmethod
    def _nav_spec_from_task(task: dict) -> Optional[_NavSpec]:
        """Build a :class:`_NavSpec` from a raw task dict, or ``None`` if invalid."""
        url = task.get("url")
        if not url:
            return None
        return _NavSpec(
            url=url,
            wait_for=task.get("wait_for"),
            timeout=task.get("timeout", 30),
            cookies=task.get("cookies", []),
            max_retries=task.get("max_retries", 3),
        )

    async def _apply_cookies(self, instance: Any, cookies: list) -> None:
        """Idempotent cookie inject (no-op when ``cookies`` is empty)."""
        if cookies:
            await instance.context.add_cookies(cookies)

    async def _wait_for_optional(
        self, page: Page, wait_for: Optional[str], timeout: int,
    ) -> None:
        """Wait for selector if caller provided one; otherwise return."""
        if wait_for:
            await page.wait_for_selector(wait_for, timeout=timeout * 1000)

    async def _capture_page_state(self, page: Page, instance: Any) -> Dict[str, Any]:
        """Snapshot content + cookies + title -- shared between both nav modes."""
        return {
            "content": await page.content(),
            "cookies": await instance.context.cookies(),
            "title": await page.title(),
        }

    async def _navigate_and_settle(
        self, page: Page, instance: Any, spec: _NavSpec, timing: _NavTiming,
    ) -> Any:
        """Shared nav core: cookies + initial goto + first scroll/delay/wait_for."""
        await self._apply_cookies(instance, spec.cookies)
        response = await page.goto(
            spec.url, wait_until=timing.wait_until, timeout=spec.timeout * 1000,
        )
        await self.pool.human_like_scroll(page, random.randint(*timing.scroll_a))
        await self.pool.human_like_delay(*timing.delay_a)
        await self._wait_for_optional(page, spec.wait_for, spec.timeout)
        return response

    @staticmethod
    def _build_success(
        page: Page,
        snapshot: Dict[str, Any],
        response: Any,
        *,
        captcha_field: bool,
    ) -> dict:
        """Compose the OK-path return dict; ``captcha_field`` toggles the captcha key."""
        out = {
            "success": True,
            "status": response.status if response else None,
            "url": page.url,
            "title": snapshot["title"],
            "content": snapshot["content"],
            "cookies": snapshot["cookies"],
            "error": None,
        }
        if captcha_field:
            out["captcha_detected"] = False
        return out

    async def _stealth_navigate(self, task: dict) -> dict:
        """Navigate to URL with stealth - no CAPTCHA detection."""
        spec = self._nav_spec_from_task(task)
        if spec is None:
            return {"success": False, "error": "No URL provided"}

        instance = await self.pool.get_instance()
        try:
            page = await instance.context.new_page()
            try:
                return await self._stealth_navigate_run(page, instance, spec)
            finally:
                await page.close()
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            await self.pool.release_instance(instance, healthy=False)
            return {"success": False, "error": str(e)}

    async def _stealth_navigate_run(
        self, page: Page, instance: Any, spec: _NavSpec,
    ) -> dict:
        """Inner body of :py:meth:`_stealth_navigate` (depth=2 in caller)."""
        await self.pool.human_like_delay(200, 800)
        response = await self._navigate_and_settle(
            page, instance, spec, self._STEALTH_TIMING,
        )
        await self.pool.human_like_scroll(page, random.randint(100, 400))
        await self.pool.human_like_delay(300, 1000)
        snapshot = await self._capture_page_state(page, instance)
        await self.pool.release_instance(instance, healthy=True)
        result = self._build_success(page, snapshot, response, captcha_field=False)
        # Stealth-nav success is the response.ok bit, not blanket True.
        result["success"] = bool(response and response.ok)
        return result

    async def _captcha_avoidance(self, task: dict) -> dict:
        """Navigate with CAPTCHA avoidance - retry if CAPTCHA detected."""
        spec = self._nav_spec_from_task(task)
        if spec is None:
            return {"success": False, "error": "No URL provided"}
        for attempt in range(spec.max_retries):
            outcome = await self._captcha_avoidance_attempt(attempt, spec)
            if outcome is not None:
                return outcome
        return {"success": False, "error": "All retry attempts exhausted"}

    async def _captcha_avoidance_attempt(
        self, attempt: int, spec: _NavSpec,
    ) -> Optional[dict]:
        """One retry of CAPTCHA-aware navigation. ``None`` = retry next attempt."""
        instance = await self.pool.get_instance()
        try:
            page = await instance.context.new_page()
            try:
                return await self._captcha_avoidance_run(page, instance, attempt, spec)
            finally:
                if not page.is_closed():
                    await page.close()
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Attempt %s failed: %s", attempt + 1, e)
            await self.pool.release_instance(instance, healthy=False)
            if attempt == spec.max_retries - 1:
                return {"success": False, "error": str(e)}
            return None

    async def _captcha_avoidance_run(
        self,
        page: Page,
        instance: Any,
        attempt: int,
        spec: _NavSpec,
    ) -> Optional[dict]:
        """Inner body of one CAPTCHA-avoidance attempt (depth <= 3)."""
        if attempt > 0:
            await self.pool.human_like_delay(1000, 3000)
            await self.pool.human_like_mouse_move(page, "body")
        response = await self._navigate_and_settle(
            page, instance, spec, self._AVOIDANCE_TIMING,
        )
        if await self._detect_captcha(page):
            return await self._on_captcha_detected(
                page, instance, attempt, spec.max_retries,
            )
        snapshot = await self._capture_page_state(page, instance)
        await self.pool.release_instance(instance, healthy=True)
        return self._build_success(page, snapshot, response, captcha_field=True)

    async def _on_captcha_detected(
        self,
        page: Page,
        instance: Any,
        attempt: int,
        max_retries: int,
    ) -> Optional[dict]:
        """CAPTCHA hit: rotate instance and either retry or surface failure."""
        logger.warning(
            "CAPTCHA detected on attempt %s, rotating instance", attempt + 1,
        )
        await page.close()
        await self.pool.release_instance(instance, healthy=False)
        if attempt < max_retries - 1:
            await self.pool._start_instance(instance)
            return None
        return {
            "success": False,
            "error": "CAPTCHA detected after max retries",
            "captcha_detected": True,
        }

    async def solve(self, task: dict) -> dict:
        """Main entry point for solving tasks."""
        if self._closed:
            return {"success": False, "error": "Solver has been closed"}
        mode = task.get("mode", "stealth_navigate")
        if not self.pool._initialized:
            await self.pool.initialize()
        if mode == "stealth_navigate":
            return await self._stealth_navigate(task)
        if mode == "captcha_avoidance":
            return await self._captcha_avoidance(task)
        return {"success": False, "error": f"Unknown mode: {mode}"}

    async def health_check(self) -> Dict[str, Any]:
        """Check if the solver is healthy."""
        try:
            if not self.pool._initialized:
                await self.pool.initialize()
            healthy_count = sum(
                1 for inst in self.pool.instances
                if inst.is_ready
                and inst.health_failures < self.pool.MAX_HEALTH_FAILURES
            )
            return {
                'healthy': healthy_count >= 2,  # At least 2/3 must be healthy
                'message': f'{healthy_count}/{len(self.pool.instances)} instances ready',
                'details': {
                    'healthy_instances': healthy_count,
                    'pool_size': len(self.pool.instances),
                },
            }
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Health check failed: %s", e)
            return {
                'healthy': False,
                'message': f'Health check failed: {str(e)}',
            }

    async def close(self) -> None:
        """Clean up resources."""
        if not self._closed:
            await self.pool.close()
            self._closed = True


async def create_stealth_solver(proxy: Optional[str] = None) -> StealthBrowserSolver:
    """Create and initialize a stealth browser solver."""
    solver = StealthBrowserSolver({'proxy': proxy})
    await solver.pool.initialize()
    return solver
