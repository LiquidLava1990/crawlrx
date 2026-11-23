"""Solver modules for the challenge-gateway service.

Solver registration lives in :mod:`crawlrx.solver_service._main_solver_registration`
(the single source of truth for the registry and its enablement gating).
Import solver classes from their modules directly.

This package must stay importable both ways:

* as ``crawlrx.solver_service.solvers`` (wheel / release tree), and
* as top-level ``solvers`` (the unified container runs ``uvicorn main:app``
  from the solver root, and solver modules import siblings flat, e.g.
  ``from _datadome_browser import ...``).

Putting the tree root and this directory on ``sys.path`` (root first)
restores the original stack's contract so flat sibling imports resolve
under either layout, and top-level ``models.py`` / ``solver_base.py`` /
``core`` / ``_stealth_engine`` resolve from the tree root. Root must
precede this directory, or this dir's ``models/`` download-helper package
would shadow the root ``models.py`` (pydantic ``Task``/``TaskResult``).
"""
import os
import sys


def _norm(path: str) -> str:
    """Normalize a sys.path entry for identity comparison across platforms."""
    return os.path.normcase(os.path.normpath(path))


_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_dir)

# Idempotent, order-deterministic bootstrap: drop stale copies of both
# entries wherever they sit, then re-insert root first, this dir second.
_expected = {_norm(_root), _norm(_dir)}
sys.path[:] = [entry for entry in sys.path if _norm(entry) not in _expected]
sys.path.insert(0, _dir)
sys.path.insert(0, _root)

try:
    from ..solver_base import BaseSolver  # package layout
except ImportError:  # flat layout (master tree): solver_base sits beside this dir
    from solver_base import BaseSolver  # type: ignore[no-redef]

__all__ = ["BaseSolver"]
