"""``sentinel_errors`` shim shared by every ``_camoufox_*`` sibling.

Centralising the shim keeps the boilerplate to a single place; every sibling
imports from here instead of repeating the path-walk + try/except shim.
"""
from __future__ import annotations

import asyncio
from typing import Any


try:
    from ..core.sentinel_errors import ErrCode, emit  # noqa: F401
except ImportError:
    class _ErrCodeFallback:
        """Minimal stand-in when sentinel_errors is unavailable."""

        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode: Any = _ErrCodeFallback()

    def emit(*a: object, **kw: object) -> None:  # noqa: ARG001
        """No-op fallback for ``sentinel_errors.emit`` when the package is absent."""
        return None

# BROAD_EXC also re-exported here so callers don't need a second path-shim
from ._solver_helpers import BROAD_EXC  # noqa: F401

# Let router timeouts and shutdown cancellation propagate through every sibling.
BROAD_EXC = tuple(error for error in BROAD_EXC if error is not asyncio.CancelledError)


__all__ = ["BROAD_EXC", "ErrCode", "emit"]
