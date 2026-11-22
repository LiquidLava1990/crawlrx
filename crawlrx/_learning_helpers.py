"""Helpers for ``learning.py`` -- DDL fragments, dataclasses, narrow extractors.

Keeps ``learning.py`` below complexity thresholds (fn_length, nesting_depth,
cognitive, param_count) without changing any observable behavior.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Schema fragments -- emit one CREATE TABLE per call site so init_db stays
# under the the function-length limit ceiling.
# ---------------------------------------------------------------------------

SCHEMA_REQUEST_LOG = """
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    scraper TEXT,
    success INTEGER NOT NULL,
    elapsed_ms INTEGER,
    status_code INTEGER,
    error_category TEXT,
    error_text TEXT,
    wait_strategy TEXT,
    stealth_profile TEXT,
    timeout_used INTEGER,
    retries_used INTEGER,
    cache_hit INTEGER DEFAULT 0,
    content_length INTEGER,
    content_quality REAL
);
"""

SCHEMA_DOMAIN_PARAMS = """
CREATE TABLE IF NOT EXISTS domain_params (
    domain TEXT PRIMARY KEY,
    recommended_timeout_ms INTEGER DEFAULT 30000,
    recommended_retries INTEGER DEFAULT 3,
    recommended_wait_strategy TEXT DEFAULT 'networkidle',
    best_stealth_profile TEXT,
    avg_success_rate REAL DEFAULT 1.0,
    avg_response_ms REAL DEFAULT 0,
    p95_response_ms REAL DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    last_success_ts REAL,
    last_failure_ts REAL,
    dominant_error TEXT,
    updated_at REAL
);
"""

SCHEMA_SCRAPER_HEALTH = """
CREATE TABLE IF NOT EXISTS scraper_health (
    scraper TEXT PRIMARY KEY,
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 1.0,
    avg_response_ms REAL DEFAULT 0,
    last_success_ts REAL,
    last_failure_ts REAL,
    disabled INTEGER DEFAULT 0,
    disabled_at REAL,
    last_probe_ts REAL,
    dominant_error TEXT,
    updated_at REAL
);
"""

SCHEMA_FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    scraper TEXT,
    useful INTEGER NOT NULL,
    quality_score REAL,
    feedback_text TEXT,
    agent_id TEXT
);
"""

SCHEMA_STRATEGY_SCORES = """
CREATE TABLE IF NOT EXISTS strategy_scores (
    domain TEXT NOT NULL,
    wait_strategy TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    avg_elapsed_ms REAL DEFAULT 0,
    score REAL DEFAULT 0.5,
    updated_at REAL,
    PRIMARY KEY (domain, wait_strategy)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_req_domain_ts ON request_log(domain, ts);
CREATE INDEX IF NOT EXISTS idx_req_endpoint ON request_log(endpoint, ts);
CREATE INDEX IF NOT EXISTS idx_req_scraper ON request_log(scraper, ts);
CREATE INDEX IF NOT EXISTS idx_feedback_domain ON feedback(domain, ts);
"""


_SCHEMA_FRAGMENTS: tuple[str, ...] = (
    SCHEMA_REQUEST_LOG,
    SCHEMA_DOMAIN_PARAMS,
    SCHEMA_SCRAPER_HEALTH,
    SCHEMA_FEEDBACK,
    SCHEMA_STRATEGY_SCORES,
    SCHEMA_INDEXES,
)


# DDL from modules that keep their own tables in the learning DB (intelligence).
_EXTENSION_FRAGMENTS: list[str] = []


def register_schema(fragment: str) -> None:
    """Add a module's DDL so every lazily opened learning DB creates its tables too."""
    if fragment not in _EXTENSION_FRAGMENTS:
        _EXTENSION_FRAGMENTS.append(fragment)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Execute every DDL fragment (core + registered extensions) and commit."""
    for fragment in (*_SCHEMA_FRAGMENTS, *_EXTENSION_FRAGMENTS):
        conn.executescript(fragment)
    conn.commit()


# ---------------------------------------------------------------------------
# Param bundles -- collapse positional params below the parameter-count limit.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutcomeRecord:
    """Bundled args for :func:`record_outcome`. Bumps the public surface
    from 13 positional params to one struct + ``url``/``endpoint``."""

    url: str
    endpoint: str
    success: bool
    elapsed_ms: int = 0
    status_code: Optional[int] = None
    error: Optional[str] = None
    scraper: Optional[str] = None
    wait_strategy: Optional[str] = None
    stealth_profile: Optional[str] = None
    timeout_used: Optional[int] = None
    retries_used: Optional[int] = None
    cache_hit: bool = False
    content_length: Optional[int] = None


@dataclass(frozen=True)
class FeedbackRecord:
    """Bundled args for :func:`record_feedback`. 7 positional params -> 1 struct."""

    url: str
    endpoint: str
    useful: bool
    quality_score: Optional[float] = None
    feedback_text: Optional[str] = None
    scraper: Optional[str] = None
    agent_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Degrading-scraper analysis -- extracted to keep `get_degrading_scrapers`
# under the cognitive + nesting ceiling.
# ---------------------------------------------------------------------------

DEGRADATION_DELTA = -0.15  # 15% drop = degrading
DEGRADE_WINDOW = 25  # last N requests vs previous N

_RECENT_RATE_SQL = """
    SELECT AVG(success) as rate FROM (
        SELECT success FROM request_log
        WHERE scraper = ? ORDER BY ts DESC LIMIT ?
    )
"""

_OLDER_RATE_SQL = """
    SELECT AVG(success) as rate FROM (
        SELECT success FROM request_log
        WHERE scraper = ? ORDER BY ts DESC LIMIT ? OFFSET ?
    )
"""


def _scraper_window_rate(
    conn: sqlite3.Connection,
    scraper: str,
    sql: str,
    *args: Any,
) -> Optional[float]:
    """Return AVG(success) over a window, or None if there is no data yet."""
    row = conn.execute(sql, (scraper, *args)).fetchone()
    if not row or row["rate"] is None:
        return None
    return float(row["rate"])


def degradation_record(
    conn: sqlite3.Connection,
    scraper: str,
) -> Optional[Dict[str, Any]]:
    """Build a degradation record for one scraper, or None if not degrading."""
    recent = _scraper_window_rate(conn, scraper, _RECENT_RATE_SQL, DEGRADE_WINDOW)
    if recent is None:
        return None
    older = _scraper_window_rate(
        conn, scraper, _OLDER_RATE_SQL, DEGRADE_WINDOW, DEGRADE_WINDOW
    )
    if older is None:
        return None
    delta = recent - older
    if delta >= DEGRADATION_DELTA:
        return None
    return {
        "scraper": scraper,
        "recent_rate": round(recent * 100, 1),
        "previous_rate": round(older * 100, 1),
        "delta_pct": round(delta * 100, 1),
        "status": "degrading",
    }


def collect_degradations(
    conn: sqlite3.Connection,
    scraper_rows: List[sqlite3.Row],
) -> List[Dict[str, Any]]:
    """Run :func:`degradation_record` over every scraper, dropping non-degraders."""
    results: List[Dict[str, Any]] = []
    for row in scraper_rows:
        record = degradation_record(conn, row["scraper"])
        if record is not None:
            results.append(record)
    return results
