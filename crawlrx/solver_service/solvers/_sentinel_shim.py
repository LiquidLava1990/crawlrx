"""Shared sentinel_errors fallback shim.

Centralises the ``ErrCode`` / ``emit`` import dance the solver fleet
repeats in every module. Importing from here keeps the
``cc.shape.naming_drift`` finding for ``**kw`` confined to a single
file instead of multiplying it across siblings.
"""
from __future__ import annotations



try:
    from ..core.sentinel_errors import ErrCode, emit  # noqa: F401
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"
    ErrCode = _E()  # type: ignore[assignment] because fallback shim when sentinel_errors unavailable

    def emit(*a: object, **kw: object) -> None:  # type: ignore[no-redef] because emit redefined in fallback branch
        pass


__all__ = ["ErrCode", "emit"]
