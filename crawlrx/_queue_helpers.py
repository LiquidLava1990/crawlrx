"""Internal helpers for :mod:`queue` (module split).

Holds the rate-limiter primitives and job-processing helpers so the parent
module stays under the file-length / nesting / cognitive ceilings.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("crawlrx.queue")

# Narrow exception families used by the queue engine.
URL_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError,
)
LOCAL_API_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError, httpx.RequestError, httpx.TimeoutException,
    OSError, ValueError, KeyError, TypeError,
)
JOB_DISPATCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, ValueError, KeyError, TypeError, RuntimeError,
    asyncio.TimeoutError,
    httpx.HTTPError, httpx.RequestError, httpx.TimeoutException,
)


# ---------------------------------------------------------------------------
# Domain rate limiter (token bucket)
# ---------------------------------------------------------------------------

class DomainBucket:
    """Token bucket for a single domain."""

    def __init__(self, rate_per_minute: float) -> None:
        self.tokens: float = rate_per_minute  # Start with full bucket
        self.rate_per_minute: float = rate_per_minute
        self.last_refill: float = time.time()
        self.lock: threading.Lock = threading.Lock()

    def _refill(self) -> None:
        """Add tokens based on time elapsed since last refill."""
        now = time.time()
        elapsed_minutes = (now - self.last_refill) / 60.0
        tokens_to_add = elapsed_minutes * self.rate_per_minute
        self.tokens = min(self.rate_per_minute, self.tokens + tokens_to_add)
        self.last_refill = now


class DomainRateLimiter:
    """Token bucket rate limiter keyed by domain."""

    def __init__(self, default_rpm: float, jitter: float) -> None:
        self.default_rpm: float = float(default_rpm)
        self.jitter: float = jitter
        self.buckets: Dict[str, DomainBucket] = {}
        self.buckets_lock: threading.Lock = threading.Lock()

    def _get_domain(self, url: str) -> str:
        """Extract a normalised domain from ``url``."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except URL_PARSE_EXCEPTIONS:
            return url

    def _apply_jitter(self, wait_time: float) -> float:
        """Apply +/- jitter randomisation to a wait time."""
        if self.jitter <= 0:
            return wait_time
        jitter_factor = 1.0 + random.uniform(-self.jitter, self.jitter)
        return wait_time * jitter_factor

    def _get_or_create_bucket(self, domain: str) -> DomainBucket:
        with self.buckets_lock:
            bucket = self.buckets.get(domain)
            if bucket is None:
                bucket = DomainBucket(self.default_rpm)
                self.buckets[domain] = bucket
            return bucket

    def acquire(self, url: str) -> float:
        """Acquire a token for ``url``; returns seconds-to-wait (0 = ok)."""
        domain = self._get_domain(url)
        bucket = self._get_or_create_bucket(domain)
        with bucket.lock:
            bucket._refill()
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            tokens_needed = 1.0 - bucket.tokens
            seconds_per_token = 60.0 / bucket.rate_per_minute
            return self._apply_jitter(tokens_needed * seconds_per_token)

    def get_bucket_stats(self, url: str) -> Dict[str, Any]:
        """Snapshot a bucket's state for debugging."""
        domain = self._get_domain(url)
        with self.buckets_lock:
            bucket = self.buckets.get(domain)
            if bucket is None:
                return {
                    "domain": domain,
                    "tokens": self.default_rpm,
                    "rate_per_minute": self.default_rpm,
                }
        with bucket.lock:
            bucket._refill()
            return {
                "domain": domain,
                "tokens": round(bucket.tokens, 2),
                "rate_per_minute": bucket.rate_per_minute,
            }


# ---------------------------------------------------------------------------
# Async-loop helpers
# ---------------------------------------------------------------------------

def schedule_on_running_loop(coro: "Coroutine[Any, Any, Any]") -> bool:
    """Schedule ``coro`` on the running event loop. Return True on success."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(coro)
    return True


# ---------------------------------------------------------------------------
# Local-API helpers
# ---------------------------------------------------------------------------

# Hardcoded host kept here because the CrawlRX queue dispatcher is intra-process.
# See https://github.com/CrawlRX/CrawlRX/issues/207 for the service-registry plan.
_LOCAL_API_HOST = "http://localhost:7337"


async def call_local_api(path: str, body: Dict[str, Any], timeout: int = 90) -> Dict[str, Any]:
    """POST to a local CrawlRX endpoint via ``httpx`` and decode JSON."""
    url = f"{_LOCAL_API_HOST}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()
    except LOCAL_API_EXCEPTIONS as exc:
        logger.error("[QUEUE] Local API call failed for %s: %s", path, exc)
        raise exc


# ---------------------------------------------------------------------------
# Job-processing primitives
# ---------------------------------------------------------------------------

@dataclass
class JobAttemptOutcome:
    """Result of a single job execution attempt."""

    success: bool
    elapsed_ms: int
    result: Optional[Dict[str, Any]]
    error_msg: Optional[str]


async def execute_job_attempt(
    dispatch: "Callable[[], Awaitable[dict[str, Any] | None]]",
    rate_limiter: DomainRateLimiter,
    job_url: str,
    job_id: str,
) -> JobAttemptOutcome:
    """Apply rate limiting and run ``dispatch``; capture timing and errors."""
    wait_time = rate_limiter.acquire(job_url)
    if wait_time > 0:
        logger.debug(
            "[QUEUE] Rate limiting job %s: waiting %.2fs", job_id, wait_time,
        )
        await asyncio.sleep(wait_time)
    start_time = time.time()
    try:
        result = await dispatch()
        success = bool(result.get("success", False)) if result else False
        error_msg = (
            result.get("error", "Unknown error")
            if (not success and result)
            else None
        )
    except JOB_DISPATCH_EXCEPTIONS as exc:
        logger.exception("[QUEUE] Job %s failed: %s", job_id, exc)
        result = None
        success = False
        error_msg = str(exc)
        return None  # explicit recovery: non-fatal: JOB_DISPATCH_EXCEPTIONS is recorded but does not abort the caller
    elapsed_ms = int((time.time() - start_time) * 1000)
    return JobAttemptOutcome(
        success=success,
        elapsed_ms=elapsed_ms,
        result=result,
        error_msg=error_msg,
    )


__all__ = [
    "DomainBucket",
    "DomainRateLimiter",
    "JOB_DISPATCH_EXCEPTIONS",
    "JobAttemptOutcome",
    "LOCAL_API_EXCEPTIONS",
    "URL_PARSE_EXCEPTIONS",
    "call_local_api",
    "execute_job_attempt",
    "schedule_on_running_loop",
]
