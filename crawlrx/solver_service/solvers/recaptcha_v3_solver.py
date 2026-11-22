"""reCAPTCHA v3 solver using Playwright CDP stealth automation.

reCAPTCHA v3 is score-based (no visual challenge). It runs silently in the
background and returns a token with a score (0.0-1.0). High scores indicate
human-like behavior.

Strategy:
  1. Connect to a stealth CDP browser (Playwright via agent port)
  2. Navigate to target page with human-like timing
  3. Inject and execute grecaptcha.execute() with the site key + action
  4. Extract the token from the response
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# core/sentinel_errors path resolver (project convention).
try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self, _: str) -> str:
            return "ERR_UNKNOWN"
    ErrCode = _E()  # type: ignore[assignment] because fallback stub when core.sentinel_errors unavailable; stub matches enum surface used by callers

    def emit(*a: object, **kw: object) -> None:
        return None


from ._solver_helpers import BROAD_EXC

from ..solver_base import BaseSolver, SolverError
from ..models import Task, TaskResult


logger = logging.getLogger(__name__)


CHROME_PATH = _os.environ.get(
    "CHROME_EXE",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
)

_GRECAPTCHA_READY_JS = (
    '() => typeof grecaptcha !== "undefined" && '
    'typeof grecaptcha.execute === "function"'
)


@dataclass(frozen=True)
class _ResultSpec:
    """Bundle of fields needed to build a TaskResult."""
    success: bool
    start_time: float
    token: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _build_result(spec: _ResultSpec) -> TaskResult:
    return TaskResult(
        success=spec.success,
        token=spec.token,
        error_code=spec.error_code,
        error_message=spec.error_message,
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=int((time.time() - spec.start_time) * 1000),
    )


def _extract_solve_params(task: Task) -> tuple[str, str, str]:
    params = task.params if hasattr(task, "params") else {}
    url = params.get("pageurl") or params.get("url", "")
    site_key = params.get("sitekey") or params.get("googlekey", "")
    action = params.get("action", "homepage")
    return url, site_key, action


async def _wait_for_grecaptcha(page: Any) -> bool:
    for _ in range(30):
        if await page.evaluate(_GRECAPTCHA_READY_JS):
            return True
        await asyncio.sleep(0.5)
    return False


async def _inject_recaptcha_script(page: Any, site_key: str) -> None:
    script = (
        '() => { return new Promise((resolve) => {'
        ' const s = document.createElement("script");'
        f' s.src = "https://www.google.com/recaptcha/api.js?render={site_key}";'
        ' s.onload = () => resolve(true);'
        ' s.onerror = () => resolve(false);'
        ' document.head.appendChild(s);'
        ' }); }'
    )
    await page.evaluate(script)
    await asyncio.sleep(2)


async def _simulate_human(page: Any) -> None:
    left, top, width, height = await page.evaluate("() => {const v=visualViewport;return [v?.offsetLeft??0,v?.offsetTop??0,v?.width??innerWidth,v?.height??innerHeight]}")
    if width <= 0 or height <= 0:
        return
    await page.mouse.move(
        left + random.uniform(.15, .85) * width,
        top + random.uniform(.15, .85) * height,
        steps=random.randint(5, 12),
    )
    await asyncio.sleep(random.uniform(0.5, 1.5))


async def _execute_grecaptcha(page: Any, site_key: str, action: str) -> Any:
    script = (
        '() => { return new Promise((resolve, reject) => {'
        ' if (typeof grecaptcha === "undefined") { reject("grecaptcha not loaded"); return; }'
        ' grecaptcha.ready(() => {'
        f' grecaptcha.execute("{site_key}", {{action: "{action}"}})'
        ' .then(token => resolve(token))'
        ' .catch(err => reject(err.toString())); }); }); }'
    )
    return await page.evaluate(script)


class ReCaptchaV3Solver(BaseSolver):
    """reCAPTCHA v3 solver -- score-based, no visual challenge."""

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.max_retries: int = config.get("max_retries", 3)
        self.cdp_port: int = config.get("cdp_port", 9234)
        self.headless: bool = config.get("headless", True)
        self.chrome_path: str = config.get("chrome_path", CHROME_PATH)

    @property
    def solver_type(self) -> str:
        return "recaptcha_v3"

    async def _launch_browser(self, p: Any) -> Any:
        return await p.chromium.launch(
            executable_path=self.chrome_path if _os.path.isfile(self.chrome_path) else None,
            headless=self.headless,
            args=list(_LAUNCH_ARGS),
        )

    @staticmethod
    async def _new_stealth_context(browser: Any) -> Any:
        return await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=_USER_AGENT,
        )

    @staticmethod
    async def _drive_page(page: Any, url: str, site_key: str, action: str) -> Optional[str]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if not await _wait_for_grecaptcha(page):
            await _inject_recaptcha_script(page, site_key)
        await _simulate_human(page)
        token = await _execute_grecaptcha(page, site_key, action)
        if token and len(str(token)) > 20:
            logger.info("reCAPTCHA v3 token obtained (%s chars)", len(token))
            return token
        return None

    async def _attempt_solve(self, url: str, site_key: str, action: str) -> Optional[str]:
        """Single solve attempt using Playwright."""
        if not HAS_PLAYWRIGHT:
            raise SolverError("ERROR_NO_PLAYWRIGHT", "playwright not installed")
        async with async_playwright() as p:
            browser = await self._launch_browser(p)
            context = await self._new_stealth_context(browser)
            page = await context.new_page()
            try:
                return await self._drive_page(page, url, site_key, action)
            finally:
                await page.close()
                await context.close()
                await browser.close()

    async def _try_one_attempt(
        self, url: str, site_key: str, action: str, attempt: int,
    ) -> tuple[Optional[str], Optional[str]]:
        """One attempt -> (token_or_none, last_error_or_none)."""
        try:
            token = await self._attempt_solve(url, site_key, action)
            if token:
                return token, None
            return None, "No token returned from grecaptcha.execute()"
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.warning("Attempt %s failed: %s", attempt, e)
            return None, str(e)

    async def solve(self, task: Task) -> TaskResult:
        start_time = time.time()
        url, site_key, action = _extract_solve_params(task)
        if not url or not site_key:
            return _build_result(_ResultSpec(
                success=False, start_time=start_time,
                error_code="ERROR_BAD_PARAMETERS",
                error_message="pageurl and sitekey are required for reCAPTCHA v3",
            ))
        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 1):
            token, err = await self._try_one_attempt(url, site_key, action, attempt)
            if token:
                return _build_result(_ResultSpec(
                    success=True, start_time=start_time, token=token,
                ))
            last_error = err
            if attempt < self.max_retries:
                await asyncio.sleep(random.uniform(1.5, 3.0))
        return _build_result(_ResultSpec(
            success=False, start_time=start_time,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            error_message=f"All {self.max_retries} attempts failed. Last: {last_error}",
        ))

    def health_check(self) -> Dict[str, Any]:
        if not HAS_PLAYWRIGHT:
            return {"healthy": False, "message": "playwright not installed"}
        chrome_ok = _os.path.isfile(self.chrome_path) if self.chrome_path else False
        return {
            "healthy": True,
            "message": "reCAPTCHA v3 solver ready" + ("" if chrome_ok else " (using system Chrome)"),
            "details": {
                "solver_type": self.solver_type,
                "chrome_path": self.chrome_path if chrome_ok else "system default",
                "max_retries": self.max_retries,
            },
        }


def create_solver(config: Dict[str, Any]) -> ReCaptchaV3Solver:
    return ReCaptchaV3Solver(config)
