"""ratelimit.py -- Per-IP request rate limiting middleware"""

import os
import time
import logging
import threading
from collections import deque
from typing import Dict, Deque, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("crawlrx.ratelimit")

# Exempt paths (exact matches)
EXEMPT_PATHS = {"/health", "/stats", "/docs", "/redoc", "/openapi.json", "/dashboard"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiting middleware using sliding window.
    
    Config via env vars:
    - CRAWLRX_RATE_LIMIT: max requests per window (default: 60)
    - CRAWLRX_RATE_WINDOW: window size in seconds (default: 60)
    
    Exempt paths: /health, /stats, /docs, /redoc, /openapi.json, /dashboard
    """
    
    def __init__(self, app: object) -> None:
        super().__init__(app)
        self.limit = int(os.getenv("CRAWLRX_RATE_LIMIT", "60"))
        self.window = int(os.getenv("CRAWLRX_RATE_WINDOW", "60"))
        
        # Data structure: ip_str -> deque of timestamps
        self._requests: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._request_count = 0  # For periodic cleanup
        
        logger.info("[RATELIMIT] Rate limiting enabled: %d req/%ds per IP", self.limit, self.window)
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract real client IP from X-Forwarded-For or request.client.host."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Take first IP in chain (closest to client)
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"
    
    def _is_exempt(self, path: str) -> bool:
        """Check if path is exempt from rate limiting."""
        return path in EXEMPT_PATHS
    
    def _cleanup_stale_ips(self) -> None:
        """Remove IPs with empty deques periodically."""
        with self._lock:
            stale_ips = [ip for ip, deq in self._requests.items() if not deq]
            for ip in stale_ips:
                del self._requests[ip]
            if stale_ips:
                logger.debug("Cleaned up %d stale IP entries", len(stale_ips))
    
    def _check_rate_limit(self, ip: str) -> tuple[bool, int]:
        """
        Check if request is within rate limit.
        Returns (allowed: bool, retry_after: int).
        """
        now = time.time()
        
        with self._lock:
            if ip not in self._requests:
                self._requests[ip] = deque(maxlen=self.limit * 2)
            
            deq = self._requests[ip]
            
            # Remove timestamps outside the window (sliding window)
            cutoff = now - self.window
            while deq and deq[0] < cutoff:
                deq.popleft()
            
            # Check limit
            if len(deq) >= self.limit:
                # Calculate retry_after based on oldest timestamp in window
                retry_after = int(deq[0] + self.window - now) + 1
                return False, max(1, retry_after)
            
            # Record this request
            deq.append(now)
            return True, 0
    
    async def dispatch(self, request: Request, call_next: object) -> object:
        """Process request with rate limiting."""
        path = request.url.path
        
        # Skip rate limiting for exempt paths
        if self._is_exempt(path):
            return await call_next(request)
        
        # Periodic cleanup every 100 requests
        self._request_count += 1
        if self._request_count >= 100:
            self._request_count = 0
            self._cleanup_stale_ips()
        
        ip = self._get_client_ip(request)
        allowed, retry_after = self._check_rate_limit(ip)
        
        if not allowed:
            logger.warning("Rate limit exceeded for IP %s on %s", ip, path)
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": "rate_limit_exceeded",
                    "message": "Too many requests. Limit: %d/%ds" % (self.limit, self.window),
                    "retry_after": retry_after,
                },
            )
        
        return await call_next(request)

