"""Cloudflare Turnstile challenge handler.

Multi-strategy approach:
1. Botright auto-solve (built-in Turnstile handling)
2. Camoufox stealth pass-through (perfect fingerprint spoofing)
3. Token harvesting (intercept and extract tokens)
4. Challenge page replay (cookie reuse)

DOM helpers live in `_turnstile_dom`; strategy plumbing (driver factories,
pollers, exception tuples, spec builders, runner) lives in
`_turnstile_strategies`. This file is the orchestrator only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Bootstrap path so `core.sentinel_errors` is importable.

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self, _: str) -> str:
            return "ERR_UNKNOWN"
    ErrCode = _E()  # type: ignore[assignment] because fallback stub when core.sentinel_errors unavailable; stub matches enum surface used by callers
    def emit(*a: object, **kw: object) -> None:
        pass

from playwright.async_api import Page

from ..solver_base import BaseSolver
from ..models import Task, TaskResult

from ._turnstile_dom import ( extract_turnstile_token, inject_token_interceptor, is_challenge_visible, is_turnstile_present, )
from ._turnstile_strategies import ( BROWSER_ERRORS, StrategyAttempt, make_botright_spec, make_camoufox_spec, make_cookie_refresh_spec, make_cookie_replay_spec, make_token_harvest_spec, run_solver_attempt, )


logger = logging.getLogger(__name__)


# Optional dependency probe -- module-level so per-call availability is a
# trivial flag read instead of a try/except wrapper around the import.
try:
    import camoufox as _camoufox_module  # noqa: F401
    _CAMOUFOX_AVAILABLE = True
except ImportError:
    _CAMOUFOX_AVAILABLE = False


class TurnstileResult:
    """Internal result container for Turnstile solving."""

    def __init__(
        self,
        success: bool = False,
        token: Optional[str] = None,
        cookies: Optional[List[Dict]] = None,
        error_message: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        self.success = success
        self.token = token
        self.cookies = cookies or []
        self.error = error_message
        self.strategy = strategy
        self.timestamp = datetime.now(tz=timezone.utc)


class TurnstileSolver(BaseSolver):
    """Cloudflare Turnstile solver with multiple bypass strategies."""

    STRATEGY_TIMEOUT = 60
    TOTAL_TIMEOUT = 180
    TURNSTILE_IFRAME_SELECTORS = [
        'iframe[src*="challenges.cloudflare.com/turnstile"]',
        'iframe[src*="challenges.cloudflare.com/cdn-cgi/challenge-platform"]',
        '.cf-turnstile',
        '[data-cf-turnstile]',
        'input[name="cf-turnstile-response"]',
        'input[name="cf-turnstile"]',
    ]
    TOKEN_SELECTORS = [
        'input[name="cf-turnstile-response"]',
        'input[name="cf-turnstile"]',
        'input[id="cf-turnstile-response"]',
        '[data-turnstile-sitekey]',
    ]
    CF_COOKIES = ['cf_clearance', '__cf_bm', '__cfduid', 'cf_chl_rc_ni']

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.cookie_store: Dict[str, Dict[str, Any]] = {}
        self.cookie_ttl_hours = config.get('cookie_ttl_hours', 24)
        self.camoufox_path = config.get('camoufox_path', None)
        self.headless = config.get('headless', True)
        self.proxy = config.get('proxy', None)

    @property
    def solver_type(self) -> str:
        return "turnstile"

    async def solve(self, task: Task) -> TaskResult:
        """Solve Turnstile using multi-strategy approach."""
        start_time = time.time()
        page_url = task.params.get('pageurl')
        site_key = task.params.get('sitekey') or task.params.get('googlekey')
        if not page_url:
            return TaskResult(
                success=False,
                error_code="ERROR_BAD_PARAMETERS",
                error_message="Missing required parameter: pageurl",
            )
        domain = urlparse(page_url).netloc
        cached_cookies = self._get_cached_cookies(domain)
        success, last_error = await self._run_strategies(
            page_url=page_url,
            site_key=site_key,
            cached_cookies=cached_cookies,
            domain=domain,
            start_time=start_time,
        )
        if success is not None:
            return success
        return TaskResult(
            success=False,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            error_message=last_error or "All solving strategies failed",
        )

    async def _run_strategies(
        self,
        page_url: str,
        site_key: Optional[str],
        cached_cookies: Optional[List[Dict]],
        domain: str,
        start_time: float,
    ) -> Tuple[Optional[TaskResult], Optional[str]]:
        """Iterate strategies, return first success."""
        last_error: Optional[str] = None
        for name, func in self._strategy_table():
            elapsed = time.time() - start_time
            if elapsed >= self.TOTAL_TIMEOUT:
                break
            remaining = min(self.STRATEGY_TIMEOUT, self.TOTAL_TIMEOUT - elapsed)
            attempt = StrategyAttempt(
                strategy_name=name,
                strategy_func=func,
                page_url=page_url,
                site_key=site_key,
                cached_cookies=cached_cookies,
                timeout_s=remaining,
            )
            outcome = await self._attempt_strategy(attempt)
            success, attempt_err = self._process_attempt_outcome(
                outcome=outcome,
                strategy_name=name,
                domain=domain,
                start_time=start_time,
            )
            if success is not None:
                return success, None
            if attempt_err:
                last_error = attempt_err
        return None, last_error

    def _strategy_table(
        self,
    ) -> List[Tuple[str, Callable[..., Awaitable[TurnstileResult]]]]:
        return [
            ("botright", self._try_botright_solve),
            ("camoufox", self._try_camoufox_solve),
            ("token_harvest", self._try_token_harvest),
            ("cookie_replay", self._try_cookie_replay),
        ]

    async def _attempt_strategy(
        self, attempt: StrategyAttempt
    ) -> Tuple[Optional[TurnstileResult], Optional[str]]:
        """Run a single strategy under timeout."""
        logger.info("Turnstile attempt: %s", attempt.strategy_name)
        try:
            result = await asyncio.wait_for(
                attempt.strategy_func(
                    attempt.page_url, attempt.site_key, attempt.cached_cookies,
                ),
                timeout=attempt.timeout_s,
            )
            return result, None
        except asyncio.TimeoutError:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            logger.warning("Strategy %s timed out", attempt.strategy_name)
            return None, f"Strategy {attempt.strategy_name} timed out"
        except BROWSER_ERRORS as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Strategy %s failed: %s", attempt.strategy_name, e)
            return None, str(e)

    def _process_attempt_outcome(
        self,
        outcome: Tuple[Optional[TurnstileResult], Optional[str]],
        strategy_name: str,
        domain: str,
        start_time: float,
    ) -> Tuple[Optional[TaskResult], Optional[str]]:
        """Convert strategy attempt outcome to TaskResult or error text."""
        result, attempt_err = outcome
        if result is None or not (result.success and result.token):
            return None, attempt_err or (result.error if result else None)
        solve_time_ms = int((time.time() - start_time) * 1000)
        logger.info("Turnstile solved via %s", strategy_name)
        if result.cookies:
            self._cache_cookies(domain, result.cookies)
        return TaskResult(
            success=True,
            token=result.token,
            solved_at=datetime.now(tz=timezone.utc),
            solve_time_ms=solve_time_ms,
        ), None

    def health_check(self) -> Dict[str, Any]:
        """Check solver health - at least one strategy must be available."""
        checks = self._collect_capability_checks()
        available = self._available_strategies(checks)
        healthy = bool(available)
        if healthy:
            joined = ', '.join(available)
            message = f"Turnstile solver ready. Available strategies: {joined}"
        else:
            message = (
                "No solving strategies available. "
                "Install at least one: playwright, botright, or camoufox"
            )
        return {
            'healthy': healthy,
            'message': message,
            'details': checks,
            'available_strategies': available,
        }

    def _collect_capability_checks(self) -> Dict[str, bool]:
        checks = {
            'playwright_available': False,
            'botright_available': False,
            'camoufox_available': False,
        }
        try:
            from playwright.async_api import async_playwright  # noqa: F401
            checks['playwright_available'] = True
        except ImportError:
            pass
        try:
            import botright  # noqa: F401
            checks['botright_available'] = True
        except ImportError:
            pass
        try:
            checks['camoufox_available'] = self._check_camoufox_available()
        except (ImportError, RuntimeError, OSError):
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return checks

    @staticmethod
    def _available_strategies(checks: Dict[str, bool]) -> List[str]:
        available: List[str] = []
        if checks.get('playwright_available'):
            available.extend(['token_harvest', 'cookie_replay'])
        if checks.get('botright_available'):
            available.append('botright')
        if checks.get('camoufox_available'):
            available.append('camoufox')
        return available

    async def _run_solver_attempt(self, spec: Any) -> "TurnstileResult":
        """Drive a strategy via the module-level runner."""
        return await run_solver_attempt(self, spec)

    async def _try_botright_solve(
        self,
        page_url: str,
        site_key: Optional[str],
        cached_cookies: Optional[List[Dict]],
    ) -> TurnstileResult:
        """Strategy 1: Botright auto-solve."""
        try:
            import botright  # noqa: F401
        except ImportError:
            return TurnstileResult(
                success=False,
                error_message="Botright not installed (pip install botright)",
            )
        return await self._run_solver_attempt(
            make_botright_spec(page_url, site_key, cached_cookies)
        )

    async def _try_camoufox_solve(
        self,
        page_url: str,
        site_key: Optional[str],
        cached_cookies: Optional[List[Dict]],
    ) -> TurnstileResult:
        """Strategy 2: Camoufox stealth pass-through."""
        if not self._check_camoufox_available():
            return TurnstileResult(
                success=False,
                error_message="Camoufox not available (install camoufox + camoufox-python)",
            )
        return await self._run_solver_attempt(
            make_camoufox_spec(page_url, site_key, cached_cookies)
        )

    async def _try_token_harvest(
        self,
        page_url: str,
        site_key: Optional[str],
        cached_cookies: Optional[List[Dict]],
    ) -> TurnstileResult:
        """Strategy 3: Token harvesting via Playwright + JS interceptor."""
        return await self._run_solver_attempt(
            make_token_harvest_spec(page_url, site_key, cached_cookies)
        )

    async def _try_cookie_replay(
        self,
        page_url: str,
        site_key: Optional[str],
        cached_cookies: Optional[List[Dict]],
    ) -> TurnstileResult:
        """Strategy 4: Cookie replay (with refresh fallback)."""
        domain = urlparse(page_url).netloc
        if not cached_cookies:
            return await self._refresh_cookies(domain, page_url)
        result = await self._run_solver_attempt(
            make_cookie_replay_spec(page_url, site_key, cached_cookies)
        )
        if result.success:
            self._cache_cookies(domain, result.cookies)
            return result
        if result.error == "Cached cookies expired":
            return await self._refresh_cookies(domain, page_url)
        return result

    async def _refresh_cookies(
        self, domain: str, page_url: str
    ) -> TurnstileResult:
        """Refresh Cloudflare cookies by solving a challenge."""
        result = await self._run_solver_attempt(make_cookie_refresh_spec(page_url))
        # cf_clearance cookie alone counts as success even without a token.
        if result.cookies and not result.token:
            has_clearance = any(
                c.get('name') == 'cf_clearance' for c in result.cookies
            )
            if has_clearance:
                result.success = True
                result.token = "COOKIE_AUTH"
                result.error = None
        if result.success:
            self._cache_cookies(domain, result.cookies)
        return result

    async def _extract_turnstile_token(self, page: Page) -> Optional[str]:
        return await extract_turnstile_token(page, self.TOKEN_SELECTORS)

    async def _is_turnstile_present(self, page: Page) -> bool:
        return await is_turnstile_present(page, self.TURNSTILE_IFRAME_SELECTORS)

    async def _is_challenge_visible(self, page: Page) -> bool:
        return await is_challenge_visible(page)

    async def _inject_token_interceptor(self, page: Page) -> None:
        await inject_token_interceptor(page)

    def _get_cached_cookies(self, domain: str) -> Optional[List[Dict]]:
        """Get cached cookies for domain if not expired."""
        entry = self.cookie_store.get(domain)
        if not entry:
            return None
        cached_time = entry.get('timestamp')
        if not cached_time:
            return None
        age = datetime.now(tz=timezone.utc) - cached_time
        if age < timedelta(hours=self.cookie_ttl_hours):
            return entry.get('cookies', [])
        del self.cookie_store[domain]
        return None

    def _cache_cookies(self, domain: str, cookies: List[Dict]) -> None:
        """Cache Cloudflare cookies for domain."""
        cf_cookies = [c for c in cookies if c.get('name') in self.CF_COOKIES]
        if cf_cookies:
            self.cookie_store[domain] = {
                'cookies': cf_cookies,
                'timestamp': datetime.now(tz=timezone.utc),
            }

    @staticmethod
    def _check_camoufox_available() -> bool:
        return _CAMOUFOX_AVAILABLE

    @staticmethod
    def _get_stealth_args() -> List[str]:
        return [
            '--disable-blink-features=AutomationControlled',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-site-isolation-trials',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-default-browser-check',
            '--window-size=1920,1080',
        ]

    @staticmethod
    def _get_stealth_ua() -> str:
        return (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )


def create_solver(config: Dict[str, Any]) -> TurnstileSolver:
    """Create and return a TurnstileSolver instance."""
    return TurnstileSolver(config)
