"""Per-domain circuit breaker + decorator wrapper.

Three-state breaker (CLOSED -> OPEN -> HALF_OPEN) tracks recent failure
timestamps in a sliding window, opens after ``FAILURE_THRESHOLD``
failures inside ``WINDOW_SECONDS``, and probes recovery via a single
test request after ``COOLDOWN_SECONDS`` elapse.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

_RETRY_PROPAGATING_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    RuntimeError,
    ValueError,
    TimeoutError,
    ConnectionError,
)

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Per-domain circuit breaker (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)."""

    FAILURE_THRESHOLD = 5
    COOLDOWN_SECONDS = 30
    WINDOW_SECONDS = 60

    STATE_CLOSED = "closed"
    STATE_OPEN = "open"
    STATE_HALF_OPEN = "half_open"

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._lock = threading.Lock()
        self._state = self.STATE_CLOSED
        self._failure_times: List[float] = []
        self._opened_at: Optional[float] = None
        self._half_open_in_flight = False

    # -- public API ----------------------------------------------------------

    @property
    def state(self) -> str:
        """Current state (recalculated on every access)."""
        with self._lock:
            return self._get_state_locked()

    def is_open(self) -> bool:
        """True if the breaker should reject this request immediately."""
        with self._lock:
            state = self._get_state_locked()
            if state == self.STATE_OPEN:
                return True
            if state != self.STATE_HALF_OPEN:
                return False
            if self._half_open_in_flight:
                return True
            self._half_open_in_flight = True
            return False

    def record_success(self) -> None:
        """Reset the breaker after a successful request."""
        with self._lock:
            self._failure_times.clear()
            self._opened_at = None
            self._half_open_in_flight = False
            prev = self._state
            self._state = self.STATE_CLOSED
            if prev != self.STATE_CLOSED:
                logger.info(
                    "CircuitBreaker[%s]: %s -> CLOSED (recovered)",
                    self.domain, prev.upper(),
                )

    def record_failure(self) -> None:
        """Record one failure; trip the breaker if over threshold."""
        now = time.time()
        with self._lock:
            self._half_open_in_flight = False
            self._failure_times = [
                t for t in self._failure_times if now - t < self.WINDOW_SECONDS
            ]
            self._failure_times.append(now)
            if (
                len(self._failure_times) >= self.FAILURE_THRESHOLD
                and self._state != self.STATE_OPEN
            ):
                self._state = self.STATE_OPEN
                self._opened_at = now
                logger.warning(
                    "CircuitBreaker[%s]: CLOSED -> OPEN (%d failures in %ds window)",
                    self.domain, len(self._failure_times), self.WINDOW_SECONDS,
                )

    def as_dict(self) -> dict:
        """Serialisable snapshot for the ``/circuit-breakers`` endpoint."""
        with self._lock:
            state = self._get_state_locked()
            now = time.time()
            cooldown_remaining = None
            if self._opened_at and state == self.STATE_OPEN:
                cooldown_remaining = max(
                    0.0, self.COOLDOWN_SECONDS - (now - self._opened_at)
                )
            return {
                "domain": self.domain,
                "state": state,
                "failure_count": len(self._failure_times),
                "failure_threshold": self.FAILURE_THRESHOLD,
                "cooldown_seconds": self.COOLDOWN_SECONDS,
                "window_seconds": self.WINDOW_SECONDS,
                "cooldown_remaining_seconds": (
                    round(cooldown_remaining, 1)
                    if cooldown_remaining is not None
                    else None
                ),
                "opened_at": self._opened_at,
            }

    # -- internal ------------------------------------------------------------

    def _get_state_locked(self) -> str:
        """Must be called with ``self._lock`` held."""
        if self._state == self.STATE_OPEN and self._opened_at is not None:
            elapsed = time.time() - self._opened_at
            if elapsed >= self.COOLDOWN_SECONDS:
                self._state = self.STATE_HALF_OPEN
                logger.info(
                    "CircuitBreaker[%s]: OPEN -> HALF_OPEN (cooldown elapsed %.1fs)",
                    self.domain, elapsed,
                )
        return self._state


# ---------------------------------------------------------------------------
# Domain registry
# ---------------------------------------------------------------------------

_cb_registry: Dict[str, CircuitBreaker] = {}
_cb_registry_lock = threading.Lock()


def get_circuit_breaker(domain: str) -> CircuitBreaker:
    """Return the singleton :class:`CircuitBreaker` for ``domain``."""
    with _cb_registry_lock:
        if domain not in _cb_registry:
            _cb_registry[domain] = CircuitBreaker(domain)
        return _cb_registry[domain]


def get_all_circuit_breakers() -> Dict[str, CircuitBreaker]:
    """Thread-safe snapshot dict of all known circuit breakers."""
    with _cb_registry_lock:
        return dict(_cb_registry)


def _resolve_circuit_domain(args: tuple, kwargs: dict) -> str:
    """First positional arg (or ``url`` kwarg) -> netloc, fallback to raw."""
    url = args[0] if args else kwargs.get("url", "")
    try:
        return urlparse(url).netloc or url
    except (ValueError, AttributeError):
        return url


def _invoke_with_circuit(func: Callable, args: tuple, kwargs: dict) -> Any:
    """Run ``func`` under the circuit breaker derived from its url arg."""
    domain = _resolve_circuit_domain(args, kwargs)
    breaker = get_circuit_breaker(domain)
    if breaker.is_open():
        raise RuntimeError(f"Circuit open for domain: {domain}")
    try:
        result = func(*args, **kwargs)
    except _RETRY_PROPAGATING_EXCEPTIONS:
        breaker.record_failure()
        raise
    breaker.record_success()
    return result


async def circuit_protected(func: Callable) -> Callable:
    """Decorator: wrap a sync ``func`` with per-domain circuit-breaker protection."""
    @functools.wraps(func)
    async def wrapper(*args: object, **kwargs: object) -> object:
        return _invoke_with_circuit(func, args, kwargs)
    return wrapper
