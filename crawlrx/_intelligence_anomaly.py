"""Anomaly detection helpers for ``intelligence.py``.

Splits :func:`detect_anomalies` (86 LOC, depth 5, cognitive 32) into
single-purpose helpers: window-rate fetch, drop record builder, persistence.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("crawlrx.intelligence")

ANOMALY_DROP_THRESHOLD = 0.25
DOMAIN_RECENT_MIN_COUNT = 5
DOMAIN_BASELINE_MIN_COUNT = 10
SCRAPER_RECENT_MIN_COUNT = 3
SCRAPER_BASELINE_MIN_COUNT = 5


@dataclass(frozen=True)
class AnomalyWindow:
    """Recent + baseline timestamps shared by every anomaly query."""

    now: float
    recent_start: float
    baseline_start: float


@dataclass(frozen=True)
class _RateRow:
    """Bundled (rate, count) extracted from one AVG/COUNT query."""

    rate: Optional[float]
    count: int


# SQL templates pre-resolved at import time to avoid f-string SQL drift.
_DOMAIN_OPEN_SQL = (
    "SELECT AVG(success) as rate, COUNT(*) as cnt FROM request_log "
    "WHERE domain = ? AND ts > ? AND cache_hit = 0"
)
_DOMAIN_RANGE_SQL = (
    "SELECT AVG(success) as rate, COUNT(*) as cnt FROM request_log "
    "WHERE domain = ? AND ts > ? AND ts <= ? AND cache_hit = 0"
)
_SCRAPER_OPEN_SQL = (
    "SELECT AVG(success) as rate, COUNT(*) as cnt FROM request_log "
    "WHERE scraper = ? AND ts > ?"
)
_SCRAPER_RANGE_SQL = (
    "SELECT AVG(success) as rate, COUNT(*) as cnt FROM request_log "
    "WHERE scraper = ? AND ts > ? AND ts <= ?"
)


def _execute_rate(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple,
) -> _RateRow:
    """Execute a rate query and wrap the (rate, count) result."""
    row = conn.execute(sql, params).fetchone()
    return _RateRow(rate=row["rate"], count=row["cnt"])


def _rate_for_domain(
    conn: sqlite3.Connection,
    domain: str,
    *,
    start: float,
    end: Optional[float] = None,
) -> _RateRow:
    """Per-domain success rate + count over a time window (cache_hit = 0)."""
    if end is None:
        return _execute_rate(conn, _DOMAIN_OPEN_SQL, (domain, start))
    return _execute_rate(conn, _DOMAIN_RANGE_SQL, (domain, start, end))


def _rate_for_scraper(
    conn: sqlite3.Connection,
    scraper: str,
    *,
    start: float,
    end: Optional[float] = None,
) -> _RateRow:
    """Per-scraper success rate + count over a time window."""
    if end is None:
        return _execute_rate(conn, _SCRAPER_OPEN_SQL, (scraper, start))
    return _execute_rate(conn, _SCRAPER_RANGE_SQL, (scraper, start, end))


def _build_drop_record(
    label_field: str,
    label_value: str,
    anomaly_type: str,
    recent: _RateRow,
    baseline: _RateRow,
) -> Optional[Dict[str, Any]]:
    """Return a drop record if the rate dropped past threshold, else None."""
    if recent.rate is None or baseline.rate is None:
        return None
    drop = baseline.rate - recent.rate
    if drop < ANOMALY_DROP_THRESHOLD:
        return None
    return {
        label_field: label_value,
        "anomaly_type": anomaly_type,
        "baseline_rate": round(baseline.rate * 100, 1),
        "current_rate": round(recent.rate * 100, 1),
        "drop_pct": round(drop * 100, 1),
        "_baseline_raw": baseline.rate,
        "_current_raw": recent.rate,
        "_drop_raw": drop,
    }


def _persist_anomaly(
    conn: sqlite3.Connection,
    label_field: str,
    label_value: str,
    record: Dict[str, Any],
    now: float,
) -> None:
    """Insert one anomaly into the anomalies table."""
    if label_field == "domain":
        conn.execute(
            """
            INSERT INTO anomalies
            (ts, domain, anomaly_type, baseline_rate, current_rate, drop_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, label_value, record["anomaly_type"],
             record["_baseline_raw"], record["_current_raw"], record["_drop_raw"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO anomalies
            (ts, scraper, anomaly_type, baseline_rate, current_rate, drop_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, label_value, record["anomaly_type"],
             record["_baseline_raw"], record["_current_raw"], record["_drop_raw"]),
        )


def _strip_raw(record: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the underscore-prefixed raw helper fields before returning."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def scan_domain_anomalies(
    conn: sqlite3.Connection,
    window: AnomalyWindow,
) -> List[Dict[str, Any]]:
    """Walk every active domain; emit anomaly records + persist them."""
    out: List[Dict[str, Any]] = []
    rows = conn.execute(
        "SELECT DISTINCT domain FROM request_log WHERE ts > ? AND cache_hit = 0",
        (window.recent_start,),
    ).fetchall()
    for row in rows:
        domain = row["domain"]
        recent = _rate_for_domain(conn, domain, start=window.recent_start)
        if recent.count < DOMAIN_RECENT_MIN_COUNT:
            continue
        baseline = _rate_for_domain(
            conn, domain, start=window.baseline_start, end=window.recent_start
        )
        if baseline.count < DOMAIN_BASELINE_MIN_COUNT:
            continue
        record = _build_drop_record(
            "domain", domain, "success_rate_drop", recent, baseline
        )
        if record is None:
            continue
        _persist_anomaly(conn, "domain", domain, record, window.now)
        out.append(_strip_raw(record))
    return out


def scan_scraper_anomalies(
    conn: sqlite3.Connection,
    window: AnomalyWindow,
) -> List[Dict[str, Any]]:
    """Walk every active scraper; emit anomaly records + persist them."""
    out: List[Dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT DISTINCT scraper FROM request_log
        WHERE ts > ? AND scraper IS NOT NULL
        """,
        (window.recent_start,),
    ).fetchall()
    for row in rows:
        scraper = row["scraper"]
        recent = _rate_for_scraper(conn, scraper, start=window.recent_start)
        if recent.count < SCRAPER_RECENT_MIN_COUNT:
            continue
        baseline = _rate_for_scraper(
            conn, scraper, start=window.baseline_start, end=window.recent_start
        )
        if baseline.count < SCRAPER_BASELINE_MIN_COUNT:
            continue
        record = _build_drop_record(
            "scraper", scraper, "scraper_degrading", recent, baseline
        )
        if record is None:
            continue
        _persist_anomaly(conn, "scraper", scraper, record, window.now)
        out.append(_strip_raw(record))
    return out


def make_anomaly_window(window_hours: float, baseline_hours: float) -> AnomalyWindow:
    """Build an AnomalyWindow rooted at ``time.time()``."""
    now = time.time()
    return AnomalyWindow(
        now=now,
        recent_start=now - (window_hours * 3600),
        baseline_start=now - (baseline_hours * 3600),
    )
