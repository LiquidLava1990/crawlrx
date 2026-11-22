"""
Logging setup for FlareSolverr using loguru.
Provides console output (colored) and file output (rotating).
Per-solver context, request/response logging, and performance timing.
"""

import contextlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Optional, TypeVar

if TYPE_CHECKING:
    from types import TracebackType

from functools import wraps

from loguru import logger

from .config import get_config, Config



def emit(*a: object, **kw: object) -> None:
    """No-op emitter used when trident core is unavailable."""
    return None


F = TypeVar("F", bound=Callable[..., Any])


# Context variables for per-solver logging
solver_context: ContextVar[Optional[str]] = ContextVar('solver', default=None)
task_context: ContextVar[Optional[str]] = ContextVar('task_id', default=None)
request_context: ContextVar[Optional[str]] = ContextVar('request_id', default=None)


class InterceptHandler:
    """Intercept standard logging and redirect to loguru."""

    def write(self, message: str) -> None:
        logger.opt(depth=1, exception=None).info(message.strip())

    def flush(self) -> None:
        pass


def _add_console_sink(level: str) -> None:
    """Attach the colored stdout sink."""
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{extra[solver]: <12}</cyan> | "
               "<yellow>{extra[task_id]: <10}</yellow> | "
               "<level>{message}</level>",
        colorize=True,
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )


def _add_rotating_file_sink(log_dir: Path, level: str) -> None:
    """Attach the rotating file sink (10MB / 7 days)."""
    logger.add(
        log_dir / "flaresolverr_{time:YYYY-MM-DD}.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
               "{extra[solver]: <12} | {extra[task_id]: <10} | {message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        enqueue=True,
        encoding="utf-8",
    )


def _add_error_file_sink(log_dir: Path) -> None:
    """Attach the error-only file sink (10MB / 30 days)."""
    logger.add(
        log_dir / "errors_{time:YYYY-MM-DD}.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
               "{extra[solver]: <12} | {extra[task_id]: <10} | {message}\n{exception}",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
        enqueue=True,
        encoding="utf-8",
    )


def setup_logging(config: Optional[Config] = None) -> None:
    """Configure loguru logging with console and file outputs."""
    if config is None:
        config = get_config()

    logger.remove()
    _add_console_sink(config.log_level)

    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    _add_rotating_file_sink(log_dir, config.log_level)
    _add_error_file_sink(log_dir)

    global _log
    _log = logger.bind(solver="system", task_id="-", request_id="-")
    _log.info("Logging initialized | Level: {} | LogDir: {}", config.log_level, log_dir)


# Module-level logger accessor
_log = logger


def get_logger(solver: Optional[str] = None, task_id: Optional[str] = None) -> object:
    """Get a logger with bound context."""
    ctx = {
        'solver': solver or solver_context.get(None) or "system",
        'task_id': task_id or task_context.get(None) or "-",
        'request_id': request_context.get(None) or "-",
    }
    return logger.bind(**ctx)


def set_solver_context(solver_name: str) -> None:
    """Set the current solver context."""
    solver_context.set(solver_name)


def set_task_context(task_id: str) -> None:
    """Set the current task context."""
    task_context.set(task_id)


def set_request_context(request_id: str) -> None:
    """Set the current request context."""
    request_context.set(request_id)


def clear_context() -> None:
    """Clear all logging context."""
    solver_context.set(None)
    task_context.set(None)
    request_context.set(None)


class SolverLogContext:
    """Context manager for solver logging."""

    def __init__(self, solver: str, task_id: Optional[str] = None) -> None:
        self.solver = solver
        self.task_id = task_id
        self._solver_token = None
        self._task_token = None

    def __enter__(self) -> 'SolverLogContext':
        self._solver_token = solver_context.set(self.solver)
        if self.task_id:
            self._task_token = task_context.set(self.task_id)
        return self

    def __exit__(self, exc_type: "type[BaseException] | None", exc_val: "BaseException | None", exc_tb: "TracebackType | None") -> None:
        if self._task_token:
            task_context.reset(self._task_token)
        solver_context.reset(self._solver_token)


