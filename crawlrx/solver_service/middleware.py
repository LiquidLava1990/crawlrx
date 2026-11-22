"""
FastAPI middleware for FlareSolverr.
- API key validation
- Request logging
- CORS
- Rate limiting
"""

import time
import uuid
from dataclasses import dataclass
from typing import Optional, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_config

import logging as _logging

# Sentinel error tracking
try:
    from .core.sentinel_errors import ErrCode, emit
except ImportError:
    # Fallback no-op when running outside trident core.
    def emit(*a: object, **kw: object) -> None:
        return None


# Concrete exception classes the request-logging middleware logs + reraises.
_REQUEST_LOG_ERRORS = (RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError)


def get_logger(name: str) -> _logging.Logger:
    """Get a logger instance (replaces missing logger module)."""
    return _logging.getLogger(f"flaresolverr.{name}")


@dataclass
class RequestLogEntry:
    """Structured fields for a single HTTP access-log line."""
    method: str
    path: str
    status_code: int
    duration_ms: float
    client_ip: str
    extra: Optional[dict] = None


def log_request(entry: RequestLogEntry) -> None:
    """Log an HTTP request from a structured entry."""
    _logger = get_logger("access")
    _logger.info(
        "%s %s %s %.1fms client=%s %s",
        entry.method,
        entry.path,
        entry.status_code,
        entry.duration_ms,
        entry.client_ip,
        entry.extra or "",
    )


def set_request_context(request_id: str) -> None:
    """Set request context (no-op -- original logger module missing)."""
    return None


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware to validate API key on protected endpoints."""

    def __init__(self, app: FastAPI, exclude_paths: Optional[list] = None) -> None:
        super().__init__(app)
        self.exclude_paths = exclude_paths or [
            "/health",
            "/stats",
            "/docs",
            "/openapi.json",
            "/redoc",
        ]
        self.config = get_config()
        self.logger = get_logger("middleware")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            return await call_next(request)

        api_key = request.headers.get("Authorization") or request.query_params.get("key")

        if api_key and api_key.lower().startswith("bearer "):
            api_key = api_key[7:]

        if not api_key:
            self.logger.warning("Missing API key | %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=403,
                content={
                    "error": "AUTH_ERROR",
                    "errorCode": "ERROR_WRONG_USER_KEY",
                    "errorDescription": "Missing API key",
                },
            )

        if api_key != self.config.api_key:
            self.logger.warning("Invalid API key | %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=403,
                content={
                    "error": "AUTH_ERROR",
                    "errorCode": "ERROR_WRONG_USER_KEY",
                    "errorDescription": "Invalid API key",
                },
            )

        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all HTTP requests with timing."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        self.logger = get_logger("middleware")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        set_request_context(request_id)

        start_time = time.perf_counter()
        client_ip = request.headers.get(
            "X-Forwarded-For",
            request.client.host if request.client else "-",
        ).split(",")[0].strip()

        try:
            response = await call_next(request)
        except _REQUEST_LOG_ERRORS as e:
            emit(ErrCode.CAPTCHA_DETECTED, f"Request error: {request.url.path}: {e}")
            duration_ms = (time.perf_counter() - start_time) * 1000
            log_request(RequestLogEntry(
                method=request.method,
                path=request.url.path,
                status_code=500,
                duration_ms=duration_ms,
                client_ip=client_ip,
                extra={"error": str(e), "request_id": request_id},
            ))
            raise

        duration_ms = (time.perf_counter() - start_time) * 1000

        log_request(RequestLogEntry(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client_ip=client_ip,
            extra={"request_id": request_id},
        ))

        response.headers["X-Request-ID"] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting middleware."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        self.config = get_config()
        self.logger = get_logger("ratelimit")

        self._requests: dict = {}
        self._cleanup_counter = 0

    def _is_rate_limited(self, client_ip: str) -> bool:
        """Check if client is rate limited."""
        if not self.config.rate_limit.enabled:
            return False

        now = time.time()
        window_start = now - 60

        client_requests = self._requests.get(client_ip, [])
        client_requests = [t for t in client_requests if t > window_start]

        if len(client_requests) >= self.config.rate_limit.requests_per_minute:
            return True

        client_requests.append(now)
        self._requests[client_ip] = client_requests

        self._cleanup_counter += 1
        if self._cleanup_counter >= 100:
            self._cleanup_old_entries(window_start)
            self._cleanup_counter = 0

        return False

    def _cleanup_old_entries(self, cutoff: float) -> None:
        """Remove old entries from rate limit store."""
        for ip in list(self._requests.keys()):
            self._requests[ip] = [t for t in self._requests[ip] if t > cutoff]
            if not self._requests[ip]:
                del self._requests[ip]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.config.rate_limit.enabled:
            return await call_next(request)

        if request.url.path in ["/health", "/stats"]:
            return await call_next(request)

        client_ip = request.headers.get(
            "X-Forwarded-For",
            request.client.host if request.client else "unknown",
        ).split(",")[0].strip()

        if self._is_rate_limited(client_ip):
            self.logger.warning("Rate limit exceeded | %s | %s", client_ip, request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMIT",
                    "errorCode": "ERROR_RATE_LIMIT",
                    "errorDescription": "Rate limit exceeded. Please slow down.",
                    "retry_after": 60,
                },
            )

        return await call_next(request)


def setup_middleware(app: FastAPI) -> None:
    """Configure all middleware for the FastAPI app."""
    config = get_config()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    get_logger("middleware").info("Middleware configured | RateLimit: %s", config.rate_limit.enabled)


def auth_error_response(message: str = "Invalid API key") -> JSONResponse:
    """Return 2Captcha-compatible auth error."""
    return JSONResponse(
        status_code=403,
        content={
            "status": 0,
            "error": "AUTH_ERROR",
            "errorCode": "ERROR_WRONG_USER_KEY",
            "errorDescription": message,
        },
    )


def rate_limit_response(retry_after: int = 60) -> JSONResponse:
    """Return 2Captcha-compatible rate limit error."""
    return JSONResponse(
        status_code=429,
        content={
            "status": 0,
            "error": "RATE_LIMIT",
            "errorCode": "ERROR_RATE_LIMIT",
            "errorDescription": "Rate limit exceeded",
            "retry_after": retry_after,
        },
    )
