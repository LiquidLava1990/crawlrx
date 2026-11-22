"""TDD contract for the datadome_solver godfile split.

Invariants captured before splitting (RED -> GREEN -> REFACTOR):
- Public class names remain importable from the original module path.
- Public method/attribute set on ``DataDomeSolver`` is preserved.
- Each split sibling under ``solvers/`` stays <=500 LOC.
- The orchestrator (``datadome_solver.py``) is itself <=500 LOC.
- The carry-forward Blocker ``cc.bug.python_assert_in_production`` is gone.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SOLVERS_DIR = Path(__file__).resolve().parents[1] / "crawlrx" / "solver_service" / "solvers"


def _import_solver(name: str):
    """Import a solver the same way the production registry does (``crawlrx.solver_service.solvers.X``)."""
    return importlib.import_module(f"crawlrx.solver_service.solvers.{name}")


def test_datadome_public_surface() -> None:
    """DataDomeSolver + DataDomeCookieStore + FingerprintManager remain importable."""
    mod = _import_solver("datadome_solver")
    for name in (
        "DataDomeSolver",
        "DataDomeCookieStore",
        "FingerprintManager",
    ):
        assert hasattr(mod, name), f"missing public symbol: {name}"


def test_datadome_solver_methods_intact() -> None:
    """The orchestrator still exposes the load-bearing methods."""
    mod = _import_solver("datadome_solver")
    cls = mod.DataDomeSolver
    for name in (
        "solve",
        "health_check",
        "is_datadome_protected",
        "extract_datadome_cookie",
    ):
        assert hasattr(cls, name), f"DataDomeSolver lost public method: {name}"


def test_datadome_strategies_methods_reachable() -> None:
    """Strategy methods stay attached to the class (used internally + by tests)."""
    mod = _import_solver("datadome_solver")
    cls = mod.DataDomeSolver
    for name in (
        "_try_cookie_strategy",
        "_try_tls_client_strategy",
        "_try_camoufox_strategy",
        "_try_botright_strategy",
        "_try_playwright_strategy",
    ):
        assert hasattr(cls, name), f"DataDomeSolver lost strategy method: {name}"


def test_datadome_class_constants_preserved() -> None:
    """Detection / config constants stay on the class."""
    mod = _import_solver("datadome_solver")
    cls = mod.DataDomeSolver
    assert getattr(cls, "solver_type") == "datadome"
    assert hasattr(cls, "DATADOME_INDICATORS")
    assert hasattr(cls, "STRATEGY_PRIORITY")
    assert hasattr(cls, "DEFAULT_TIMEOUT")
    assert hasattr(cls, "MAX_RETRIES")


@pytest.mark.parametrize(
    "fname",
    [
        "datadome_solver.py",
        "_datadome_strategies.py",
        "_datadome_browser.py",
        "_datadome_tls.py",
    ],
)
def test_datadome_sibling_under_500_loc(fname: str) -> None:
    """Each sibling produced by the split stays <=500 LOC."""
    path = SOLVERS_DIR / fname
    assert path.exists(), f"{fname} missing"
    line_count = sum(1 for _ in path.open(encoding="utf-8"))
    assert line_count <= 500, f"{fname} is {line_count} LOC (>500)"


def test_datadome_no_python_assert_in_solve() -> None:
    """Carry-forward python_assert_in_production blocker must be gone."""
    src = (SOLVERS_DIR / "datadome_solver.py").read_text(encoding="utf-8")
    assert "assert url is not None" not in src, (
        "carry-forward Blocker (python_assert_in_production) still present"
    )
