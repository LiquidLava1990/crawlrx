"""
DEPRECATED: Backward compatibility shim.
Use ``from ..solver_base import BaseSolver`` (package root) directly.
"""
from ..solver_base import BaseSolver, SolverError, SolverTimeoutError  # noqa: F401

__all__ = ["BaseSolver", "SolverError", "SolverTimeoutError"]
