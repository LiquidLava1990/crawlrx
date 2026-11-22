"""Module-level helpers for `solver_router.py` (extracted to keep file <500 LOC).

Pure functions / leaf coroutines used by `SolverRouter`. Kept here so the
router class stays focused on orchestration while these helpers get reused
and tested independently.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict

from .models import TaskResult
from .solver_base import BaseSolver, SolverError


RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ERROR_SOLVER_NOT_AVAILABLE", "ERROR_SOLVER_UNHEALTHY", "ERROR_SOLVER_NOT_FOUND",
     "ERROR_SOLVER_NOT_LOADED", "ERROR_TIMEOUT", "ERROR_CAPTCHA_UNSOLVABLE"}
)


def build_error_result(error_code: str, error_message: str, start_time: float) -> TaskResult:
    """Construct a failed TaskResult with consistent timestamp + solve-time fields."""
    return TaskResult(
        success=False,
        error_code=error_code,
        error_message=error_message,
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=int((time.time() - start_time) * 1000),
    )


def _from_dict(raw: Dict[str, Any]) -> TaskResult:
    """Build a TaskResult from a dict-shaped solver return."""
    return TaskResult(
        success=raw.get("success", False),
        token=raw.get("token"),
        error_code=raw.get("error_code", raw.get("error")),
        error_message=raw.get("error_message", raw.get("error")),
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=raw.get("duration_ms") or raw.get("solve_time_ms"),
    )


def _from_attrs(raw: Any) -> TaskResult:
    """Build a TaskResult by reading attributes off `raw`."""
    return TaskResult(
        success=getattr(raw, "success", False),
        token=getattr(raw, "token", None),
        error_code=getattr(raw, "error_code", None),
        error_message=getattr(raw, "error_message", getattr(raw, "message", None)),
        solved_at=datetime.now(tz=timezone.utc),
    )


def normalize_solver_result(raw_result: object) -> TaskResult:
    """Coerce a solver's raw return value into a TaskResult."""
    if isinstance(raw_result, TaskResult):
        return raw_result
    if isinstance(raw_result, dict):
        return _from_dict(raw_result)
    if hasattr(raw_result, "to_task_result"):
        return raw_result.to_task_result()
    return _from_attrs(raw_result)


async def safe_solver_health_check(solver: BaseSolver) -> Dict[str, Any]:
    """Run a solver's health_check (sync or coroutine) and trap failures."""
    try:
        result = solver.health_check()
        if asyncio.iscoroutine(result):
            return await result
        return result
    except (SolverError, RuntimeError, OSError, ValueError) as e:
        return {"healthy": False, "error": str(e)}
