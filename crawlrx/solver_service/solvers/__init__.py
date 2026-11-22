"""Solver modules for the challenge-gateway service.

Solver registration lives in :mod:`crawlrx.solver_service._main_solver_registration`
(the single source of truth for the registry and its enablement gating).
Import solver classes from their modules directly.
"""
from ..solver_base import BaseSolver

__all__ = ["BaseSolver"]
