"""Regression: solver registration must consume SolverConfig via its .dict() contract.

SolverConfig is a stdlib dataclass with its own dict() serializer — it is not a
pydantic model, so model_dump() raises AttributeError and silently drops every
solver from the registry (caught live: 12+ solvers failed registration with
"'SolverConfig' object has no attribute 'model_dump'").
"""
from __future__ import annotations

from typing import Any, Dict

from crawlrx.solver_service.config import SolverConfig
from crawlrx.solver_service._main_solver_registration import _try_register


def test_solver_config_dict_returns_plain_dict() -> None:
    """The dataclass serializer exposes constructor kwargs as a plain dict."""
    cfg = SolverConfig(enabled=True, timeout=5.0)
    payload = cfg.dict()
    assert isinstance(payload, dict)
    assert payload["enabled"] is True
    assert payload["timeout"] == 5.0


def test_registration_passes_dict_into_solver_constructor(monkeypatch) -> None:
    """_try_register hands solver classes a dict built from SolverConfig.dict()."""

    captured: Dict[str, Any] = {}

    class _RecordingSolver:
        def __init__(self, config: Dict[str, Any]) -> None:
            captured.update(config)

    monkeypatch.setattr(
        "crawlrx.solver_service.solvers.tls_solver.TLSSolver", _RecordingSolver,
    )

    ok, detail = _try_register(
        "tls", "crawlrx.solver_service.solvers.tls_solver", "TLSSolver",
    )
    assert ok, f"registration failed: {detail}"
    assert isinstance(captured, dict), "solver received non-dict config"
    assert "enabled" in captured
