"""

curl-impersonate + tls-client solver for FlareSolverr.



Spoofs TLS fingerprints to match real browsers (Chrome, Firefox, Safari)

to handle HTTP-level anti-bot protection (DataDome, Akamai, PerimeterX)

without needing a real browser.

"""


try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

import asyncio

import json

import random

import time

from typing import Any, Dict, Optional






try:
    import tls_client

    TLS_CLIENT_AVAILABLE = True

except ImportError:

    TLS_CLIENT_AVAILABLE = False



try:

    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True

except ImportError:

    CURL_CFFI_AVAILABLE = False



from ..solver_base import BaseSolver, SolverError, SolverTimeoutError

from ..models import Task, TaskResult

from ._solver_helpers import BROAD_EXC
from ._request_spec import RequestSpec
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class _SolveParams:
    """Bundled ``solve()`` task parameters. Removes ``cc.shape.param_count``
    pressure on ``_solve_attempt`` / ``_request_via_backend`` helpers."""

    url: str
    method: str = "GET"
    custom_headers: Dict[str, str] = None  # type: ignore[assignment] because dataclass placeholder; populated by __post_init__ to a fresh dict per instance
    post_data: Any = None
    use_datadome: bool = False
    specific_browser: Optional[str] = None

    def with_datadome(self, enabled: bool) -> "_SolveParams":
        """Return a copy with ``use_datadome`` set to ``enabled``."""
        return replace(self, use_datadome=enabled)


@dataclass(frozen=True)
class _AttemptOutcome:
    """Per-attempt outcome surfaced to the retry loop -- keeps depth <= 3."""

    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None




