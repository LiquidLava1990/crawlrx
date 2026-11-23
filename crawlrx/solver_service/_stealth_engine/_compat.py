"""Layout-resilient imports for the stealth engine.

The engine lives inside ``crawlrx.solver_service`` in the release tree and as
a top-level ``_stealth_engine`` package in the master tree (``crawlrx-solver``).
``sibling("solvers._stealth_browser_fingerprint")`` resolves the right import
path for whichever layout is active and caches the verdict.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

_LAYOUT: str | None = None


def layout() -> str:
    """'package' (crawlrx.solver_service.*) or 'flat' (top-level solvers.*)."""
    global _LAYOUT
    if _LAYOUT is None:
        try:
            importlib.import_module("crawlrx.solver_service.solvers.solver_base")
            _LAYOUT = "package"
        except ImportError:
            _LAYOUT = "flat"
    return _LAYOUT


def engine_module(name: str) -> Any:
    """Import this engine's own submodule under the active layout."""
    return importlib.import_module(
        f"crawlrx.solver_service._stealth_engine.{name}"
        if layout() == "package"
        else f"_stealth_engine.{name}"
    )


def sibling(name: str) -> Any:
    """Import a module that lives beside this engine (solvers/, botright_patches/, core/)."""
    if layout() == "package":
        return importlib.import_module(f"crawlrx.solver_service.{name}")
    return importlib.import_module(name)


def core_module(name: str) -> Any:
    """Import from the engine's core/ helpers under either layout."""
    if layout() == "package":
        return importlib.import_module(f"crawlrx.solver_service.core.{name}")
    return importlib.import_module(f"core.{name}")
