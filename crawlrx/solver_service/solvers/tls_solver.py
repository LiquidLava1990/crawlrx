"""TLS client / hrequests HTTP solver — thin orchestrator.

HTTP-level anti-bot bypass using real browser TLS fingerprints. Per-engine
adapters live in ``_tls_clients``; request/response/retry shapes live in
``_tls_request``. This module is the stable public surface.

Engines:
  - hrequests: Python HTTP with browser TLS fingerprints (Go backend)
  - tls-client: Direct TLS fingerprint spoofing (Chrome/Firefox/Safari)

Use cases:
  - Bypass TLS fingerprint detection without a full browser
  - Cookie harvesting from protected endpoints
  - API scraping behind environment-check
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# --- ErrCode/emit shim -----------------------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass


from ..solver_base import BaseSolver  # noqa: E402
from ..models import Task, TaskResult  # noqa: E402

from ._solver_helpers import BROAD_EXC  # noqa: E402
from ._request_spec import RequestSpec  # noqa: E402

from ._tls_clients import (  # noqa: E402
    HREQUESTS_AVAILABLE,
    TLS_CLIENT_AVAILABLE,
    execute_hrequests,
    execute_tls_client,
    list_available_engines,
)
from ._tls_request import (  # noqa: E402
    TLSResponse,
    _EngineAttempt,
    api_request_via_solver,
    fetch_page_via_solver,
    harvest_cookies_via_solver,
    replace_request_spec,
)


logger = logging.getLogger(__name__)


_DEFAULT_HEADERS: Dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


class TLSSolver(BaseSolver):
    """TLS fingerprint-based HTTP solver.

    Uses ``hrequests`` and ``tls-client`` to bypass environment-check through
    real browser TLS fingerprints without launching a browser.
    """

    HREQUESTS_PROFILES: List[str] = [
        "chrome", "firefox", "opera", "opera_gx",
    ]
    TLS_CLIENT_PROFILES: List[str] = [
        "chrome_120", "chrome_123", "chrome_124",
        "firefox_120", "firefox_123", "firefox_128",
        "safari_17_0", "safari_17_5", "safari_ios_17_0", "safari_ios_17_5",
        "edge_120", "edge_123",
    ]
    BROWSER_REQUIRED_INDICATORS: List[str] = [
        "enable javascript", "enable cookies", "browser check",
        "challenge-platform", "cf-browser-verification", "turnstile",
        "datadome", "perimeterx", "px-captcha", "ak_bmsc", "__cf_bm",
        "cloudflare-challenge", "checking your browser", "please wait",
        "ddos protection", "rate limited", "blocked", "access denied",
        "forbidden",
    ]
    BOT_DETECTION_STATUS_CODES: List[int] = [403, 429, 503, 520, 521, 522, 523, 524]

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.prefer_engine = config.get("prefer_engine", "auto")
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1.0)
        self.follow_redirects = config.get("follow_redirects", True)
        self.verify_ssl = config.get("verify_ssl", True)
        self.timeout_seconds = config.get("timeout_seconds", 30)
        self.use_http2 = config.get("use_http2", True)
        self.proxy = config.get("proxy", None)
        self.default_headers = config.get("default_headers", dict(_DEFAULT_HEADERS))
        self.available_engines: List[str] = list_available_engines()

    @property
    def solver_type(self) -> str:
        """Stable identifier surfaced to the registry."""
        return "tls_http"

    # ---- Health check ---------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """Check solver health and available engines."""
        has_engines = bool(self.available_engines)
        message = (
            f"Available engines: {', '.join(self.available_engines)}"
            if has_engines
            else "No TLS engines available"
        )
        return {
            "healthy": has_engines,
            "enabled": self.enabled,
            "engines_available": self.available_engines,
            "hrequests_available": HREQUESTS_AVAILABLE,
            "tls_client_available": TLS_CLIENT_AVAILABLE,
            "message": message,
        }

    # ---- Spec / response shaping ---------------------------------------

    def _build_request_spec(self, task: Task) -> Optional[RequestSpec]:
        """Pull task params into a :class:`RequestSpec`. ``None`` = missing url."""
        url = task.params.get("url")
        if not url:
            return None
        return RequestSpec(
            url=url,
            method=task.params.get("method", "GET").upper(),
            headers={**self.default_headers, **task.params.get("headers", {})},
            data=task.params.get("data"),
            json_data=task.params.get("json"),
            proxy=task.params.get("proxy", self.proxy),
            follow_redirects=task.params.get("follow_redirects", self.follow_redirects),
        )

    @staticmethod
    def _build_token(response: TLSResponse) -> str:
        """JSON-encode the response payload that callers consume as ``token``."""
        return json.dumps(
            {
                "content": response.content,
                "cookies": response.cookies,
                "headers": dict(response.headers),
                "status_code": response.status_code,
                "url": response.url,
                "engine": response.engine,
                "fingerprint": response.fingerprint_used,
            },
            default=str,
        )

    def _result_from_response(
        self, response: TLSResponse, solve_time_ms: int,
    ) -> TaskResult:
        """Map a :class:`TLSResponse` to a public ``TaskResult``."""
        now = datetime.now(tz=timezone.utc)
        if response.requires_browser:
            return TaskResult(
                success=False,
                error_code="ERROR_REQUIRES_BROWSER",
                error_message=(
                    "Site requires full browser automation. "
                    "Upgrade to Camoufox/Botright solver."
                ),
                solved_at=now,
                solve_time_ms=solve_time_ms,
            )
        if response.success:
            return TaskResult(
                success=True,
                token=self._build_token(response),
                solved_at=now,
                solve_time_ms=solve_time_ms,
            )
        code = (
            f"ERROR_HTTP_{response.status_code}"
            if response.status_code
            else "ERROR_REQUEST_FAILED"
        )
        return TaskResult(
            success=False,
            error_code=code,
            error_message=response.error_message or "Request failed",
            solved_at=now,
            solve_time_ms=solve_time_ms,
        )

    def _solve_preflight(self) -> Optional[TaskResult]:
        """Reject calls early if solver disabled or no engine available."""
        if not self.enabled:
            return TaskResult(
                success=False,
                error_code="ERROR_SOLVER_NOT_AVAILABLE",
                error_message=f"Solver '{self.solver_type}' is disabled",
            )
        if not self.available_engines:
            return TaskResult(
                success=False,
                error_code="ERROR_ENGINE_UNAVAILABLE",
                error_message=(
                    "Neither hrequests nor tls-client is installed. "
                    "Run: pip install tls-client hrequests"
                ),
            )
        return None

    # ---- Solve entry-point ---------------------------------------------

    async def solve(self, task: Task) -> TaskResult:
        """Solve a task using TLS fingerprint-based HTTP requests.

        Expected ``task.params``: url, method, data, json, headers, engine,
        proxy, follow_redirects.
        """
        preflight = self._solve_preflight()
        if preflight is not None:
            return preflight
        start_time = time.time()
        try:
            spec = self._build_request_spec(task)
            if spec is None:
                return TaskResult(
                    success=False,
                    error_code="ERROR_BAD_PARAMETERS",
                    error_message="Missing required parameter: 'url'",
                )
            engine_preference = task.params.get("engine", self.prefer_engine)
            response = await self._execute_with_retry(
                spec, engine_preference=engine_preference,
            )
            return self._result_from_response(
                response, int((time.time() - start_time) * 1000),
            )
        except asyncio.TimeoutError:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            return TaskResult(
                success=False,
                error_code="ERROR_TIMEOUT",
                error_message=f"Request timed out after {self.timeout}s",
                solved_at=datetime.now(tz=timezone.utc),
                solve_time_ms=int((time.time() - start_time) * 1000),
            )
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return TaskResult(
                success=False,
                error_code="ERROR_EXCEPTION",
                error_message=str(e),
                solved_at=datetime.now(tz=timezone.utc),
                solve_time_ms=int((time.time() - start_time) * 1000),
            )

    # ---- Retry orchestration -------------------------------------------

    async def _execute_with_retry(
        self,
        spec: RequestSpec,
        engine_preference: str = "auto",
    ) -> TLSResponse:
        """Execute request with retry using different fingerprints."""
        engines = self._engines_for_preference(engine_preference)
        if not engines:
            return TLSResponse(
                success=False,
                error_message=f"Requested engine '{engine_preference}' not available",
            )
        last_error: Optional[str] = None
        profiles_tried: List[str] = []
        for attempt in range(self.max_retries):
            for engine in engines:
                outcome = await self._try_engine_attempt(
                    engine, spec, attempt, profiles_tried,
                )
                if outcome.terminal:
                    return outcome.response  # type: ignore[return-value] because outcome.terminal guarantees non-None TLSResponse
                last_error = outcome.error or last_error
                if outcome.delay:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        return TLSResponse(
            success=False,
            error_message=(
                f"All retries exhausted. Last error: {last_error}. "
                f"Tried: {', '.join(profiles_tried)}"
            ),
            fingerprint_used=profiles_tried[-1] if profiles_tried else "",
        )

    def _engines_for_preference(self, engine_preference: str) -> List[str]:
        """Resolve the engine order for a given preference."""
        if engine_preference == "hrequests" and "hrequests" in self.available_engines:
            return ["hrequests"]
        if engine_preference == "tls-client" and "tls-client" in self.available_engines:
            return ["tls-client"]
        return list(self.available_engines)

    def _profile_for(self, engine: str, attempt: int) -> str:
        """Pick the next fingerprint profile for ``engine`` on this attempt."""
        profiles = (
            self.HREQUESTS_PROFILES
            if engine == "hrequests"
            else self.TLS_CLIENT_PROFILES
        )
        return profiles[attempt % len(profiles)]

    async def _try_engine_attempt(
        self,
        engine: str,
        spec: RequestSpec,
        attempt: int,
        profiles_tried: List[str],
    ) -> _EngineAttempt:
        """One engine x profile attempt. Returns terminal-or-retry verdict."""
        fingerprint = self._profile_for(engine, attempt)
        profiles_tried.append(f"{engine}:{fingerprint}")
        try:
            response = await asyncio.to_thread(
                self._run_engine, engine, spec, fingerprint,
            )
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return _EngineAttempt(terminal=False, error=str(e), delay=True)
        if response.success or response.requires_browser:
            return _EngineAttempt(terminal=True, response=response)
        if response.status_code in self.BOT_DETECTION_STATUS_CODES:
            return _EngineAttempt(
                terminal=False,
                error=f"Status {response.status_code} with {fingerprint}",
                delay=True,
            )
        return _EngineAttempt(terminal=True, response=response)

    def _run_engine(
        self, engine: str, spec: RequestSpec, fingerprint: str,
    ) -> TLSResponse:
        """Dispatch a request through the named engine."""
        if engine == "hrequests":
            return execute_hrequests(
                replace_request_spec(spec, browser=fingerprint),
                self.timeout_seconds,
            )
        return execute_tls_client(
            replace_request_spec(spec, fingerprint=fingerprint),
            self.timeout_seconds,
        )

    # ---- Convenience methods (delegate to _tls_request helpers) --------

    async def fetch_page(
        self, spec: RequestSpec, engine: str = "auto",
    ) -> TLSResponse:
        """Fetch a page directly via the solver pipeline."""
        return await fetch_page_via_solver(self, spec, engine=engine)

    async def harvest_cookies(
        self, url: str,
        proxy: Optional[str] = None, engine: str = "auto",
    ) -> Dict[str, str]:
        """Harvest cookies from a protected endpoint."""
        return await harvest_cookies_via_solver(self, url, proxy, engine)

    async def api_request(self, spec: RequestSpec) -> Dict[str, Any]:
        """Make a JSON-aware API request with TLS fingerprint spoofing."""
        return await api_request_via_solver(self, spec)


# --- Module-level convenience functions -----------------------------------


def create_tls_solver(config: Optional[Dict[str, Any]] = None) -> TLSSolver:
    """Create a :class:`TLSSolver` instance with optional configuration."""
    return TLSSolver(config or {})


async def tls_fetch(
    spec: RequestSpec,
    *,
    engine: str = "auto",
    timeout: int = 30,
) -> TLSResponse:
    """Quick fetch using TLS fingerprint spoofing."""
    solver = create_tls_solver({"timeout": timeout})
    return await solver.fetch_page(spec, engine=engine)


async def tls_harvest_cookies(
    url: str,
    proxy: Optional[str] = None,
    engine: str = "auto",
) -> Dict[str, str]:
    """Quick cookie harvest using TLS fingerprint spoofing."""
    solver = create_tls_solver()
    return await solver.harvest_cookies(url, proxy, engine)


async def tls_api_request(
    spec: RequestSpec,
) -> Dict[str, Any]:
    """Quick API request using TLS fingerprint spoofing."""
    solver = create_tls_solver()
    return await solver.api_request(spec)


__all__ = [
    "TLSSolver",
    "TLSResponse",
    "create_tls_solver",
    "tls_fetch",
    "tls_harvest_cookies",
    "tls_api_request",
    "HREQUESTS_AVAILABLE",
    "TLS_CLIENT_AVAILABLE",
    "BROAD_EXC",
    "ErrCode",
    "emit",
]
