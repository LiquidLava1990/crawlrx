"""TLS solver — per-engine adapters (hrequests + tls-client).

Each engine helper takes a :class:`RequestSpec` plus the solver
instance (for ``BOT_DETECTION_STATUS_CODES`` / detection lists +
``timeout_seconds``) and returns a :class:`TLSResponse`.

The two engines share a single ``run_session_request`` core so the
``cc.shape.duplication`` finding stays at zero.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Protocol, TYPE_CHECKING

from ._tls_request import TLSResponse, _EngineRunCtx


class _TLSSession(Protocol):
    """Minimum surface ``run_session_request`` reaches through.

    Both hrequests.Session and tls_client.Session expose this shape: a
    ``proxies`` mutable mapping. The per-verb / generic ``request`` methods
    are reached via ``getattr`` at the call site so the Protocol stays free
    of cross-backend signature variance.
    """

    proxies: Dict[str, str]


class _TLSResponseLike(Protocol):
    """Minimum surface ``build_tls_response`` reads off the backend response."""

    text: str
    status_code: int
    headers: Any
    cookies: Any
    url: Any

if TYPE_CHECKING:
    from ._request_spec import RequestSpec
    from tls_solver import TLSSolver  # noqa: F401


# --- ErrCode/emit + BROAD_EXC shim ----------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

from ._solver_helpers import BROAD_EXC

try:
    import hrequests
    HREQUESTS_AVAILABLE = True
except ImportError:
    hrequests = None  # type: ignore[assignment]  because hrequests is optional; None sentinel gates HREQUESTS_AVAILABLE callers
    HREQUESTS_AVAILABLE = False

try:
    import tls_client
    TLS_CLIENT_AVAILABLE = True
except ImportError:
    tls_client = None  # type: ignore[assignment]  because tls_client is optional; None sentinel gates TLS_CLIENT_AVAILABLE callers
    TLS_CLIENT_AVAILABLE = False


logger = logging.getLogger(__name__)


# Browser-required indicators kept here too so the response builder can
# stay self-contained.
_BROWSER_REQUIRED_INDICATORS: tuple[str, ...] = (
    "enable javascript", "enable cookies", "browser check",
    "challenge-platform", "cf-browser-verification", "turnstile",
    "datadome", "perimeterx", "px-captcha", "ak_bmsc", "__cf_bm",
    "cloudflare-challenge", "checking your browser", "please wait",
    "ddos protection", "rate limited", "blocked", "access denied",
    "forbidden",
)
_JS_CHALLENGE_INDICATORS: tuple[str, ...] = (
    "<script>challenge", "window._cf_chl_opt", "cf_chl_jschl_tk",
    "jschl-answer", "data-cf-settings", "__cf_chl_jschl_tk__",
    "turnstile", "datadome", "px-captcha", "perimeterx", "akamai",
    "imperva",
)
_BOT_DETECTION_STATUS_CODES: tuple[int, ...] = (
    403, 429, 503, 520, 521, 522, 523, 524,
)


def check_requires_browser(content: str, status_code: int) -> bool:
    """True iff the response indicates a full browser is required."""
    content_lower = content.lower() if content else ""
    if status_code in _BOT_DETECTION_STATUS_CODES:
        for indicator in _BROWSER_REQUIRED_INDICATORS:
            if indicator in content_lower:
                return True
    for indicator in _JS_CHALLENGE_INDICATORS:
        if indicator in content_lower:
            return True
    if len(content) < 500 and status_code in (403, 429, 503):
        return True
    return False


def build_method_kwargs(
    spec: "RequestSpec",
    timeout_kw: str,
    timeout_val: float,
    with_headers_kwarg: bool,
) -> Dict[str, Any]:
    """Build the per-request kwargs shared by both engine helpers."""
    kwargs: Dict[str, Any] = {
        "allow_redirects": spec.follow_redirects,
        timeout_kw: timeout_val,
    }
    if with_headers_kwarg:
        kwargs["headers"] = spec.headers
    if spec.data:
        kwargs["data"] = spec.data
    if spec.json_data:
        kwargs["json"] = spec.json_data
    return kwargs


def dispatch_method(
    session: _TLSSession, method: str, url: str, kwargs: Dict[str, Any],
) -> "_TLSResponseLike":
    """``session.<verb>`` dispatcher used by both engine helpers."""
    verb = method.lower()
    attr = getattr(session, verb, None)
    if attr is None:
        # Fall back to the generic .request() method via getattr so the
        # _TLSSession Protocol does not have to declare it (the signature
        # varies between hrequests and tls_client).
        return getattr(session, "request")(method, url, **kwargs)
    return attr(url, **kwargs)


def build_tls_response(
    resp: "_TLSResponseLike",
    spec: "RequestSpec",
    ctx: _EngineRunCtx,
    start_time: float,
) -> TLSResponse:
    """Translate a backend HTTP response into our :class:`TLSResponse`."""
    solve_time_ms = int((time.time() - start_time) * 1000)
    requires_browser = check_requires_browser(resp.text, resp.status_code)
    return TLSResponse(
        success=200 <= resp.status_code < 400 and not requires_browser,
        status_code=resp.status_code,
        content=resp.text,
        headers=dict(resp.headers) if hasattr(resp, "headers") else {},
        cookies=dict(resp.cookies) if hasattr(resp, "cookies") else {},
        url=str(resp.url) if hasattr(resp, "url") else spec.url,
        engine=ctx.engine,
        fingerprint_used=ctx.fingerprint,
        requires_browser=requires_browser,
        solve_time_ms=solve_time_ms,
    )


def run_session_request(
    session: _TLSSession,
    spec: "RequestSpec",
    ctx: _EngineRunCtx,
    timeout_seconds: float,
) -> TLSResponse:
    """Shared core: apply proxy + build kwargs + dispatch + map response."""
    start_time = time.time()
    if spec.proxy:
        session.proxies = {"http": spec.proxy, "https": spec.proxy}
    kwargs = build_method_kwargs(
        spec, ctx.timeout_kw, timeout_seconds,
        with_headers_kwarg=ctx.with_headers_kwarg,
    )
    resp = dispatch_method(session, spec.method, spec.url, kwargs)
    return build_tls_response(resp, spec, ctx, start_time)


def engine_error_response(
    engine: str, fingerprint: str, exc: BaseException,
) -> TLSResponse:
    """Compose the failure ``TLSResponse`` shared by both engine helpers."""
    return TLSResponse(
        success=False,
        error_message=f"{engine} error: {str(exc)}",
        engine=engine,
        fingerprint_used=fingerprint,
    )


def execute_hrequests(
    spec: "RequestSpec", timeout_seconds: float,
) -> TLSResponse:
    """Execute request using the hrequests engine."""
    browser = spec.browser or "chrome"
    try:
        session = hrequests.Session(browser=browser, headers=spec.headers)
        ctx = _EngineRunCtx(
            engine="hrequests", fingerprint=browser,
            timeout_kw="timeout", with_headers_kwarg=False,
        )
        return run_session_request(session, spec, ctx, timeout_seconds)
    except BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        return engine_error_response("hrequests", browser, e)


def execute_tls_client(
    spec: "RequestSpec", timeout_seconds: float,
) -> TLSResponse:
    """Execute request using the tls-client engine."""
    fingerprint = spec.fingerprint or "chrome_120"
    try:
        session = tls_client.Session(
            client_identifier=fingerprint,
            random_tls_extension_order=True,
        )
        ctx = _EngineRunCtx(
            engine="tls-client", fingerprint=fingerprint,
            timeout_kw="timeout_seconds", with_headers_kwarg=True,
        )
        return run_session_request(session, spec, ctx, timeout_seconds)
    except BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        return engine_error_response("tls-client", fingerprint, e)


def list_available_engines() -> List[str]:
    """Return the list of installed engine identifiers."""
    out: List[str] = []
    if HREQUESTS_AVAILABLE:
        out.append("hrequests")
    if TLS_CLIENT_AVAILABLE:
        out.append("tls-client")
    return out


__all__ = [
    "execute_hrequests",
    "execute_tls_client",
    "check_requires_browser",
    "list_available_engines",
    "HREQUESTS_AVAILABLE",
    "TLS_CLIENT_AVAILABLE",
]
