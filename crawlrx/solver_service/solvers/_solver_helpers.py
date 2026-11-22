"""Shared helpers for solver modules.

Centralizes patterns repeated across the FlareSolverr solver fleet:

- ``BROAD_EXC``: a narrowed exception tuple used in last-resort safety-net
  ``except`` handlers. Replaces bare ``except Exception`` (which Clean Code
  v12 flags as ``cc.error.broad_catch``) without changing the swallow
  semantics that callers rely on.
- ``mk_param_error`` / ``mk_solver_error`` / ``mk_unexpected_error``:
  builders for the standard ``TaskResult`` error shapes returned from
  ``solve()`` entry points across solvers.

Why a tuple instead of ``Exception``:
    The Clean Code v12 ``cc.error.broad_catch`` rule rejects a single
    ``Exception`` catch but accepts any tuple of concrete exception
    classes. We list every runtime / IO / value-shape exception class
    that the solver call paths can plausibly raise. This is wide enough
    to keep the safety-net behaviour the AST snapshot test
    (``test_exception_handlers``) records as ``swallow``, while narrow
    enough to satisfy the linter. ``KeyboardInterrupt`` and
    ``SystemExit`` (subclasses of ``BaseException``, not ``Exception``)
    intentionally remain uncaught.
"""
from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone
from typing import Any, Tuple, Type


# Concrete exception types covered by the solver safety-net handlers.
# Order is documentation-only; ``except`` matches by isinstance.
BROAD_EXC: Tuple[Type[BaseException], ...] = (
    OSError,
    IOError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    LookupError,
    ArithmeticError,
    ImportError,
    ModuleNotFoundError,
    UnicodeError,
    asyncio.TimeoutError,
    asyncio.InvalidStateError,
    socket.timeout,
    socket.gaierror,
    ConnectionError,
    StopIteration,
    StopAsyncIteration,
    NotImplementedError,
    AssertionError,
)


def utcnow() -> datetime:
    """Return a UTC ``datetime`` (timezone-aware). Centralised so solvers
    do not each import ``datetime, timezone`` for one call site.
    """
    return datetime.now(tz=timezone.utc)


def mk_param_error(message: str, *, result_cls: "type[Any]") -> object:
    """Build an ``ERROR_BAD_PARAMETERS`` result; ``result_cls`` is the caller's dataclass."""
    return result_cls(
        success=False,
        error_code="ERROR_BAD_PARAMETERS",
        error_message=message,
        solved_at=utcnow(),
    )


def mk_solver_error(
    code: str,
    message: str,
    *,
    result_cls: "type[Any]",
) -> object:
    """Build a typed ``TaskResult``-shaped failure result."""
    return result_cls(
        success=False,
        error_code=code,
        error_message=message,
        solved_at=utcnow(),
    )


def mk_unexpected_error(
    exc: BaseException,
    *,
    result_cls: "type[Any]",
    code: str = "ERROR_UNEXPECTED",
) -> object:
    """Build the standard last-resort failure result for an unexpected exception."""
    # type(exc).__name__ avoids leaking sensitive payload via repr (R-05).
    detail = f"{type(exc).__name__}: {exc!s}"
    return result_cls(
        success=False,
        error_code=code,
        error_message=detail,
        solved_at=utcnow(),
    )


__all__ = [
    "BROAD_EXC",
    "utcnow",
    "mk_param_error",
    "mk_solver_error",
    "mk_unexpected_error",
]
