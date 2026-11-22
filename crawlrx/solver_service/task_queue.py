"""
In-memory async task queue for challenge resolution.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
import logging

from .models import Task, TaskState, TaskResult, CaptchaMethod

try:
    from .core.sentinel_errors import emit  # noqa: F401
except ImportError:
    def emit(*a: object, **kw: object) -> None:
        return None


logger = logging.getLogger(__name__)


# Concrete error classes the cleanup loop / processor can raise.
_CLEANUP_LOOP_ERRORS = (RuntimeError, OSError, KeyError, ValueError, AttributeError)
_PROCESSOR_ERRORS = (RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError)


class TaskQueue:
    """Async in-memory task queue with TTL and background processing."""

    def __init__(
        self,
        ttl_seconds: int = 300,
        cleanup_interval: int = 60,
        max_concurrent: int = 10,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.cleanup_interval = cleanup_interval
        self.max_concurrent = max_concurrent

        self._tasks: Dict[str, Task] = {}
        self._task_times: Dict[str, datetime] = {}

        self._processing: Dict[str, bool] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)

        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        self._background_tasks: set = set()

        self._stats: Dict[str, Any] = {
            'total_submitted': 0,
            'total_solved': 0,
            'total_failed': 0,
            'by_type': {}
        }

        self._processor: Optional[Callable[[Task], asyncio.Future[TaskResult]]] = None

    def set_processor(
        self,
        processor: Callable[[Task], asyncio.Future[TaskResult]],
    ) -> None:
        """Set the task processor function."""
        self._processor = processor

    async def start(self) -> None:
        """Start the background cleanup worker."""
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Task queue started with TTL=%ss, cleanup_interval=%ss",
            self.ttl_seconds, self.cleanup_interval,
        )

    async def stop(self) -> None:
        """Stop the background cleanup worker."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Task queue stopped")

    async def _run_cleanup_tick(self) -> bool:
        """One sweep: returns False if loop must exit (cancelled), True otherwise."""
        try:
            await asyncio.sleep(self.cleanup_interval)
            await self._cleanup_expired()
            return True
        except asyncio.CancelledError:
            # explicit recovery: cancel signals shutdown; caller exits the loop
            return False
        except _CLEANUP_LOOP_ERRORS as e:
            logger.exception("Error in cleanup loop: %s", e)
            # explicit recovery: cleanup non-fatal; loop continues
            return True

    async def _cleanup_loop(self) -> None:
        """Background loop to clean up expired tasks."""
        while self._running:
            if not await self._run_cleanup_tick():
                break

    async def _cleanup_expired(self) -> None:
        """Remove expired tasks from storage."""
        now = datetime.now(tz=timezone.utc)
        expired = []

        for task_id, task_time in list(self._task_times.items()):
            if now - task_time > timedelta(seconds=self.ttl_seconds):
                expired.append(task_id)

        for task_id in expired:
            self._remove_task(task_id)
            logger.debug("Cleaned up expired task: %s", task_id)

    def _remove_task(self, task_id: str) -> None:
        """Remove a task from all storage."""
        self._tasks.pop(task_id, None)
        self._task_times.pop(task_id, None)
        self._processing.pop(task_id, None)

    def submit(
        self,
        method: CaptchaMethod,
        params: Dict[str, Any],
    ) -> Task:
        """Submit a new challenge resolution task; returns the created Task."""
        task_id = str(uuid.uuid4()).replace("-", "")[:20]

        task = Task(
            id=task_id,
            state=TaskState.PENDING,
            method=method,
            params=params,
        )

        self._tasks[task_id] = task
        self._task_times[task_id] = datetime.now(tz=timezone.utc)
        self._stats['total_submitted'] += 1

        method_str = method.value
        if method_str not in self._stats['by_type']:
            self._stats['by_type'][method_str] = {
                'submitted': 0,
                'solved': 0,
                'failed': 0,
            }
        self._stats['by_type'][method_str]['submitted'] += 1

        _task = asyncio.create_task(self._process_task(task))
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)

        logger.info("Task submitted: %s (%s)", task_id, method.value)
        return task

    async def _process_task(self, task: Task) -> None:
        """Process a task with semaphore-controlled concurrency."""
        if not self._processor:
            logger.error("No processor set for task %s", task.id)
            self._fail_task(task, "ERROR_SOLVER_NOT_CONFIGURED", "Task processor not configured")
            return

        async with self._semaphore:
            task.started_at = datetime.now(tz=timezone.utc)
            task.state = TaskState.PROCESSING
            self._processing[task.id] = True

            try:
                result = await self._processor(task)
                self._complete_task(task, result)
            except asyncio.CancelledError as _swallow_exc:
                self._fail_task(task, "ERROR_CANCELLED", "Task cancelled")
                raise _swallow_exc
            except Exception as e:
                # This is the task boundary: unexpected processor failures must
                # become terminal results rather than leave the task processing.
                logger.exception("Error processing task %s: %s", task.id, e)
                self._fail_task(task, "ERROR_INTERNAL", str(e))
                return None
            finally:
                self._processing.pop(task.id, None)

    def _complete_task(self, task: Task, result: TaskResult) -> None:
        """Mark a task as completed with result."""
        task.completed_at = datetime.now(tz=timezone.utc)
        task.result = result

        if result.success:
            task.state = TaskState.SOLVED
            self._stats['total_solved'] += 1
            self._stats['by_type'][task.method.value]['solved'] += 1
            logger.info("Task solved: %s", task.id)
        else:
            task.state = TaskState.FAILED
            self._stats['total_failed'] += 1
            self._stats['by_type'][task.method.value]['failed'] += 1
            logger.warning("Task failed: %s - %s", task.id, result.error_code)

    def _fail_task(self, task: Task, error_code: str, error_message: str) -> None:
        """Mark a task as failed."""
        self._complete_task(task, TaskResult(
            success=False,
            error_code=error_code,
            error_message=error_message,
        ))

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task status for polling."""
        task = self._tasks.get(task_id)
        if not task:
            return None

        return {
            'id': task.id,
            'state': task.state.value,
            'method': task.method.value,
            'method_hint': task.params.get('method_hint'),
            'captcha_id': task.params.get('captcha_id'),
            'response': task.to_2captcha_response(),
        }

    def _avg_solve_time_for_method(self, method: str) -> Optional[float]:
        """Average solve time (ms) for solved tasks of `method`, or None."""
        solve_times = [
            t.result.solve_time_ms for t in self._tasks.values()
            if t.method.value == method
            and t.state == TaskState.SOLVED
            and t.result and t.result.solve_time_ms
        ]
        if not solve_times:
            return None
        return sum(solve_times) / len(solve_times)

    def _by_type_stats(self) -> Dict[str, Any]:
        """Per-method counters + success-rate + avg solve time."""
        by_type: Dict[str, Any] = {}
        for method, data in self._stats['by_type'].items():
            total = data['solved'] + data['failed']
            success_rate = (data['solved'] / total * 100) if total > 0 else 0
            avg_time = self._avg_solve_time_for_method(method)
            by_type[method] = {
                'submitted': data['submitted'],
                'solved': data['solved'],
                'failed': data['failed'],
                'success_rate': round(success_rate, 2),
                'avg_solve_time_ms': round(avg_time, 2) if avg_time else None,
            }
        return by_type

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        stats: Dict[str, Any] = {
            'total_submitted': self._stats['total_submitted'],
            'total_solved': self._stats['total_solved'],
            'total_failed': self._stats['total_failed'],
            'pending': sum(1 for t in self._tasks.values() if t.state == TaskState.PENDING),
            'processing': sum(1 for t in self._tasks.values() if t.state == TaskState.PROCESSING),
            'by_type': self._by_type_stats(),
        }

        total_done = stats['total_solved'] + stats['total_failed']
        stats['success_rate'] = (
            round(stats['total_solved'] / total_done * 100, 2) if total_done > 0 else 0
        )

        all_solve_times = [
            t.result.solve_time_ms for t in self._tasks.values()
            if t.state == TaskState.SOLVED and t.result and t.result.solve_time_ms
        ]
        stats['avg_solve_time_ms'] = (
            round(sum(all_solve_times) / len(all_solve_times), 2) if all_solve_times else None
        )
        return stats

    def list_active_tasks(self) -> List[Task]:
        """List all active (pending/processing) tasks."""
        return [
            t for t in self._tasks.values()
            if t.state in (TaskState.PENDING, TaskState.PROCESSING)
        ]
