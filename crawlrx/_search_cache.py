"""SQLite result cache for search.py (split out of search.py for size)."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from typing import List, Optional

from ._search_helpers import SEARCH_NET_EXCEPTIONS
from .config import CACHE_DB_PATH

logger = logging.getLogger("crawlrx.search.cache")

# Cache TTL for search results (1 hour)
SEARCH_CACHE_TTL = 3600


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def cache_init() -> None:
    """Ensure search_cache table exists."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                results_json TEXT NOT NULL,
                engine TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def cache_get(cache_key: str) -> Optional[dict]:
    """Return cached result dict or None if missing/expired."""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT results_json, engine, created_at FROM search_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, *SEARCH_NET_EXCEPTIONS) as exc:
        logger.warning("Cache get failed: %s", type(exc).__name__)
        return None

    if not row:
        return None
    results_json, engine, created_at = row
    if time.time() - created_at > SEARCH_CACHE_TTL:
        return None
    try:
        data = json.loads(results_json)
    except json.JSONDecodeError:
        logger.warning("Cache get: corrupted JSON")
        return None
    logger.info("Cache HIT for key %s (engine=%s)", cache_key, engine)
    return {"results": data, "engine": engine, "from_cache": True}


def cache_set(cache_key: str, results: list, engine: str) -> None:
    """Store results in cache."""
    try:
        conn = _connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO search_cache
                   (cache_key, results_json, engine, created_at)
                   VALUES (?, ?, ?, ?)""",
                (cache_key, json.dumps(results), engine, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Cache set failed: %s", type(exc).__name__)
        return None  # explicit recovery: non-fatal: (sqlite3.Error, TypeError, ValueError) is recorded but does not abort the caller
    logger.debug("Cache SET for key %s", cache_key)


def make_cache_key(
    query: str,
    engine: str,
    num_results: int,
    language: Optional[str],
    country: Optional[str],
) -> str:
    """Build a deterministic cache key."""
    parts = "%s|%s|%d|%s|%s" % (
        query.lower().strip(),
        engine,
        num_results,
        language or "",
        country or "",
    )
    return hashlib.sha256(parts.encode()).hexdigest()[:32]


def dedup_results(results: List[dict]) -> List[dict]:
    """Deduplicate results list by URL, preserving order."""
    seen: set = set()
    out: List[dict] = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            out.append(r)
    return out
