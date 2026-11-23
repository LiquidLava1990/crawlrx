"""Solver modules for the challenge-gateway service.

Solver registration lives in :mod:`crawlrx.solver_service._main_solver_registration`
(the single source of truth for the registry and its enablement gating).
Import solver classes from their modules directly.

This package must stay importable both ways:

* as ``crawlrx.solver_service.solvers`` (wheel / release tree), and
* as top-level ``solvers`` (the unified container runs ``uvicorn main:app``
  from the solver root, and solver modules import siblings flat, e.g.
  ``from _datadome_browser import ...``).

Putting this directory on ``sys.path`` restores the original stack's
contract so flat sibling imports resolve under either layout.
"""
import os
import sys

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from ..solver_base import BaseSolver  # noqa: E402

__all__ = ["BaseSolver"]
