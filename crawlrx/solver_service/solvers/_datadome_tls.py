"""DataDome TLS strategies — cookie reuse + tls-client fingerprint iteration.

Split out of ``datadome_solver.py``. The orchestrator binds each
public ``_try_*`` function to a method on ``DataDomeSolver``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # avoid cycles at runtime
    from datadome_solver import DataDomeSolver  # noqa: F401


# --- ErrCode/emit shim -----------------------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

from ._solver_helpers import BROAD_EXC as _BROAD_EXC

try:
    import tls_client
except ImportError:
    tls_client = None


logger = logging.getLogger(__name__)

_TLS_CLIENT_IDS = (
    "chrome_120", "chrome_121", "chrome_119", "safari_17", "firefox_121",
)


def build_tls_request_headers(extra: Optional[Dict] = None) -> Dict[str, str]:
    """Compose the standard browser-like header bundle, merged with ``extra``."""
    request_headers: Dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if extra:
        request_headers.update(extra)
    return request_headers


# --- Strategy 1: Cookie reuse ---------------------------------------------


def _cookie_strategy_prereqs(
    solver: "DataDomeSolver", url: str,
) -> Optional[Dict]:
    """Return the stored cookie when cookie-strategy can run, else ``None``."""
    if not solver.strategies["cookie"]:
        return None
    cookie = solver.cookie_store.get_cookie(url)
    if not cookie:
        return None
    if tls_client is None:
        logger.warning("tls_client not available for cookie strategy")
        return None
    return cookie


def _validate_cookie_response(
    solver: "DataDomeSolver", response: Any, url: str,
) -> bool:
    """Return True if ``response`` indicates a successful (un-protected) page."""
    if response.status_code != 200:
        return False
    if solver.is_datadome_protected(response.text):
        logger.warning("Cookie rejected, invalidating")
        solver.cookie_store.invalidate(url)
        return False
    return True


def try_cookie_strategy(
    solver: "DataDomeSolver",
    url: str,
    headers: Optional[Dict] = None,
) -> Optional[Any]:
    """Try solving with stored cookie."""
    from datadome_solver import TaskResult  # late binding to avoid cycle

    cookie = _cookie_strategy_prereqs(solver, url)
    if cookie is None:
        return None
    logger.info("Trying cookie strategy for %s", url)
    try:
        session = tls_client.Session(
            client_identifier="chrome_120",
            random_tls_extension_order=True,
        )
        request_headers = build_tls_request_headers(headers)
        request_headers["Cookie"] = f"{cookie['name']}={cookie['value']}"
        response = session.get(url, headers=request_headers, timeout_seconds=30)
    except _BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        logger.warning("Cookie strategy failed: %s", exc)
        return None
    if not _validate_cookie_response(solver, response, url):
        return None
    logger.info("Cookie strategy succeeded")
    return TaskResult(
        success=True,
        token=cookie["value"],
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=0,
    )


# --- Strategy 2: tls-client HTTP bypass -----------------------------------


def _tls_client_success_result(
    solver: "DataDomeSolver", url: str, response: Any,
) -> Any:
    """Persist any datadome cookie and build the success TaskResult."""
    from datadome_solver import TaskResult

    set_cookie = response.headers.get("set-cookie", "")
    if "datadome=" in set_cookie:
        match = re.search(r"datadome=([^;]+)", set_cookie)
        if match:
            solver.cookie_store.set_cookie(url, "datadome", match.group(1))
    return TaskResult(
        success=True,
        token=set_cookie if set_cookie else "bypassed",
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=0,
    )


def _tls_client_attempt(
    solver: "DataDomeSolver",
    url: str,
    headers: Optional[Dict],
    client_id: str,
) -> Optional[Any]:
    """One client_id attempt; return TaskResult on bypass, None otherwise."""
    try:
        session = tls_client.Session(
            client_identifier=client_id,
            random_tls_extension_order=True,
        )
        request_headers = build_tls_request_headers(headers)
        if solver.proxy:
            session.proxies = {"http": solver.proxy, "https": solver.proxy}
        response = session.get(url, headers=request_headers, timeout_seconds=30)
        if response.status_code != 200:
            return None
        if solver.is_datadome_protected(response.text):
            logger.debug("tls-client %s hit DataDome", client_id)
            return None
        logger.info("tls-client strategy succeeded with %s", client_id)
        return _tls_client_success_result(solver, url, response)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.debug("tls-client %s failed: %s", client_id, e)
        return None


def _iterate_tls_client_ids(
    solver: "DataDomeSolver", url: str, headers: Optional[Dict],
) -> Optional[Any]:
    """Try each Chrome client_id; return first successful TaskResult."""
    for client_id in _TLS_CLIENT_IDS:
        result = _tls_client_attempt(solver, url, headers, client_id)
        if result is not None:
            return result
    logger.info("tls-client strategy failed, falling back to browser")
    return None


def try_tls_client_strategy(
    solver: "DataDomeSolver",
    url: str,
    headers: Optional[Dict] = None,
) -> Optional[Any]:
    """Try bypassing with tls-client using a Chrome fingerprint."""
    if not solver.strategies["tls_client"]:
        return None
    if tls_client is None:
        logger.warning("tls_client not available, skipping strategy")
        return None
    logger.info("Trying tls-client strategy for %s", url)
    try:
        return _iterate_tls_client_ids(solver, url, headers)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("tls-client strategy failed: %s", e)
        return None


__all__ = [
    "build_tls_request_headers",
    "try_cookie_strategy",
    "try_tls_client_strategy",
    "tls_client",
]
