"""Retry-with-exponential-backoff helpers.

Used by the rest of CrawlRX as a decorator (``with_retry``) and by tests
as low-level building blocks (``_RetryPolicy`` / ``_run_with_retry``).
"""
from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

_RETRY_PROPAGATING_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    RuntimeError,
    ValueError,
    TimeoutError,
    ConnectionError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RetryPolicy:
    """Bundled retry policy for the with_retry helper chain."""

    max_attempts: int = 3
    backoff_base: float = 1.5


async def _retry_one_attempt(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    attempt: int,
    policy: _RetryPolicy,
) -> tuple[bool, Any, Optional[BaseException]]:
    """Single retry attempt: returns (succeeded, value, error)."""
    try:
        value = await fn(*args, **kwargs)
        return True, value, None
    except _RETRY_PROPAGATING_EXCEPTIONS as exc:
        if attempt < policy.max_attempts:
            wait = policy.backoff_base ** attempt
            logger.warning(
                "Attempt %d/%d failed for %s: %s - retrying in %.1fs",
                attempt, policy.max_attempts, fn.__name__, exc, wait,
            )
            await asyncio.sleep(wait)
        return False, None, exc


async def _run_with_retry(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    policy: _RetryPolicy,
) -> Any:
    """Run ``fn(*args, **kwargs)`` with exponential backoff retries."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        ok, value, err = await _retry_one_attempt(fn, args, kwargs, attempt, policy)
        if ok:
            return value
        last_err = err
    logger.error(
        "All %d attempts failed for %s: %s",
        policy.max_attempts, fn.__name__, last_err,
    )
    if last_err is None:
        raise RuntimeError(
            f"_run_with_retry exhausted {policy.max_attempts} attempts for "
            f"{fn.__name__} but captured no error; contract violation -- "
            "the loop must record an error on each failed attempt"
        )
    raise last_err


def with_retry(max_attempts: int = 3, backoff_base: float = 1.5) -> Callable:
    """Decorator: retry async ``fn`` with exponential backoff on transient errors."""
    policy = _RetryPolicy(max_attempts=max_attempts, backoff_base=backoff_base)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> object:
            return await _run_with_retry(fn, args, kwargs, policy)
        return wrapper
    return decorator