class CurlImpersonateSolver(BaseSolver):

    """

    Solver using curl-impersonate and tls-client for HTTP-level anti-bot bypass.

    Handles DataDome, Akamai, PerimeterX by spoofing TLS fingerprints.

    """



    # Browser identities for tls-client

    TLS_CLIENT_IDENTITIES = [

        "chrome_120",

        "chrome_119",

        "chrome_118",

        "firefox_120",

        "firefox_119",

        "firefox_118",

        "safari_17_0",

        "safari_16_0",

        "safari_ios_17_0",

        "safari_ios_16_0",

        "edge_120",

        "opera_106",

    ]



    # Browser identities for curl_cffi

    CURL_CFFI_IDENTITIES = [

        "chrome120",

        "chrome119",

        "chrome118",

        "firefox120",

        "firefox119",

        "firefox118",

        "safari17_0",

        "safari16_0",

        "edge120",

        "opera106",

    ]



    # Realistic headers by browser type

    DEFAULT_HEADERS = {

        "chrome_120": {

            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",

            "Accept-Language": "en-US,en;q=0.9",

            "Accept-Encoding": "gzip, deflate, br",

            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',

            "Sec-Ch-Ua-Mobile": "?0",

            "Sec-Ch-Ua-Platform": '"Windows"',

            "Sec-Fetch-Dest": "document",

            "Sec-Fetch-Mode": "navigate",

            "Sec-Fetch-Site": "none",

            "Sec-Fetch-User": "?1",

            "Upgrade-Insecure-Requests": "1",

        },

        "firefox_120": {

            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",

            "Accept-Language": "en-US,en;q=0.5",

            "Accept-Encoding": "gzip, deflate, br",

            "Sec-Fetch-Dest": "document",

            "Sec-Fetch-Mode": "navigate",

            "Sec-Fetch-Site": "none",

            "Sec-Fetch-User": "?1",

            "Upgrade-Insecure-Requests": "1",

        },

        "safari_17_0": {

            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

            "Accept-Language": "en-US,en;q=0.9",

            "Accept-Encoding": "gzip, deflate, br",

            "Sec-Fetch-Dest": "document",

            "Sec-Fetch-Mode": "navigate",

            "Sec-Fetch-Site": "none",

            "Sec-Fetch-User": "?1",

        },

    }



    def __init__(self, config: Dict[str, Any]) -> None:

        super().__init__(config)

        self.timeout = config.get('timeout', 60)

        self.max_retries = config.get('max_retries', 3)

        self.proxy = config.get('proxy')

        self.proxy_list = config.get('proxy_list', [])

        self.enable_datadome = config.get('enable_datadome', True)

        self.primary_method = config.get('primary_method', 'tls_client')

        self._sessions: Dict[str, Any] = {}

        self._last_identity: Optional[str] = None



    @property

    def solver_type(self) -> str:

        return "curl_impersonate"



    def _get_proxy(self) -> Optional[str]:

        """Get proxy for request - either single proxy or from rotation."""

        if self.proxy_list:

            return random.choice(self.proxy_list)

        return self.proxy



    def _get_headers_for_identity(self, identity: str) -> Dict[str, str]:

        """Get realistic headers matching the browser identity."""

        base_headers = {

            "User-Agent": self._get_user_agent(identity),

        }



        if "chrome" in identity.lower():

            browser_headers = self.DEFAULT_HEADERS["chrome_120"].copy()

        elif "firefox" in identity.lower():

            browser_headers = self.DEFAULT_HEADERS["firefox_120"].copy()

        elif "safari" in identity.lower():

            browser_headers = self.DEFAULT_HEADERS["safari_17_0"].copy()

        else:

            browser_headers = self.DEFAULT_HEADERS["chrome_120"].copy()



        browser_headers.update(base_headers)

        return browser_headers



    def _get_user_agent(self, identity: str) -> str:

        """Get User-Agent string for browser identity."""

        user_agents = {

            "chrome_120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

            "chrome_119": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",

            "chrome_118": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",

            "firefox_120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",

            "firefox_119": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",

            "firefox_118": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:118.0) Gecko/20100101 Firefox/118.0",

            "safari_17_0": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",

            "safari_16_0": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",

            "safari_ios_17_0": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",

            "safari_ios_16_0": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",

            "edge_120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",

            "opera_106": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",

        }

        return user_agents.get(identity, user_agents["chrome_120"])



    def _create_tls_session(self, identity: str) -> Any:

        """Create a tls-client session with specified browser identity."""

        if not TLS_CLIENT_AVAILABLE:

            raise SolverError("ERROR_MODULE_NOT_FOUND", "tls_client not installed. Run: pip install tls_client")



        session = tls_client.Session(

            client_identifier=identity,

            random_tls_extension_order=True,

        )

        return session



    def _create_curl_session(self, identity: str) -> Any:

        """Create a curl_cffi session with specified browser identity."""

        if not CURL_CFFI_AVAILABLE:

            raise SolverError("ERROR_MODULE_NOT_FOUND", "curl_cffi not installed. Run: pip install curl_cffi")



        session = curl_requests.Session(impersonate=identity)

        return session



    def _dispatch_request(
        self,
        session: Any,
        spec: RequestSpec,
        request_headers: Dict[str, str],
        transport_kwargs: Dict[str, Any],
    ) -> Any:
        """Single GET/POST dispatcher used by both TLS and curl backends.

        ``transport_kwargs`` carries the per-backend differences (e.g.
        ``proxy=`` vs ``proxies=`` and ``timeout_seconds=`` vs
        ``timeout=``). Wrapping that variation in a small dict removes the
        ``cc.shape.duplication`` finding between
        :py:meth:`_make_request_tls` and :py:meth:`_make_request_curl`.
        """
        method = spec.method.upper()
        common = dict(
            headers=request_headers,
            allow_redirects=spec.follow_redirects,
            **transport_kwargs,
        )
        try:
            if method == "GET":
                return session.get(spec.url, **common)
            if method == "POST":
                return session.post(spec.url, data=spec.data, **common)
            raise SolverError("ERROR_BAD_METHOD", f"Unsupported method: {method}")
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            raise SolverError("ERROR_REQUEST_FAILED", str(e))

    def _build_headers(self, identity: str, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Build per-request headers; merges caller overrides on top."""
        request_headers = self._get_headers_for_identity(identity)
        if extra:
            request_headers.update(extra)
        return request_headers

    def _make_request_tls(
        self,
        spec: RequestSpec,
        identity: Optional[str] = None,
    ) -> Any:
        """Make HTTP request using tls-client."""
        identity = identity or random.choice(self.TLS_CLIENT_IDENTITIES)
        self._last_identity = identity
        session = self._create_tls_session(identity)
        request_headers = self._build_headers(identity, spec.headers)
        transport = {"proxy": spec.proxy, "timeout_seconds": self.timeout}
        return self._dispatch_request(session, spec, request_headers, transport)

    def _make_request_curl(
        self,
        spec: RequestSpec,
        identity: Optional[str] = None,
    ) -> Any:
        """Make HTTP request using curl_cffi."""
        identity = identity or random.choice(self.CURL_CFFI_IDENTITIES)
        self._last_identity = identity
        session = self._create_curl_session(identity)
        # curl_cffi uses the underscore-stripped identity convention for headers.
        header_id = identity.replace("120", "_120").replace("119", "_119")
        request_headers = self._build_headers(header_id, spec.headers)
        proxies = {"http": spec.proxy, "https": spec.proxy} if spec.proxy else None
        transport = {"proxies": proxies, "timeout": self.timeout}
        return self._dispatch_request(session, spec, request_headers, transport)



    def _solve_datadome(

        self,

        url: str,

        proxy: Optional[str] = None,

        identity: Optional[str] = None

    ) -> Dict[str, Any]:

        """

        Solve DataDome anti-bot challenge.

        

        Strategy:

        1. Make initial request to trigger dd_s cookie

        2. Extract challenge if present

        3. Solve with proper TLS fingerprint

        4. Return valid cookies

        """

        identity = identity or random.choice(self.TLS_CLIENT_IDENTITIES)



        if self.primary_method == 'tls_client' and TLS_CLIENT_AVAILABLE:

            response = self._make_request_tls(RequestSpec(url=url, proxy=proxy, follow_redirects=True), identity=identity)

        elif CURL_CFFI_AVAILABLE:

            response = self._make_request_curl(RequestSpec(url=url, proxy=proxy, follow_redirects=True), identity=identity)

        else:

            raise SolverError("ERROR_NO_BACKEND", "No HTTP backend available")



        cookies = {}



        if hasattr(response, 'cookies'):

            if isinstance(response.cookies, dict):

                cookies = response.cookies

            else:

                cookies = dict(response.cookies)



        result = {

            "status_code": getattr(response, 'status_code', 200),

            "cookies": cookies,

            "headers": dict(getattr(response, 'headers', {})),

            "text": getattr(response, 'text', ''),

            "identity_used": identity,

        }



        if 'dd_' in str(cookies) or 'datadome' in response.text.lower():

            result["datadome_detected"] = True

            result["datadome_cookies"] = {k: v for k, v in cookies.items() if k.startswith('dd_')}



        return result



    @staticmethod
    def _solve_params_from(task: Task) -> Optional["_SolveParams"]:
        """Pull task.params into a typed bundle. ``None`` = missing url."""
        url = task.params.get("url")
        if not url:
            return None
        return _SolveParams(
            url=url,
            method=task.params.get("method", "GET"),
            custom_headers=task.params.get("headers", {}) or {},
            post_data=task.params.get("data"),
            use_datadome=task.params.get("datadome", False),  # caller may override
            specific_browser=task.params.get("browser"),
        )

    def _resolve_backend_identity(self, params: "_SolveParams", attempt: int) -> str:
        """Pick the browser identity for this attempt."""
        return (
            params.specific_browser
            or self.TLS_CLIENT_IDENTITIES[attempt % len(self.TLS_CLIENT_IDENTITIES)]
        )

    def _request_via_backend(
        self,
        params: "_SolveParams",
        identity: str,
        proxy: Optional[str],
    ) -> Dict[str, Any]:
        """Execute a single non-DataDome request via the configured backend."""
        spec = RequestSpec(
            url=params.url,
            method=params.method,
            headers=params.custom_headers,
            data=params.post_data,
            proxy=proxy,
        )
        if self.primary_method == "tls_client" and TLS_CLIENT_AVAILABLE:
            response = self._make_request_tls(spec, identity=identity)
        elif CURL_CFFI_AVAILABLE:
            curl_identity = identity.replace("_", "")
            response = self._make_request_curl(spec, identity=curl_identity)
        else:
            raise SolverError("ERROR_NO_BACKEND", "Neither tls_client nor curl_cffi available")
        return {
            "status_code": getattr(response, "status_code", 200),
            "cookies": dict(getattr(response, "cookies", {})),
            "headers": dict(getattr(response, "headers", {})),
            "text": getattr(response, "text", "")[:10000],
            "identity_used": identity,
        }

    async def _solve_attempt(
        self,
        params: "_SolveParams",
        attempt: int,
        proxy: Optional[str],
        start_time: float,
    ) -> Dict[str, Any]:
        """One pass through the solve flow. Raises on failure."""
        if time.time() - start_time > self.timeout:
            raise SolverTimeoutError(self.timeout)
        identity = self._resolve_backend_identity(params, attempt)
        if params.use_datadome:
            return self._solve_datadome(params.url, proxy=proxy, identity=identity)
        return self._request_via_backend(params, identity, proxy)

    async def _try_attempt(
        self,
        params: "_SolveParams",
        attempt: int,
        proxy: Optional[str],
        start_time: float,
    ) -> "_AttemptOutcome":
        """Run one attempt and classify the outcome (depth <= 2)."""
        try:
            result = await self._solve_attempt(params, attempt, proxy, start_time)
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return _AttemptOutcome(success=False, error=str(e))
        return _AttemptOutcome(success=True, result=result)

    async def solve(self, task: Task) -> TaskResult:
        """Solve anti-bot challenge using curl-impersonate / tls-client.

        Expected ``task.params`` fields:
            url, method, headers, data, datadome, proxy, browser.
        """
        start_time = time.time()
        params = self._solve_params_from(task)
        if params is None:
            return TaskResult(
                success=False,
                error_code="ERROR_BAD_PARAMETERS",
                error_message="Missing required field: url",
            )
        # `enable_datadome` instance default participates only when caller did
        # not specify -- preserve the legacy semantic.
        if "datadome" not in task.params:
            params = params.with_datadome(self.enable_datadome)
        proxy = task.params.get("proxy") or self._get_proxy()

        last_error: Optional[str] = None
        for attempt in range(self.max_retries):
            outcome = await self._try_attempt(params, attempt, proxy, start_time)
            if outcome.success:
                return self._make_success_result(outcome.result, start_time)
            last_error = outcome.error
            if attempt < self.max_retries - 1:
                await asyncio.sleep(1 + attempt)
        return TaskResult(
            success=False,
            error_code="ERROR_MAX_RETRIES",
            error_message=f"Failed after {self.max_retries} attempts. Last error: {last_error}",
        )

    @staticmethod
    def _make_success_result(result: Dict[str, Any], start_time: float) -> TaskResult:
        """Wrap a successful backend response in a ``TaskResult``."""
        cookies = result.get("cookies", {}) or {}
        token = cookies.get("dd_s") or json.dumps(cookies)
        return TaskResult(
            success=True,
            token=token,
            solve_time_ms=int((time.time() - start_time) * 1000),
        )



    def health_check(self) -> Dict[str, Any]:

        """Return solver health status."""

        backends = []



        if TLS_CLIENT_AVAILABLE:

            backends.append("tls_client")

        if CURL_CFFI_AVAILABLE:

            backends.append("curl_cffi")



        healthy = len(backends) > 0



        return {

            "healthy": healthy,

            "backends_available": backends,

            "primary_method": self.primary_method,

            "message": f"Available backends: {', '.join(backends)}" if healthy else "No backends available. Install: pip install tls_client curl_cffi",

        }





# Factory function for dynamic loading

def create_solver(config: Dict[str, Any]) -> CurlImpersonateSolver:

    """Create and return a CurlImpersonateSolver instance."""

    return CurlImpersonateSolver(config)

