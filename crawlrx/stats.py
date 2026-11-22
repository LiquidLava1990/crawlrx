"""
CrawlRX Stats Module
In-memory request statistics tracking and /stats endpoint.
"""

import time
import threading
from typing import Dict, Optional
from datetime import datetime

from pydantic import BaseModel, Field
from fastapi import APIRouter

# Create router for stats endpoint
router = APIRouter(tags=["system"])

# Thread-safe stats storage
_stats_lock = threading.Lock()

# Module-level stats storage
_stats = {
    "start_time": time.time(),
    "total_requests": 0,
    "requests_by_endpoint": {},  # {"/crawl": 42, "/search": 18, ...}
    "success_count": 0,
    "error_count": 0,
    "response_times_ms": {},  # {"/crawl": [120, 150, ...], ...}
    "errors_today": 0,
    "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
}


class StatsResponse(BaseModel):
    """Response model for GET /stats"""
    uptime_seconds: float = Field(..., description="Service uptime in seconds")
    total_requests: int = Field(..., description="Total requests processed")
    requests_by_endpoint: Dict[str, int] = Field(
        default_factory=dict,
        description="Request count per endpoint (e.g., {'/crawl': 42})"
    )
    success_rate: float = Field(
        ...,
        description="Success rate from 0.0 to 1.0",
        ge=0.0,
        le=1.0
    )
    avg_response_ms: Dict[str, float] = Field(
        default_factory=dict,
        description="Average response time per endpoint in ms"
    )
    errors_today: int = Field(..., description="Number of errors today")
    cache_stats: Optional[dict] = Field(
        default=None,
        description="Cache statistics if cache module available"
    )


def _check_day_rollover():
    """Check if we need to reset daily counters."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _stats_lock:
        if _stats["last_reset_date"] != today:
            _stats["errors_today"] = 0
            _stats["last_reset_date"] = today


def record_request(endpoint: str, elapsed_ms: int, success: bool) -> None:
    """
    Record a request in the stats.
    Thread-safe operation.
    
    Args:
        endpoint: API endpoint path
        elapsed_ms: Request duration in milliseconds
        success: Whether the request succeeded
    """
    _check_day_rollover()
    
    with _stats_lock:
        _stats["total_requests"] += 1
        
        # Count by endpoint
        endpoint_counts: Dict[str, int] = _stats["requests_by_endpoint"]
        endpoint_counts.setdefault(endpoint, 0)
        endpoint_counts[endpoint] += 1

        # Success/error tracking
        if success:
            _stats["success_count"] += 1
        else:
            _stats["error_count"] += 1
            _stats["errors_today"] += 1

        # Response time tracking
        response_times: Dict[str, list] = _stats["response_times_ms"]
        endpoint_times = response_times.setdefault(endpoint, [])
        endpoint_times.append(elapsed_ms)

        # Keep only last 1000 response times per endpoint to prevent memory bloat
        if len(endpoint_times) > 1000:
            response_times[endpoint] = endpoint_times[-1000:]


def get_stats() -> StatsResponse:
    """Get current statistics."""
    _check_day_rollover()
    
    with _stats_lock:
        uptime = time.time() - _stats["start_time"]
        total = _stats["total_requests"]
        
        # Calculate success rate
        if total > 0:
            success_rate = _stats["success_count"] / total
        else:
            success_rate = 1.0  # No errors if no requests
        
        # Calculate average response times per endpoint
        avg_response_ms = {}
        for endpoint, times in _stats["response_times_ms"].items():
            if times:
                avg_response_ms[endpoint] = round(sum(times) / len(times), 2)
            else:
                avg_response_ms[endpoint] = 0.0
        
        # Try to get cache stats if available
        cache_stats = None
        try:
            from . import config
            # Cache module can add stats here if implemented
            cache_stats = None  # Placeholder for future cache stats
        except ImportError:
            pass
        
        return StatsResponse(
            uptime_seconds=round(uptime, 2),
            total_requests=total,
            requests_by_endpoint=_stats["requests_by_endpoint"].copy(),
            success_rate=round(success_rate, 4),
            avg_response_ms=avg_response_ms,
            errors_today=_stats["errors_today"],
            cache_stats=cache_stats
        )


@router.get("/circuit-breakers", summary="Circuit breaker states per domain")
async def circuit_breakers_endpoint() -> object:
    """
    Returns the current state of all domain circuit breakers.

    State values:
    - **closed** -- domain is healthy, requests pass through normally
    - **open** -- domain is failing; requests are rejected immediately (no Playwright call)
    - **half_open** -- in cooldown recovery; one test request allowed through

    A breaker trips OPEN after 5 failures within a 60-second window.
    After 30s cooldown it moves to HALF_OPEN for a probe request.
    Success closes it; another failure re-opens it.
    """
    from .utils import get_all_circuit_breakers
    breakers = get_all_circuit_breakers()
    return {
        "total": len(breakers),
        "circuit_breakers": [cb.as_dict() for cb in breakers.values()],
    }


@router.get("/stats", response_model=StatsResponse, summary="Service statistics")
async def stats_endpoint() -> StatsResponse:
    """
    Returns service statistics including:
    - Uptime
    - Total requests processed
    - Requests per endpoint
    - Success rate
    - Average response times
    - Error count (daily)
    
    No authentication required - public endpoint like /health.
    """
    return get_stats()
