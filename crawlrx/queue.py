"""queue.py -- Job queue + rate limiter for CrawlRX.

Provides asynchronous job processing with:
- In-memory job queue with background worker
- Per-domain rate limiting (token bucket)
- Job status tracking and retry logic
- REST API for job submission and monitoring
"""

import asyncio
import logging
import threading
import time
import uuid
from enum import Enum
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ._queue_helpers import (
    DomainRateLimiter,
    JobAttemptOutcome,
    call_local_api,
    execute_job_attempt,
    schedule_on_running_loop,
)
from .config import DEFAULT_RATE_LIMIT_RPM, RATE_LIMIT_JITTER

logger = logging.getLogger("crawlrx.queue")
router = APIRouter(prefix="/queue", tags=["queue"])


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRYING = "retrying"


class Job(BaseModel):
    job_id: str
    status: JobStatus
    endpoint: str  # "/crawl", "/scrape", "/batch"
    url: str  # or first URL for batch
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[dict] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    payload: Optional[dict] = None  # Store original payload for retries

    model_config = {"from_attributes": True}


class QueueStats(BaseModel):
    pending: int
    running: int
    done: int
    failed: int
    total: int
    max_concurrent: int


class SubmitRequest(BaseModel):
    endpoint: str = Field(..., description="Endpoint to call: /crawl, /scrape, /batch")
    url: str = Field(..., description="URL to process")
    payload: Optional[dict] = Field(default=None, description="Additional parameters for the endpoint")


class SubmitResponse(BaseModel):
    job_id: str


class JobListResponse(BaseModel):
    jobs: List[Job]
    count: int


# ─────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────

_rate_limiter: Optional[DomainRateLimiter] = None
_rate_limiter_lock: threading.Lock = threading.Lock()


