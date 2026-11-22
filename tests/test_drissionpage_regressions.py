"""Regression tests for the DrissionPage solver service module.

These pin three fixes: ``_extract_token`` must be a real instance method, the
iframe selector tuple must be assigned exactly once, and ``asyncio`` must be
imported at module level (not inside ``_chromium_body``).
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

# DrissionPage is an optional heavy runtime dependency of the solver module.
pytest.importorskip("DrissionPage", reason="drissionpage_solver needs the optional DrissionPage runtime")

from crawlrx.solver_service.solvers import drissionpage_solver  # noqa: E402
from crawlrx.solver_service.solvers.drissionpage_solver import DrissionPageSolver  # noqa: E402

EXPECTED_IFRAME_SELECTORS = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]',
    'iframe[src*="challenge"]',
    ".g-recaptcha iframe",
    ".h-captcha iframe",
)


def _make_solver() -> DrissionPageSolver:
    """Build a solver from a minimal config dict, exactly as BaseSolver expects."""
    return DrissionPageSolver({"enabled": True, "timeout": 30, "max_retries": 2})


def test_extract_token_is_instance_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_extract_token`` binds ``self`` and routes to the selector/datadome helpers."""
    assert not isinstance(inspect.getattr_static(DrissionPageSolver, "_extract_token"), staticmethod)

    solver = _make_solver()
    fake_page: Any = object()
    selector_calls: list[str] = []
    datadome_calls: list[Any] = []
    selector_result: list[str] = [""]
    datadome_result: list[str] = [""]

    def fake_from_selectors(page: Any, captcha_type: str) -> str:
        selector_calls.append(captcha_type)
        return selector_result[0]

    def fake_datadome_cookie(page: Any) -> str:
        datadome_calls.append(page)
        return datadome_result[0]

    monkeypatch.setattr(solver, "_extract_token_from_selectors", fake_from_selectors)
    monkeypatch.setattr(solver, "_extract_datadome_cookie_value", fake_datadome_cookie)

    # datadome: selectors are consulted first; a falsy result falls through to the cookie path.
    selector_result[0] = ""
    datadome_result[0] = "dd-token-sentinel"
    assert solver._extract_token(fake_page, "datadome") == "dd-token-sentinel"
    assert selector_calls == ["datadome"]
    assert datadome_calls == [fake_page]

    # any other type routes to the selectors helper only.
    selector_calls.clear()
    datadome_calls.clear()
    selector_result[0] = "sel-token-sentinel"
    assert solver._extract_token(fake_page, "recaptcha") == "sel-token-sentinel"
    assert selector_calls == ["recaptcha"]
    assert datadome_calls == []

    # a falsy selector result for a non-datadome type yields None without touching datadome.
    selector_calls.clear()
    selector_result[0] = ""
    assert solver._extract_token(fake_page, "hcaptcha") is None
    assert selector_calls == ["hcaptcha"]
    assert datadome_calls == []


def test_iframe_selector_tuple_defined_once() -> None:
    """The iframe selector tuple holds the six canonical selectors, assigned exactly once."""
    assert drissionpage_solver._DRISSIONPAGE_IFRAME_SELECTORS == EXPECTED_IFRAME_SELECTORS
    source = Path(str(drissionpage_solver.__file__)).read_text(encoding="utf-8")
    assert source.count("_DRISSIONPAGE_IFRAME_SELECTORS = (") == 1


def test_asyncio_imported_at_module_level() -> None:
    """``asyncio`` resolves on the module, proving a top-of-file import."""
    assert drissionpage_solver.asyncio is asyncio
