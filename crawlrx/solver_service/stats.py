"""
Statistics tracking for FlareSolverr.
In-memory stats (reset on restart) with thread-safe counters.
Provides data for GET /stats endpoint.
"""

import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from collections import defaultdict
from statistics import mean

# Sentinel error tracking


_RECENT_SAMPLE_WINDOW: int = 100
_MAX_RETAINED_SAMPLES: int = 1000


@dataclass
class _BaseRunStats:
    """Shared counters/timings for any solve-tracking entity."""
    tasks_submitted: int = 0
    tasks_solved: int = 0
    tasks_failed: int = 0
    total_solve_time_ms: float = 0.0
    solve_times_ms: List[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        total = self.tasks_solved + self.tasks_failed
        if total == 0:
            return 0.0
        return (self.tasks_solved / total) * 100

    @property
    def avg_solve_time_ms(self) -> float:
        """Average over the last `_RECENT_SAMPLE_WINDOW` samples."""
        if not self.solve_times_ms:
            return 0.0
        return mean(self.solve_times_ms[-_RECENT_SAMPLE_WINDOW:])

    def record_success(self, duration_ms: float) -> None:
        """Record a successful solve and trim the sample window."""
        self.tasks_solved += 1
        self.total_solve_time_ms += duration_ms
        self.solve_times_ms.append(duration_ms)
        if len(self.solve_times_ms) > _MAX_RETAINED_SAMPLES:
            self.solve_times_ms = self.solve_times_ms[-_MAX_RETAINED_SAMPLES:]

    def record_failure(self) -> None:
        """Record a failed solve."""
        self.tasks_failed += 1

    def to_dict(self) -> Dict[str, Any]:
        """Serialize counters/derived metrics for the /stats endpoint."""
        return {
            'tasks_submitted': self.tasks_submitted,
            'tasks_solved': self.tasks_solved,
            'tasks_failed': self.tasks_failed,
            'success_rate': round(self.success_rate, 2),
            'avg_solve_time_ms': round(self.avg_solve_time_ms, 2),
            'total_solve_time_ms': round(self.total_solve_time_ms, 2),
        }


@dataclass
class SolverStats(_BaseRunStats):
    """Statistics for a specific solver."""


@dataclass
class CaptchaTypeStats(_BaseRunStats):
    """Statistics for a specific CAPTCHA type."""

    def record_submission(self) -> None:
        """Record a task submission."""
        self.tasks_submitted += 1


_PRIVATE_HELPERS = (_BaseRunStats,)


class StatsManager:
    """
    Thread-safe statistics manager.
    Singleton pattern for global access.
    """
    _instance: Optional['StatsManager'] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> 'StatsManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._start_time = time.time()
            self._data_lock = threading.RLock()

            self._captcha_stats: Dict[str, CaptchaTypeStats] = defaultdict(CaptchaTypeStats)
            self._solver_stats: Dict[str, SolverStats] = defaultdict(SolverStats)

            self._queue_depth = 0
            self._peak_queue_depth = 0
            self._total_queued = 0
            self._total_processed = 0

    @property
    def uptime_seconds(self) -> float:
        """Get service uptime in seconds."""
        return time.time() - self._start_time

    @property
    def queue_depth(self) -> int:
        """Get current queue depth."""
        with self._data_lock:
            return self._queue_depth

    def record_submission(self, captcha_type: str) -> None:
        """Record a task submission."""
        with self._data_lock:
            self._captcha_stats[captcha_type].record_submission()

    def record_solver_submission(self, solver: str) -> None:
        """Record a solver task submission."""
        with self._data_lock:
            self._solver_stats[solver].tasks_submitted += 1

    def record_success(self, captcha_type: str, solver: str, duration_ms: float) -> None:
        """Record a successful solve."""
        with self._data_lock:
            self._captcha_stats[captcha_type].record_success(duration_ms)
            self._solver_stats[solver].record_success(duration_ms)

    def record_failure(self, captcha_type: str, solver: str) -> None:
        """Record a failed solve."""
        with self._data_lock:
            self._captcha_stats[captcha_type].record_failure()
            self._solver_stats[solver].record_failure()

    def queue_push(self) -> None:
        """Record task entering queue."""
        with self._data_lock:
            self._queue_depth += 1
            self._total_queued += 1
            if self._queue_depth > self._peak_queue_depth:
                self._peak_queue_depth = self._queue_depth

    def queue_pop(self) -> None:
        """Record task leaving queue."""
        with self._data_lock:
            if self._queue_depth > 0:
                self._queue_depth -= 1
                self._total_processed += 1

    def get_captcha_stats(self, captcha_type: Optional[str] = None) -> Dict[str, Any]:
        """Get stats for a specific CAPTCHA type or all types."""
        with self._data_lock:
            if captcha_type:
                return self._captcha_stats[captcha_type].to_dict()
            return {k: v.to_dict() for k, v in self._captcha_stats.items()}

    def get_solver_stats(self, solver: Optional[str] = None) -> Dict[str, Any]:
        """Get stats for a specific solver or all solvers."""
        with self._data_lock:
            if solver:
                return self._solver_stats[solver].to_dict()
            return {k: v.to_dict() for k, v in self._solver_stats.items()}

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        with self._data_lock:
            total_submitted = sum(s.tasks_submitted for s in self._captcha_stats.values())
            total_solved = sum(s.tasks_solved for s in self._captcha_stats.values())
            total_failed = sum(s.tasks_failed for s in self._captcha_stats.values())

            total_attempted = total_solved + total_failed
            overall_success_rate = (total_solved / total_attempted * 100) if total_attempted > 0 else 0.0

            return {
                'total_submitted': total_submitted,
                'total_solved': total_solved,
                'total_failed': total_failed,
                'overall_success_rate': round(overall_success_rate, 2),
                'current_queue_depth': self._queue_depth,
                'peak_queue_depth': self._peak_queue_depth,
                'total_queued': self._total_queued,
                'total_processed': self._total_processed,
                'uptime_seconds': round(self.uptime_seconds, 2),
            }

    def get_full_stats(self) -> Dict[str, Any]:
        """Get complete statistics for /stats endpoint."""
        with self._data_lock:
            return {
                'summary': self.get_summary(),
                'captcha_types': self.get_captcha_stats(),
                'solvers': self.get_solver_stats(),
            }

    def reset(self) -> None:
        """Reset all statistics (use with caution)."""
        with self._data_lock:
            self._captcha_stats.clear()
            self._solver_stats.clear()
            self._queue_depth = 0
            self._peak_queue_depth = 0
            self._total_queued = 0
            self._total_processed = 0
            self._start_time = time.time()


def get_stats() -> StatsManager:
    """Get the global stats manager instance."""
    return StatsManager()


def format_uptime(seconds: float) -> str:
    """Format uptime seconds to human-readable string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
