"""TLS solver — request shape, retry verdict types, engine context, and
solver-level convenience entry-points.

Owns the dataclasses that flow between the orchestrator and the
per-engine adapters, plus the ``fetch_page`` / ``harvest_cookies`` /
``api_request`` flows used both by ``TLSSolver`` methods and the
module-level convenience functions in ``tls_solver``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ._request_spec import RequestSpec
    from tls_solver import TLSSolver  # noqa: F401


# --- ErrCode/emit shim -----------------------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass


@dataclass
class TLSResponse:
    """Response data from TLS-based HTTP request."""

    success: bool
    status_code: int = 0
    content: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    engine: str = ""  # 'hrequests' or 'tls-client'
    fingerprint_used: str = ""
    error_message: Optional[str] = None
    requires_browser: bool = False  # True if site needs full browser
    solve_time_ms: int = 0


@dataclass(frozen=True)
class _EngineAttempt:
    """Per-engine x profile attempt verdict surfaced to ``_execute_with_retry``.

    ``terminal=True`` means the loop should return ``response`` immediately
    (success or definitive failure). ``terminal=False`` means try the next
    engine / next attempt; when ``delay`` is true the retry sleep applies.
    """

    terminal: bool
    response: Optional["TLSResponse"] = None
    error: Optional[str] = None
    delay: bool = False


@dataclass(frozen=True)
class _EngineRunCtx:
    """Bundled context for the per-engine session-request helper.

    Keeps the helper under the ``cc.shape.param_count`` ceiling without
    splitting the engine into more callable surfaces.
    """

    engine: str
    fingerprint: str
    timeout_kw: str
    with_headers_kwarg: bool


def replace_request_spec(spec: RequestSpec, **changes: object) -> RequestSpec:
    """``dataclasses.replace`` shim; ``**changes`` are RequestSpec field overrides."""
    from dataclasses import replace as _dc_replace
    return _dc_replace(spec, **changes)


async def fetch_page_via_solver(
    solver: "TLSSolver", spec: "RequestSpec", engine: str = "auto",
) -> "TLSResponse":
    """Drive ``solver.solve()`` for a direct fetch and unmarshal the token."""
    from ..models import Task, CaptchaMethod  # late binding to avoid cycle

    task = Task(
        id=f"direct_{int(time.time()*1000)}",
        method=CaptchaMethod.IMAGE,
        params={
            "url": spec.url,
            "method": spec.method,
            "headers": spec.headers or {},
            "data": spec.data,
            "json": spec.json_data,
            "proxy": spec.proxy,
            "engine": engine,
        },
    )
    result = await solver.solve(task)
    if result.success and result.token:
        try:
            data = json.loads(result.token)
            return TLSResponse(
                success=True,
                status_code=data.get("status_code", 0),
                content=data.get("content", ""),
                headers=data.get("headers", {}),
                cookies=data.get("cookies", {}),
                url=data.get("url", spec.url),
                engine=data.get("engine", ""),
                fingerprint_used=data.get("fingerprint", ""),
                solve_time_ms=result.solve_time_ms or 0,
            )
        except json.JSONDecodeError:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
    return TLSResponse(
        success=False,
        error_message=result.error_message or "Failed to fetch page",
        requires_browser=result.error_code == "ERROR_REQUIRES_BROWSER",
    )


async def harvest_cookies_via_solver(
    solver: "TLSSolver", url: str,
    proxy: Optional[str] = None, engine: str = "auto",
) -> Dict[str, str]:
    """Harvest cookies from a protected endpoint via the solver pipeline."""
    from ..solver_base import SolverError
    from ._request_spec import RequestSpec

    spec = RequestSpec(url=url, proxy=proxy)
    response = await fetch_page_via_solver(solver, spec, engine=engine)
    if response.requires_browser:
        raise SolverError(
            "ERROR_REQUIRES_BROWSER",
            "Site requires full browser automation for cookie harvesting",
        )
    return response.cookies


async def api_request_via_solver(
    solver: "TLSSolver", spec: "RequestSpec",
) -> Dict[str, Any]:
    """Make a JSON-aware API request via the solver pipeline."""
    from ..solver_base import SolverError

    req_headers = dict(spec.headers or {})
    if "Content-Type" not in req_headers and spec.json_data:
        req_headers["Content-Type"] = "application/json"
    if "Accept" not in req_headers:
        req_headers["Accept"] = "application/json"
    api_spec = replace_request_spec(spec, headers=req_headers)
    response = await fetch_page_via_solver(solver, api_spec)
    if response.requires_browser:
        raise SolverError(
            "ERROR_REQUIRES_BROWSER",
            "API endpoint requires full browser automation",
        )
    try:
        json_content = json.loads(response.content)
    except json.JSONDecodeError:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        json_content = None
    return {
        "success": response.success,
        "status_code": response.status_code,
        "content": response.content,
        "json": json_content,
        "cookies": response.cookies,
        "headers": response.headers,
        "engine": response.engine,
        "fingerprint": response.fingerprint_used,
    }


__all__ = [
    "TLSResponse",
    "_EngineAttempt",
    "_EngineRunCtx",
    "replace_request_spec",
    "fetch_page_via_solver",
    "harvest_cookies_via_solver",
    "api_request_via_solver",
]
