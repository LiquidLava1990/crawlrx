"""
CrawlRX Authentication Middleware
API Key authentication for CrawlRX API
"""

import os
import logging
from typing import List, Optional

from fastapi import Request, HTTPException
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("crawlrx")

# API Key header configuration
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_api_keys() -> List[str]:
    """
    Load API keys from CRAWLRX_API_KEYS env var (comma-separated).
    Returns empty list if not set or empty (local mode - auth disabled).
    """
    keys_env = os.getenv("CRAWLRX_API_KEYS", "").strip()
    if not keys_env:
        return []
    # Split by comma and strip whitespace, filter empty strings
    keys = [k.strip() for k in keys_env.split(",") if k.strip()]
    return keys


# Cache the keys at module load (env vars don't change during runtime without restart)
_API_KEYS = get_api_keys()

# Public paths that skip authentication
# Only liveness and static pages are public. Status and stats expose request
# history, domains and configuration, so they need the key like the API does.
PUBLIC_PATHS = {
    "/",
    "/health",
    "/dashboard",
    "/docs",
    "/redoc",
    "/openapi.json",
}


def is_public_path(path: str) -> bool:
    """Check if a path is public (no auth required)."""
    # Exact match
    if path in PUBLIC_PATHS:
        return True
    # Handle trailing slashes
    if path.rstrip("/") in PUBLIC_PATHS:
        return True
    return False


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce API Key authentication.
    
    - If CRAWLRX_API_KEYS is not set or empty: auth is DISABLED (local mode)
    - If keys are configured: requires X-API-Key header matching one of the keys
    - Public paths (/health, /docs, etc.) skip auth check
    """
    
    async def dispatch(self, request: Request, call_next: object) -> object:
        # Check if auth is configured (keys loaded from env)
        if not _API_KEYS:
            # Local mode - auth disabled, pass through
            return await call_next(request)
        
        # Check if this is a public path
        if is_public_path(request.url.path):
            return await call_next(request)
        
        # Get API key from header
        api_key = request.headers.get("X-API-Key")
        
        if not api_key:
            logger.warning("Missing API key for %s %s", request.method, request.url.path)
            return self._error_response(401, "Invalid or missing API key")
        
        if api_key not in _API_KEYS:
            logger.warning("Invalid API key for %s %s", request.method, request.url.path)
            return self._error_response(401, "Invalid or missing API key")
        
        # Valid API key - continue
        return await call_next(request)
    
    def _error_response(self, status_code: int, detail: str):
        """Return JSON error response."""
        from starlette.responses import JSONResponse
        return JSONResponse(
            status_code=status_code,
            content={"error": detail}
        )


def require_api_key(request: Request) -> Optional[str]:
    """
    Dependency function for route-level API key enforcement.
    Can be used on individual routes if not using middleware.
    """
    if not _API_KEYS:
        return None  # Local mode - no key required
    
    api_key = request.headers.get("X-API-Key")
    
    if not api_key or api_key not in _API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    
    return api_key


