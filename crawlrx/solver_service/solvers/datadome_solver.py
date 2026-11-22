"""DataDome challenge handler — thin orchestrator.

Multi-strategy bypass for DataDome protection. Concrete strategy
implementations live in sibling modules:

  * ``_datadome_browser`` — cookie store + fingerprint manager
  * ``_datadome_tls`` — cookie reuse + tls-client iteration
  * ``_datadome_strategies`` — Camoufox + Botright + Playwright (stealth)

Detection indicators:
  - ``datadome.co`` in page source
  - ``dd_s`` cookie presence
  - 403 with DataDome headers (``X-DataDome``, ``Server: DataDome``)
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import Browser, BrowserContext


# --- ErrCode/emit shim -----------------------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass


from ..solver_base import BaseSolver  # noqa: E402

try:
    from ..models import Task, TaskResult  # noqa: E402
except ImportError:
    from datetime import datetime as _dt
    from pydantic import BaseModel  # noqa: E402

    class TaskResult(BaseModel):  # type: ignore[no-redef]
        success: bool
        token: Optional[str] = None
        error_code: Optional[str] = None
        error_message: Optional[str] = None
        solved_at: Optional[_dt] = None
        solve_time_ms: Optional[int] = None

    class Task(BaseModel):  # type: ignore[no-redef]
        id: str
        params: Dict[str, Any]


# --- Sibling helpers -------------------------------------------------------

from ._datadome_browser import DataDomeCookieStore, FingerprintManager  # noqa: E402
from ._datadome_strategies import (  # noqa: E402
    try_botright_strategy,
    try_camoufox_strategy,
    try_playwright_strategy,
)
from ._datadome_tls import (  # noqa: E402
    try_cookie_strategy,
    try_tls_client_strategy,
)


logger = logging.getLogger(__name__)


# Detection patterns (module-level constant; class re-exposes via class attr).
_DATADOME_INDICATORS = (
    r"datadome\.co",
    r"dd___jsl",
    r"dd_s",
    r"dd_c",
    r"X-DataDome",
    r"Server:\s*DataDome",
    r"datadome-captcha",
    r"captcha-datadome",
    r"dd-response",
)


class DataDomeSolver(BaseSolver):
    """DataDome challenge handler with multi-strategy bypass.

    Strategies (in priority order): cookie reuse -> tls-client ->
    Camoufox -> Botright -> Playwright + stealth.
    """

    solver_type = "datadome"

    DATADOME_INDICATORS = list(_DATADOME_INDICATORS)
    STRATEGY_PRIORITY = ["cookie", "tls_client", "camoufox", "botright"]
    DEFAULT_TIMEOUT = 90
    MAX_RETRIES = 3

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.timeout = config.get("timeout", self.DEFAULT_TIMEOUT)
        self.max_retries = config.get("max_retries", self.MAX_RETRIES)
        self.proxy = config.get("proxy")
        self.headless = config.get("headless", True)
        self.strategies = {
            "cookie": config.get("use_cookie", True),
            "tls_client": config.get("use_tls_client", True),
            "camoufox": config.get("use_camoufox", True),
            "botright": config.get("use_botright", True),
        }
        self.cookie_store = DataDomeCookieStore()
        self.fingerprint_manager = FingerprintManager()
        # Browser resources (lazy init)
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # Camoufox availability cache
        self._camoufox_available = False
        self._camoufox_fp = None
        logger.info(
            "DataDomeSolver initialized (timeout=%ss, retries=%s)",
            self.timeout, self.max_retries,
        )

    # ---- Detection ------------------------------------------------------

    def is_datadome_protected(
        self, html: str, headers: Optional[Dict] = None, status: int = 200,
    ) -> bool:
        """True if response indicates DataDome protection."""
        if status == 403 and headers:
            header_str = json.dumps(dict(headers)).lower()
            if any(x in header_str for x in ("datadome", "x-datadome")):
                return True
        for pattern in self.DATADOME_INDICATORS:
            if re.search(pattern, html, re.IGNORECASE):
                return True
        return False

    def extract_datadome_cookie(self, cookies: List[Dict]) -> Optional[Dict]:
        """Extract datadome cookie from cookie list."""
        for cookie in cookies:
            name = cookie.get("name", "")
            if name == "datadome" or name.startswith("dd_"):
                return cookie
        return None

    # ---- Strategy method bindings (delegating to siblings) --------------

    def _try_cookie_strategy(
        self, url: str, headers: Optional[Dict] = None,
    ) -> Optional[TaskResult]:
        """Try solving with stored cookie."""
        return try_cookie_strategy(self, url, headers)

    def _try_tls_client_strategy(
        self, url: str, headers: Optional[Dict] = None,
    ) -> Optional[TaskResult]:
        """Try bypassing with tls-client using a Chrome fingerprint."""
        return try_tls_client_strategy(self, url, headers)

    async def _try_camoufox_strategy(
        self, url: str, domain: str,
    ) -> Optional[TaskResult]:
        """Try solving with Camoufox - best fingerprint spoofing."""
        return await try_camoufox_strategy(self, url, domain)

    async def _try_botright_strategy(
        self, url: str, domain: str,
    ) -> Optional[TaskResult]:
        """Try solving with Botright as fallback (real Chromium + stealth)."""
        return await try_botright_strategy(self, url, domain)

    async def _try_playwright_strategy(
        self, url: str, domain: str,
    ) -> Optional[TaskResult]:
        """Fallback using standard Playwright with stealth patches."""
        return await try_playwright_strategy(self, url, domain)

    # ---- Solve entry-point ---------------------------------------------

    def _validate_solve_inputs(
        self, task: Task,
    ) -> "tuple[Optional[TaskResult], Optional[str]]":
        """Return ``(failure_result, url)`` -- exactly one is non-None."""
        if not self.enabled:
            return TaskResult(
                success=False,
                error_code="ERROR_SOLVER_NOT_AVAILABLE",
                error_message="DataDome solver is disabled",
            ), None
        url = task.params.get("pageurl") or task.params.get("url")
        if not url:
            return TaskResult(
                success=False,
                error_code="ERROR_BAD_PARAMETERS",
                error_message="Missing 'pageurl' parameter",
            ), None
        return None, url

    async def _run_strategy_chain(
        self, url: str, headers: Dict, domain: str,
    ) -> Optional[TaskResult]:
        """Try every strategy in priority order; return the first hit or None."""
        chain: List[Callable[[], Awaitable[Optional[TaskResult]]]] = [
            lambda: self._try_cookie_strategy(url, headers),
            lambda: self._try_tls_client_strategy(url, headers),
            lambda: self._try_camoufox_strategy(url, domain),
            lambda: self._try_botright_strategy(url, domain),
            lambda: self._try_playwright_strategy(url, domain),
        ]
        for strategy in chain:
            result = await strategy()
            if result:
                return result
        return None

    async def solve(self, task: Task) -> TaskResult:
        """Solve DataDome CAPTCHA using multi-strategy approach."""
        failure, url = self._validate_solve_inputs(task)
        if failure is not None:
            return failure
        if url is None:  # explicit guard (was: assert) -- survives `python -O`
            raise ValueError(
                "DataDomeSolver._validate_solve_inputs returned (None, None); "
                "expected exactly one branch to populate url",
            )
        domain = self.cookie_store._get_domain_key(url)
        headers = task.params.get("headers", {})
        logger.info("Solving DataDome for %s", url)
        start_time = time.time()
        for attempt in range(self.max_retries):
            logger.info(
                "Attempt %s/%s for %s",
                attempt + 1, self.max_retries, domain,
            )
            if attempt > 0:
                self.fingerprint_manager.rotate_fingerprint(domain)
            result = await self._run_strategy_chain(url, headers, domain)
            if result:
                return result
            logger.warning("All strategies failed on attempt %s", attempt + 1)
        total_time = int((time.time() - start_time) * 1000)
        logger.error(
            "All strategies failed for %s after %s attempts",
            url, self.max_retries,
        )
        return TaskResult(
            success=False,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            error_message=f"Failed to bypass DataDome after {self.max_retries} attempts",
            solve_time_ms=total_time,
        )

    # ---- Health check ---------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """Return solver health status across all strategies."""
        checks = {
            "tls_client": _has_module("tls_client"),
            "camoufox": _has_module("camoufox"),
            "botright": _has_module("botright"),
            "playwright": _has_module("playwright.async_api"),
        }
        healthy = any(checks.values())
        return {
            "healthy": healthy,
            "message": (
                "DataDome solver operational"
                if healthy
                else "No solving strategies available"
            ),
            "details": {
                "strategies_available": checks,
                "timeout": self.timeout,
                "max_retries": self.max_retries,
                "cookie_store_size": len(self.cookie_store._cookies),
            },
        }


# cc-allow:cc.perf.missing_memoization  reason: __import__ has side-effects (sys.modules registration)
def _has_module(name: str) -> bool:
    """True iff ``name`` is importable in the current environment."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


__all__ = [
    "DataDomeSolver",
    "DataDomeCookieStore",
    "FingerprintManager",
    "Task",
    "TaskResult",
    "ErrCode",
    "emit",
]