class RequestLogContext:
    """Context manager for request logging."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._token = None

    def __enter__(self) -> 'RequestLogContext':
        self._token = request_context.set(self.request_id)
        return self

    def __exit__(self, exc_type: "type[BaseException] | None", exc_val: "BaseException | None", exc_tb: "TracebackType | None") -> None:
        request_context.reset(self._token)


@dataclass
class RequestLogEntry:
    """Structured fields for a single HTTP access-log line."""
    method: str
    path: str
    status_code: int
    duration_ms: float
    client_ip: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


def _status_marker(status_code: int) -> str:
    """Map HTTP status code to a single-glyph success/fail/warn marker."""
    if 200 <= status_code < 300:
        return "[OK]"
    if status_code >= 400:
        return "[X]"
    return "[!]"


def log_request(entry: RequestLogEntry) -> None:
    """Log an HTTP request from a structured entry."""
    log = get_logger()
    log.info(
        "{} {} {} | {} | {:.2f}ms | {}",
        _status_marker(entry.status_code),
        entry.method,
        entry.path,
        entry.status_code,
        entry.duration_ms,
        entry.client_ip or "-",
        **(entry.extra or {}),
    )


def log_api_call(endpoint: str, payload_size: int, response_size: int,
                 duration_ms: float, success: bool) -> None:
    """Log an API call with timing."""
    log = get_logger()
    status = "OK" if success else "FAIL"
    log.info(
        "API {} | {} | payload={}B response={}B | {:.2f}ms",
        endpoint, status, payload_size, response_size, duration_ms,
    )


def log_performance(operation: str, duration_ms: float,
                    details: Optional[Dict[str, Any]] = None) -> None:
    """Log performance metrics."""
    log = get_logger()
    detail_str = ""
    if details:
        detail_str = " | " + " ".join(f"{k}={v}" for k, v in details.items())
    log.info("PERF {} | {:.2f}ms{}", operation, duration_ms, detail_str)


def log_solver_start(solver: str, task_id: str, captcha_type: str,
                     details: Optional[Dict[str, Any]] = None) -> None:
    """Log solver task start."""
    log = get_logger(solver, task_id)
    detail_str = ""
    if details:
        detail_str = " | " + " ".join(f"{k}={v}" for k, v in details.items())
    log.info("? START {} | {}{}", solver, captcha_type, detail_str)


def log_solver_end(solver: str, task_id: str, success: bool,
                   duration_ms: float, result: Optional[str] = None) -> None:
    """Log solver task completion."""
    log = get_logger(solver, task_id)
    status = "[OK] SUCCESS" if success else "[X] FAILED"
    if result and len(result) > 50:
        result_str = f" | result={result[:50]}..."
    elif result:
        result_str = f" | result={result}"
    else:
        result_str = ""
    log.info("? END {} | {} | {:.2f}ms{}", solver, status, duration_ms, result_str)


def log_queue_event(event: str, queue_depth: int, task_id: Optional[str] = None) -> None:
    """Log queue state change."""
    log = get_logger(task_id=task_id)
    log.info("QUEUE {} | depth={}", event, queue_depth)


_TIMING_TRACKED_ERRORS = (
    RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError, ImportError,
)


def _emit_timing(
    operation: str, start: float, status: str, error_message: Optional[str] = None,
) -> None:
    """Common emit path for the timing decorator's success and error branches."""
    duration_ms = (time.perf_counter() - start) * 1000
    details: Dict[str, Any] = {"status": status}
    if error_message is not None:
        details["error"] = error_message
    log_performance(operation, duration_ms, details)


@contextlib.contextmanager
def _timing_block(operation: str) -> Iterator[None]:
    """Context manager that emits success/error timing metrics for a code block."""
    start = time.perf_counter()
    try:
        yield
    except _TIMING_TRACKED_ERRORS as exc:
        _emit_timing(operation, start, "error", error_message=str(exc))
        raise
    _emit_timing(operation, start, "success")


class TimingLogger:
    """Decorator to log function execution time under `operation`."""

    __slots__ = ("operation",)

    def __init__(self, operation: str) -> None:
        self.operation = operation

    def __call__(self: object, func: F) -> F:
        op = self.operation

        @wraps(func)
        def invoke(*args: object, **kwargs: object) -> object:
            with _timing_block(op):
                return func(*args, **kwargs)
        return invoke  # type: ignore[return-value] because decorator preserves callable signature via @wraps; mypy can't track ParamSpec through nested decorator


# Legacy callsite alias for the PascalCase canonical (remove by 2026-10-01).
timing_logger = TimingLogger  # noqa: N816


class PerformanceTimer:
    """Context manager for timing code blocks."""

    def __init__(self, operation: str, log_on_exit: bool = True) -> None:
        self.operation = operation
        self.log_on_exit = log_on_exit
        self.start_time: Optional[float] = None
        self.duration_ms: Optional[float] = None

    def __enter__(self) -> 'PerformanceTimer':
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type: "type[BaseException] | None", exc_val: "BaseException | None", exc_tb: "TracebackType | None") -> None:
        if self.start_time is not None:
            self.duration_ms = (time.perf_counter() - self.start_time) * 1000

            if self.log_on_exit:
                status = "error" if exc_type else "success"
                log_performance(self.operation, self.duration_ms, {"status": status})

    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        if self.duration_ms is not None:
            return self.duration_ms
        if self.start_time is not None:
            return (time.perf_counter() - self.start_time) * 1000
        return 0.0
