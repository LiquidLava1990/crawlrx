"""TDD contract for the tls_solver godfile split.

Invariants captured before splitting:
- Public class names + module-level functions remain importable.
- ``TLSSolver`` keeps its public method set.
- Each split sibling under ``solvers/`` stays <=500 LOC.
- The orchestrator (``tls_solver.py``) is itself <=500 LOC.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SOLVERS_DIR = Path(__file__).resolve().parents[1] / "crawlrx" / "solver_service" / "solvers"


def _import_solver(name: str):
    """Import a solver the same way the production registry does (``crawlrx.solver_service.solvers.X``)."""
    return importlib.import_module(f"crawlrx.solver_service.solvers.{name}")


def test_tls_public_surface() -> None:
    """TLSSolver + TLSResponse + factory + module-level coroutines preserved."""
    mod = _import_solver("tls_solver")
    for name in (
        "TLSSolver",
        "TLSResponse",
        "create_tls_solver",
        "tls_fetch",
        "tls_harvest_cookies",
        "tls_api_request",
        "HREQUESTS_AVAILABLE",
        "TLS_CLIENT_AVAILABLE",
    ):
        assert hasattr(mod, name), f"missing public symbol: {name}"


def test_tls_solver_methods_intact() -> None:
    """The orchestrator still exposes the load-bearing methods."""
    mod = _import_solver("tls_solver")
    cls = mod.TLSSolver
    for name in (
        "solve",
        "health_check",
        "fetch_page",
        "harvest_cookies",
        "api_request",
        "solver_type",
    ):
        assert hasattr(cls, name), f"TLSSolver lost public method: {name}"


def test_tls_class_constants_preserved() -> None:
    """Profile + detection lists stay on the class."""
    mod = _import_solver("tls_solver")
    cls = mod.TLSSolver
    assert hasattr(cls, "HREQUESTS_PROFILES")
    assert hasattr(cls, "TLS_CLIENT_PROFILES")
    assert hasattr(cls, "BROWSER_REQUIRED_INDICATORS")
    assert hasattr(cls, "BOT_DETECTION_STATUS_CODES")


@pytest.mark.parametrize(
    "fname",
    [
        "tls_solver.py",
        "_tls_request.py",
        "_tls_clients.py",
    ],
)
def test_tls_sibling_under_500_loc(fname: str) -> None:
    """Each sibling produced by the split stays <=500 LOC."""
    path = SOLVERS_DIR / fname
    assert path.exists(), f"{fname} missing"
    line_count = sum(1 for _ in path.open(encoding="utf-8"))
    assert line_count <= 500, f"{fname} is {line_count} LOC (>500)"