def get_rate_limiter() -> DomainRateLimiter:
    """Get the singleton DomainRateLimiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                _rate_limiter = DomainRateLimiter(
                    DEFAULT_RATE_LIMIT_RPM, RATE_LIMIT_JITTER,
                )
    return _rate_limiter


# ─────────────────────────────────────────────
# Job queue
# ─────────────────────────────────────────────

class JobQueue:
    """Thread-safe in-memory job queue with background worker."""

    MAX_CONCURRENT: int = 5
    MAX_HISTORY: int = 1000

    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        self._pending_queue: Optional[asyncio.Queue] = None
        self.jobs_lock: threading.Lock = threading.Lock()
        self.semaphore: Optional[asyncio.Semaphore] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.running: bool = False
        self.rate_limiter: DomainRateLimiter = get_rate_limiter()
        self._background_tasks: set = set()

    @property
    def pending_queue(self) -> asyncio.Queue:
        """Lazy-init asyncio.Queue on first access (requires running event loop)."""
        if self._pending_queue is None:
            self._pending_queue = asyncio.Queue()
        return self._pending_queue

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start the background worker task."""
        if self.running:
            return
        self.running = True
        if loop is None:
            loop = asyncio.get_running_loop()
        self.semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        self.worker_task = loop.create_task(self._worker())
        logger.info("[QUEUE] Worker started (max_concurrent=%d)", self.MAX_CONCURRENT)

    def stop(self) -> None:
        """Stop the background worker."""
        self.running = False
        if self.worker_task:
            self.worker_task.cancel()
            logger.info("[QUEUE] Worker stopped")

    def _enqueue_pending(self, job_id: str) -> None:
        """Push ``job_id`` onto the pending queue, preferring the running loop."""
        if schedule_on_running_loop(self.pending_queue.put(job_id)):
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            logger.warning(
                "[QUEUE] No event loop available, job %s queued but not dispatched yet",
                job_id,
            )
            return None  # explicit recovery: non-fatal: RuntimeError is recorded but does not abort the caller
        if loop.is_running():
            task = asyncio.ensure_future(self.pending_queue.put(job_id), loop=loop)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        else:
            loop.run_until_complete(self.pending_queue.put(job_id))

    def submit(self, endpoint: str, url: str, payload: Optional[dict] = None) -> str:
        """Submit a new job to the queue; return its ``job_id``."""
        job_id = str(uuid.uuid4())[:16]
        job = Job(
            job_id=job_id,
            status=JobStatus.PENDING,
            endpoint=endpoint,
            url=url,
            created_at=time.time(),
            payload=payload or {},
        )
        with self.jobs_lock:
            self.jobs[job_id] = job
            self._evict_if_needed()
        self._enqueue_pending(job_id)
        logger.info("[QUEUE] Submitted job %s: %s %s", job_id, endpoint, url)
        return job_id

    def _evict_if_needed(self) -> None:
        """Evict oldest non-active jobs if we exceed MAX_HISTORY."""
        if len(self.jobs) <= self.MAX_HISTORY:
            return
        sorted_jobs = sorted(self.jobs.values(), key=lambda j: j.created_at)
        to_remove = len(self.jobs) - self.MAX_HISTORY
        removed = 0
        for job in sorted_jobs:
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                continue
            del self.jobs[job.job_id]
            removed += 1
            if removed >= to_remove:
                break

    def get(self, job_id: str) -> Optional[Job]:
        """Get a job snapshot by ID."""
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            return job.model_copy() if job else None

    def list(self, status: Optional[JobStatus] = None, limit: int = 50) -> List[Job]:
        """List jobs, newest first, optionally filtered by status."""
        with self.jobs_lock:
            jobs_list = list(self.jobs.values())
        if status:
            jobs_list = [j for j in jobs_list if j.status == status]
        jobs_list.sort(key=lambda j: j.created_at, reverse=True)
        return [j.model_copy() for j in jobs_list[:limit]]

    def stats(self) -> QueueStats:
        """Get queue statistics."""
        counts = {s: 0 for s in JobStatus}
        with self.jobs_lock:
            for job in self.jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
            total = len(self.jobs)
        return QueueStats(
            pending=counts[JobStatus.PENDING],
            running=counts[JobStatus.RUNNING],
            done=counts[JobStatus.DONE],
            failed=counts[JobStatus.FAILED],
            total=total,
            max_concurrent=self.MAX_CONCURRENT,
        )

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending job; True if it was pending."""
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.PENDING:
                return False
            job.status = JobStatus.FAILED
            job.completed_at = time.time()
            job.error = "Cancelled by user"
            return True

    async def _worker(self) -> None:
        """Background worker that drains the pending queue."""
        logger.info("[QUEUE] Worker loop started")
        while self.running:
            try:
                job_id = await asyncio.wait_for(self.pending_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            task = asyncio.create_task(self._process_job(job_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    def _claim_pending_job(self, job_id: str) -> Optional[Job]:
        """Mark a pending job as RUNNING and return its in-place ref, or None."""
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.PENDING:
                return None
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            job.attempts += 1
            return self.jobs[job_id].model_copy()

    def _record_outcome(
        self,
        job_id: str,
        outcome: JobAttemptOutcome,
    ) -> None:
        """Apply outcome to the canonical job, scheduling retries on failure."""
        with self.jobs_lock:
            job_ref = self.jobs[job_id]
            job_ref.elapsed_ms = outcome.elapsed_ms
            if outcome.success:
                job_ref.status = JobStatus.DONE
                job_ref.result = outcome.result
                job_ref.completed_at = time.time()
                logger.info(
                    "[QUEUE] Job %s completed in %dms", job_id, outcome.elapsed_ms,
                )
                return
            if job_ref.attempts < job_ref.max_attempts:
                job_ref.status = JobStatus.RETRYING
                logger.warning(
                    "[QUEUE] Job %s failed, scheduling retry (%d/%d)",
                    job_id, job_ref.attempts, job_ref.max_attempts,
                )
                task = asyncio.create_task(self._retry_job(job_id))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return
            job_ref.status = JobStatus.FAILED
            job_ref.error = outcome.error_msg or "Max retries exceeded"
            job_ref.completed_at = time.time()
            logger.error(
                "[QUEUE] Job %s failed permanently after %d attempts",
                job_id, job_ref.attempts,
            )

    async def _process_job(self, job_id: str) -> None:
        """Process a single job with retry logic."""
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        async with self.semaphore:
            job = self._claim_pending_job(job_id)
            if job is None:
                return
            logger.info("[QUEUE] Processing job %s (attempt %d)", job_id, job.attempts)
            outcome = await execute_job_attempt(
                dispatch=lambda: self._dispatch_job(job),
                rate_limiter=self.rate_limiter,
                job_url=job.url,
                job_id=job_id,
            )
            self._record_outcome(job_id, outcome)

    async def _retry_job(self, job_id: str, delay: float = 2.0) -> None:
        """Retry a job after a delay."""
        await asyncio.sleep(delay)
        await self.pending_queue.put(job_id)
        with self.jobs_lock:
            if job_id in self.jobs:
                self.jobs[job_id].status = JobStatus.PENDING

    async def _dispatch_job(self, job: Job) -> dict:
        """Dispatch job to the appropriate endpoint handler."""
        endpoint = job.endpoint
        payload = job.payload or {}
        url = job.url
        if endpoint in ("/crawl", "crawl"):
            return await self._do_crawl_job(url, payload)
        if endpoint in ("/scrape", "scrape"):
            return await self._do_scrape_job(url, payload)
        if endpoint in ("/batch", "batch"):
            return await self._do_batch_job(url, payload)
        raise ValueError(f"Unknown endpoint: {endpoint}")

    async def _do_crawl_job(self, url: str, payload: dict) -> dict:
        """Execute a crawl job via local /crawl API."""
        body = {"url": url, **payload}
        body.setdefault("extract_text", True)
        body.setdefault("extract_markdown", True)
        return await call_local_api("/crawl", body)

    async def _do_scrape_job(self, url: str, payload: dict) -> dict:
        """Execute a scrape job via local /scrape API."""
        body = {"url": url, **payload}
        return await call_local_api("/scrape", body)

    async def _do_batch_job(self, url: str, payload: dict) -> dict:
        """Execute a batch job (single URL) via /crawl or /scrape."""
        mode = payload.get("mode", "crawl")
        path = "/scrape" if mode == "scrape" else "/crawl"
        body = {"url": url, **{k: v for k, v in payload.items() if k != "mode"}}
        return await call_local_api(path, body)


# ─────────────────────────────────────────────
# Singleton queue accessor
# ─────────────────────────────────────────────

_queue: Optional[JobQueue] = None
_queue_lock: threading.Lock = threading.Lock()


def _try_start_queue(queue: JobQueue) -> None:
    """Start ``queue`` if a running event loop is available."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    queue.start(loop)


