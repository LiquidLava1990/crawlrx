"""
CrawlRX Response Cache
========================
Two-layer cache: in-memory (fast) + SQLite persistent (survives restarts).
Thread-safe using threading.Lock.
"""

import hashlib
import json
import time
import threading
import logging
from typing import Optional, Any, Dict, OrderedDict as OrderedDictType
from collections import OrderedDict

from .persistent_cache import get_persistent_cache
from .config import CACHE_TTL

logger = logging.getLogger("crawlrx.cache")

# Narrow exception family for sqlite-backed persistent cache I/O. Anything
# beyond OS / sqlite errors is a real bug and must propagate.
_PERSISTENT_CACHE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)
try:  # sqlite3 ships with stdlib but allow for stripped builds
    import sqlite3 as _sqlite3
    _PERSISTENT_CACHE_EXCEPTIONS = _PERSISTENT_CACHE_EXCEPTIONS + (_sqlite3.Error,)
except ImportError:  # pragma: no cover -- sqlite always present in CPython
    pass


class ResponseCache:
    """
    Thread-safe in-memory cache with TTL and LRU eviction.

    - TTL default: 5 minutes
    - Max 500 entries (LRU eviction when full)
    - Thread-safe using threading.Lock
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL, max_size: int = 500) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: Dict[str, Dict[str, Any]] = {}  # key -> {"value": ..., "expires_at": ...}
        self._access_order: OrderedDictType[str, None] = OrderedDict()  # LRU tracking
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _try_memory_hit(self, key: str) -> Optional[Any]:
        """Layer-1 lookup; caller MUST hold ``self._lock``."""
        if key not in self._cache:
            return None
        entry = self._cache[key]
        if time.time() > entry["expires_at"]:
            self._cache.pop(key, None)
            self._access_order.pop(key, None)
            return None
        if key in self._access_order:
            self._access_order.move_to_end(key)
        else:
            self._access_order[key] = None
        self._hits += 1
        logger.debug("Cache hit (memory) for key: %s", key[:16])
        return entry["value"]

    def _try_persistent_hit(self, key: str) -> Optional[Any]:
        """Layer-2 lookup; caller MUST hold ``self._lock``."""
        try:
            persistent = get_persistent_cache()
            persisted_value = persistent.get(key)
        except _PERSISTENT_CACHE_EXCEPTIONS as exc:
            logger.debug(
                "Persistent cache lookup failed (%s): %s",
                type(exc).__name__,
                exc,
            )
            return None
        if persisted_value is None:
            return None
        self._cache[key] = {
            "value": persisted_value,
            "expires_at": time.time() + self.ttl_seconds,
        }
        self._access_order[key] = None
        self._access_order.move_to_end(key)
        self._hits += 1
        logger.debug("Cache hit (persistent) for key: %s", key[:16])
        return persisted_value

    def get(self, key: str) -> Optional[Any]:
        """Two-layer lookup (in-memory then persistent); ``None`` if absent."""
        with self._lock:
            memory_hit = self._try_memory_hit(key)
            if memory_hit is not None:
                return memory_hit
            persistent_hit = self._try_persistent_hit(key)
            if persistent_hit is not None:
                return persistent_hit
            self._misses += 1
            return None

    def _persist_dict(self, key: str, value: Any) -> None:
        """Best-effort persistent-cache write of dict payloads."""
        if not isinstance(value, dict):
            return
        try:
            get_persistent_cache().set(key, value)
        except _PERSISTENT_CACHE_EXCEPTIONS as exc:
            logger.debug(
                "Failed to write to persistent cache (%s): %s",
                type(exc).__name__,
                exc,
            )

    def set(self, key: str, value: object, ttl_override: Optional[int] = None) -> None:
        """Store value in both in-memory and persistent cache with TTL."""
        ttl = ttl_override if ttl_override is not None else self.ttl_seconds
        with self._lock:
            # Evict oldest entries if at capacity
            while len(self._cache) >= self.max_size and self._access_order:
                oldest_key = next(iter(self._access_order))
                logger.debug("Cache eviction for key: %s", oldest_key[:16])
                del self._cache[oldest_key]
                del self._access_order[oldest_key]

            expires_at = time.time() + ttl
            self._cache[key] = {
                "value": value,
                "expires_at": expires_at,
            }
            self._access_order[key] = None
            self._access_order.move_to_end(key)
            logger.debug("Cache set for key: %s (TTL: %ds)", key[:16], ttl)
            self._persist_dict(key, value)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                if key in self._access_order:
                    del self._access_order[key]
                logger.debug("Cache invalidated for key: %s", key[:16])

    def clear(self) -> None:
        """Clear all entries from cache."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._access_order.clear()
            logger.info("Cache cleared - removed %d entries", count)

    def _persistent_stats_block(self) -> Dict[str, Any]:
        """Return a stats sub-dict for the persistent layer (or error info)."""
        try:
            persistent_stats = get_persistent_cache().stats()
        except _PERSISTENT_CACHE_EXCEPTIONS as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "hits": persistent_stats["hits"],
            "misses": persistent_stats["misses"],
            "size": persistent_stats["size"],
            "max_entries": persistent_stats["max_entries"],
            "hit_rate_percent": persistent_stats["hit_rate_percent"],
            "ttl_seconds": persistent_stats["ttl_seconds"],
            "db_path": persistent_stats["db_path"],
        }

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics: hits, misses, size (memory + persistent)."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0
            now = time.time()
            valid_entries = sum(
                1 for entry in self._cache.values()
                if entry["expires_at"] > now
            )
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": valid_entries,
                "max_size": self.max_size,
                "hit_rate_percent": round(hit_rate, 2),
                "ttl_seconds": self.ttl_seconds,
                "persistent": self._persistent_stats_block(),
            }

    def make_key(self, endpoint: str, params: Dict[str, Any]) -> str:
        """SHA256 cache key from endpoint + sorted params (drops cache-meta keys)."""
        cacheable_params = {
            k: v for k, v in params.items()
            if v is not None and k not in ("cache", "cache_ttl", "session_id")
        }
        raw = endpoint + json.dumps(cacheable_params, sort_keys=True, default=str)
        key = hashlib.sha256(raw.encode()).hexdigest()
        logger.debug("Generated cache key: %s... for endpoint %s", key[:16], endpoint)
        return key


# Module-level singleton
_cache = ResponseCache()


def get_cache() -> ResponseCache:
    """Get the module-level cache singleton."""
    return _cache
