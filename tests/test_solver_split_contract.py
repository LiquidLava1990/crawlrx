"""TDD contract for the godfile splits of stealth_browser + camoufox_solver.

Invariants:
- Public class names remain importable from the original module path.
- Each sibling under solvers/ stays <=500 LOC.
- Public attributes/methods documented as load-bearing remain reachable.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SOLVERS_DIR = Path(__file__).resolve().parents[1] / "crawlrx" / "solver_service" / "solvers"


def _import_solver(name: str):
    """Import a solver the same way the production registry does (``crawlrx.solver_service.solvers.X``)."""
    return importlib.import_module(f"crawlrx.solver_service.solvers.{name}")


def test_stealth_browser_public_surface() -> None:
    """StealthBrowserSolver, StealthBrowserPool, BrowserFingerprint, BrowserInstance, create_stealth_solver remain importable."""
    mod = _import_solver("stealth_browser")
    for name in (
        "StealthBrowserSolver",
        "StealthBrowserPool",
        "BrowserFingerprint",
        "BrowserInstance",
        "create_stealth_solver",
    ):
        assert hasattr(mod, name), f"missing public symbol: {name}"


def test_camoufox_solver_public_surface() -> None:
    """CamoufoxSolver + create_solver + BrowserPool remain importable."""
    mod = _import_solver("camoufox_solver")
    for name in ("CamoufoxSolver", "BrowserPool", "BrowserInstance", "create_solver"):
        assert hasattr(mod, name), f"missing public symbol: {name}"


@pytest.mark.parametrize(
    "fname",
    [
        "stealth_browser.py",
        "_stealth_browser_pool.py",
        "_stealth_browser_launch.py",
        "_stealth_browser_fingerprint.py",
        "camoufox_solver.py",
        "_camoufox_strategies.py",
        "_camoufox_browser.py",
        "_camoufox_helpers.py",
    ],
)
def test_sibling_under_500_loc(fname: str) -> None:
    """Each sibling produced by the split stays <=500 LOC (excluding blanks/comments not counted -- raw)."""
    path = SOLVERS_DIR / fname
    assert path.exists(), f"{fname} missing"
    line_count = sum(1 for _ in path.open(encoding="utf-8"))
    assert line_count <= 500, f"{fname} is {line_count} LOC (>500)"


def test_stealth_solver_methods_intact() -> None:
    """The orchestrator still exposes solve, health_check, close."""
    mod = _import_solver("stealth_browser")
    cls = mod.StealthBrowserSolver
    for name in ("solve", "health_check", "close"):
        assert hasattr(cls, name)


def test_camoufox_solver_methods_intact() -> None:
    """The orchestrator still exposes solve, health_check, close + handler factory."""
    mod = _import_solver("camoufox_solver")
    cls = mod.CamoufoxSolver
    for name in ("solve", "health_check", "close", "_captcha_handler_for"):
        assert hasattr(cls, name)