def get_job_queue() -> JobQueue:
    """Get the singleton JobQueue instance."""
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = JobQueue()
                _try_start_queue(_queue)
    return _queue


# ─────────────────────────────────────────────
# Router endpoints
# ─────────────────────────────────────────────

@router.post("/submit", response_model=SubmitResponse, summary="Submit a new job")
async def queue_submit(request: SubmitRequest) -> SubmitResponse:
    """Submit a new job to the queue."""
    queue = get_job_queue()
    endpoint = request.endpoint
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    job_id = queue.submit(endpoint, request.url, request.payload)
    return SubmitResponse(job_id=job_id)


@router.get("/{job_id}", response_model=Job, summary="Get job status")
async def queue_get(job_id: str) -> Job:
    """Get the status and result of a job."""
    queue = get_job_queue()
    job = queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@router.get("", response_model=JobListResponse, summary="List jobs")
async def queue_list(
    status: Optional[JobStatus] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000, description="Max jobs to return"),
) -> JobListResponse:
    """List recent jobs, optionally filtered by status."""
    queue = get_job_queue()
    jobs = queue.list(status=status, limit=limit)
    return JobListResponse(jobs=jobs, count=len(jobs))


@router.get("/stats", response_model=QueueStats, summary="Queue statistics")
async def queue_stats() -> QueueStats:
    """Get queue statistics: pending, running, done, failed counts."""
    queue = get_job_queue()
    return queue.stats()


@router.delete("/{job_id}", summary="Cancel a pending job")
async def queue_cancel(job_id: str) -> dict:
    """Cancel a pending job."""
    queue = get_job_queue()
    cancelled = queue.cancel(job_id)
    if cancelled:
        return {"success": True, "message": f"Job {job_id} cancelled"}
    job = queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    raise HTTPException(
        status_code=400,
        detail=f"Job {job_id} cannot be cancelled (status: {job.status})",
    )


# ─────────────────────────────────────────────
# Lifecycle hooks for startup/shutdown
# ─────────────────────────────────────────────

def init_queue() -> None:
    """Initialize the queue on app startup."""
    queue = get_job_queue()
    _try_start_queue(queue)


def shutdown_queue() -> None:
    """Shutdown the queue on app shutdown."""
    global _queue
    if _queue:
        _queue.stop()
        _queue = None
