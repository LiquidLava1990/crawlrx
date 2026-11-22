"""
CrawlRX Persistent SQLite Cache
================================
SQLite-backed persistent cache -- survives restarts.
Thread-safe using threading.Lock.
"""

import os
import json
import time
import sqlite3
import threading
import logging
from typing import Optional, Dict
from pathlib import Path

logger = logging.getLogger("crawlrx.persistent_cache")


class PersistentCache:
    """
    SQLite-backed persistent cache -- survives restarts.
    
    - TTL default: 1 hour (configurable)
    - Max 10000 entries (oldest evicted when full)
    - Thread-safe using threading.Lock
    - Auto-cleanup of expired entries every hour
    """
    
    def __init__(self, db_path: str, ttl_seconds: int = 3600, max_entries: int = 10000) -> None:
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._cleanup_stopped = False
        self._cleanup_timer: threading.Timer | None = None
        
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._init_db()
        
        # Run initial cleanup and schedule periodic cleanup
        self.cleanup_expired()
        self._schedule_cleanup()
    
    def _init_db(self) -> None:
        """Initialize SQLite database with cache table."""
        with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)
            """)
            conn.commit()
        logger.info("Persistent cache initialized at %s", self.db_path)
    
    def _run_periodic_cleanup(self) -> None:
        """Single tick of the periodic cleanup chain (no recursion)."""
        if self._cleanup_stopped:
            return
        try:
            deleted = self.cleanup_expired()
            if deleted > 0:
                logger.debug("Periodic cleanup removed %d expired entries", deleted)
        except (sqlite3.Error, OSError) as exc:
            logger.error("Error in periodic cleanup: %s", exc)
            return None  # explicit recovery: cleanup is best-effort; (sqlite3.Error, OSError) is non-fatal
        if not self._cleanup_stopped:
            self._schedule_cleanup()

    def _schedule_cleanup(self) -> None:
        """Schedule periodic cleanup every hour."""
        timer = threading.Timer(3600.0, self._run_periodic_cleanup)
        timer.daemon = True
        self._cleanup_timer = timer
        timer.start()

    def stop_cleanup(self) -> None:
        """Cancel the periodic cleanup timer chain."""
        self._cleanup_stopped = True
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
    
    def _decode_or_purge(self, conn: sqlite3.Connection, key: str, value_json: str) -> Optional[dict]:
        """JSON-decode a cached value; purge + None on decode failure."""
        try:
            value = json.loads(value_json)
            self._hits += 1
            logger.debug("Persistent cache hit for key: %s", key[:16])
            return value
        except json.JSONDecodeError as e:
            logger.error("Failed to decode cached value for key %s: %s", key[:16], e)
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            conn.commit()
            self._misses += 1
            return None

    def _get_locked(self, key: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            if row is None:
                self._misses += 1
                return None
            value_json, expires_at = row
            if time.time() > expires_at:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                conn.commit()
                self._misses += 1
                return None
            return self._decode_or_purge(conn, key, value_json)

    def get(self, key: str) -> Optional[dict]:
        """Return the cached dict for ``key`` or None if missing/expired."""
        with self._lock:
            try:
                return self._get_locked(key)
            except sqlite3.Error as e:
                logger.error("SQLite error in get(): %s", e)
                self._misses += 1
                return None
    
    def set(self, key: str, value: dict) -> None:
        """
        Store value in persistent cache with TTL.
        
        Args:
            key: Cache key
            value: Dict value to cache (will be JSON serialized)
        """
        with self._lock:
            try:
                # Evict oldest entries if at capacity
                self._evict_if_needed()
                
                # Serialize value to JSON
                value_json = json.dumps(value, default=str)
                
                expires_at = time.time() + self.ttl_seconds
                created_at = time.time()
                
                with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO cache (key, value, expires_at, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, value_json, expires_at, created_at)
                    )
                    conn.commit()
                
                logger.debug("Persistent cache set for key: %s (TTL: %ds)", key[:16], self.ttl_seconds)
                
            except (sqlite3.Error, TypeError) as e:
                logger.error("Failed to set cache for key %s: %s", key[:16], e)
                return None  # explicit recovery: non-fatal: (sqlite3.Error, TypeError) is recorded but does not abort the caller
    
    def _evict_if_needed(self) -> None:
        """Evict oldest entries if cache is at capacity."""
        try:
            with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                # Count current entries
                cursor = conn.execute("SELECT COUNT(*) FROM cache")
                count = cursor.fetchone()[0]
                
                if count >= self.max_entries:
                    # Delete oldest entries (by created_at)
                    to_delete = count - self.max_entries + 1  # +1 to make room for new entry
                    conn.execute(
                        """
                        DELETE FROM cache WHERE key IN (
                            SELECT key FROM cache ORDER BY created_at ASC LIMIT ?
                        )
                        """,
                        (to_delete,)
                    )
                    conn.commit()
                    logger.debug("Evicted %d oldest entries from persistent cache", to_delete)
        except sqlite3.Error as e:
            logger.error("Error during cache eviction: %s", e)
            return None  # explicit recovery: non-fatal: sqlite3.Error is recorded but does not abort the caller
    
    def _open_wal_conn(self) -> sqlite3.Connection:
        """Open a WAL-mode connection (caller ``with``-manages lifetime)."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def delete(self, key: str) -> None:
        """Remove a specific key from persistent cache."""
        with self._lock:
            try:
                with self._open_wal_conn() as conn:
                    conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                    conn.commit()
                logger.debug("Persistent cache deleted for key: %s", key[:16])
            except sqlite3.Error as exc:
                logger.error("Failed to delete cache for key %s: %s", key[:16], exc)
                return None  # explicit recovery: non-fatal: sqlite3.Error is recorded but does not abort the caller

    def clear(self) -> None:
        """Clear all entries from persistent cache."""
        with self._lock:
            try:
                with self._open_wal_conn() as conn:
                    cursor = conn.execute("DELETE FROM cache")
                    conn.commit()
                    count = cursor.rowcount
                logger.info("Persistent cache cleared - removed %d entries", count)
                self._hits = 0
                self._misses = 0
            except sqlite3.Error as exc:
                logger.error("Failed to clear cache: %s", exc)
                return None  # explicit recovery: non-fatal: sqlite3.Error is recorded but does not abort the caller
    
    def stats(self) -> dict:
        """
        Return cache statistics.
        
        Returns:
            Dict with hits, misses, size, hit_rate, ttl_seconds, max_entries
        """
        with self._lock:
            try:
                with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    # Count non-expired entries
                    now = time.time()
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM cache WHERE expires_at > ?",
                        (now,)
                    )
                    valid_entries = cursor.fetchone()[0]
                    
                    # Total entries
                    cursor = conn.execute("SELECT COUNT(*) FROM cache")
                    total_entries = cursor.fetchone()[0]
                
                total_requests = self._hits + self._misses
                hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0
                
                return {
                    "hits": self._hits,
                    "misses": self._misses,
                    "size": valid_entries,
                    "total_entries": total_entries,
                    "hit_rate_percent": round(hit_rate, 2),
                    "ttl_seconds": self.ttl_seconds,
                    "max_entries": self.max_entries,
                    "db_path": self.db_path,
                }
            except sqlite3.Error as e:
                logger.error("Failed to get cache stats: %s", e)
                return {
                    "hits": self._hits,
                    "misses": self._misses,
                    "size": 0,
                    "total_entries": 0,
                    "hit_rate_percent": 0,
                    "ttl_seconds": self.ttl_seconds,
                    "max_entries": self.max_entries,
                    "db_path": self.db_path,
                    "error": str(e),
                }
    
    def cleanup_expired(self) -> int:
        """
        Remove all expired entries from cache.
        
        Returns:
            Number of entries deleted.
        """
        with self._lock:
            try:
                with sqlite3.connect(self.db_path, check_same_thread=False, timeout=10) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    now = time.time()
                    cursor = conn.execute(
                        "DELETE FROM cache WHERE expires_at <= ?",
                        (now,)
                    )
                    conn.commit()
                    deleted = cursor.rowcount
                
                if deleted > 0:
                    logger.info("Cleaned up %d expired entries from persistent cache", deleted)
                return deleted
                
            except sqlite3.Error as e:
                logger.error("Failed to cleanup expired cache: %s", e)
                return 0


# Module-level singleton
_persistent_cache: Optional[PersistentCache] = None
_persistent_cache_lock = threading.Lock()


def get_persistent_cache(
    db_path: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    max_entries: Optional[int] = None
) -> PersistentCache:
    """
    Get the module-level persistent cache singleton.
    
    Args:
        db_path: Path to SQLite database (default: crawlrx_cache.db in parent dir)
        ttl_seconds: TTL in seconds (default: 3600)
        max_entries: Maximum entries (default: 10000)
    
    Returns:
        PersistentCache instance
    """
    global _persistent_cache
    
    if _persistent_cache is None:
        with _persistent_cache_lock:
            if _persistent_cache is None:
                # Import here to avoid circular import
                from .config import CACHE_DB_PATH, CACHE_TTL
                
                db = db_path or CACHE_DB_PATH
                ttl = ttl_seconds or CACHE_TTL
                max_ent = max_entries or 10000
                
                _persistent_cache = PersistentCache(
                    db_path=db,
                    ttl_seconds=ttl,
                    max_entries=max_ent
                )
    
    return _persistent_cache


def reset_persistent_cache() -> None:
    """Reset the singleton (mainly for testing)."""
    global _persistent_cache
    with _persistent_cache_lock:
        _persistent_cache = None

