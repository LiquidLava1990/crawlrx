"""Insights / analytics queries for ``learning.py``.

Each top-level function reads a slice of the learning DB. Splitting them out
keeps :func:`get_insights` under the function-length limit and shrinks the host file.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List

MIN_SAMPLES_FOR_TUNING = 10


def _request_volume(conn: sqlite3.Connection, day_ago: float, hour_ago: float) -> Dict[str, int]:
    """Total / hourly / 24h success+failure counts."""
    total = conn.execute("SELECT COUNT(*) as c FROM request_log").fetchone()["c"]
    recent = conn.execute(
        "SELECT COUNT(*) as c FROM request_log WHERE ts > ?", (hour_ago,)
    ).fetchone()["c"]
    successes = conn.execute(
        "SELECT COUNT(*) as c FROM request_log WHERE success = 1 AND ts > ?",
        (day_ago,),
    ).fetchone()["c"]
    failures = conn.execute(
        "SELECT COUNT(*) as c FROM request_log WHERE success = 0 AND ts > ?",
        (day_ago,),
    ).fetchone()["c"]
    return {"total": total, "recent": recent, "successes": successes, "failures": failures}


def _failing_domains(conn: sqlite3.Connection, day_ago: float) -> List[Dict[str, Any]]:
    """Top failing domains by failure ratio in the last 24h."""
    return [dict(r) for r in conn.execute(
        """
        SELECT domain,
               COUNT(*) as total,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures,
               ROUND(CAST(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100, 1) as failure_pct
        FROM request_log WHERE ts > ?
        GROUP BY domain HAVING failures > 0
        ORDER BY failure_pct DESC LIMIT 10
        """,
        (day_ago,),
    ).fetchall()]


def _scraper_health_overview(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """All scrapers ordered by ascending success rate."""
    return [dict(r) for r in conn.execute(
        """
        SELECT scraper, total_requests, success_rate, disabled,
               dominant_error, avg_response_ms
        FROM scraper_health ORDER BY success_rate ASC
        """
    ).fetchall()]


def _error_breakdown(conn: sqlite3.Connection, day_ago: float) -> Dict[str, int]:
    """Error category histogram for the last 24h."""
    rows = conn.execute(
        """
        SELECT error_category, COUNT(*) as c
        FROM request_log WHERE success = 0 AND ts > ? AND error_category IS NOT NULL
        GROUP BY error_category ORDER BY c DESC
        """,
        (day_ago,),
    ).fetchall()
    return {r["error_category"]: r["c"] for r in rows}


def _top_domains(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Top domains by request volume."""
    return [dict(r) for r in conn.execute(
        """
        SELECT domain, total_requests, avg_success_rate, recommended_timeout_ms,
               recommended_wait_strategy, p95_response_ms
        FROM domain_params ORDER BY total_requests DESC LIMIT 10
        """
    ).fetchall()]


def _feedback_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Feedback totals + useful-rate."""
    feedback_total = conn.execute(
        "SELECT COUNT(*) as c FROM feedback"
    ).fetchone()["c"]
    feedback_useful = conn.execute(
        "SELECT COUNT(*) as c FROM feedback WHERE useful = 1"
    ).fetchone()["c"]
    rate = (
        round(feedback_useful / feedback_total * 100, 1) if feedback_total > 0 else 0
    )
    return {
        "total": feedback_total,
        "useful": feedback_useful,
        "useful_rate": rate,
    }


def _disabled_scrapers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """All scrapers currently auto-disabled."""
    return [dict(r) for r in conn.execute(
        """
        SELECT scraper, success_rate, total_requests, dominant_error, disabled_at
        FROM scraper_health WHERE disabled = 1
        """
    ).fetchall()]


def _strategy_winners(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Top wait-strategy winners per domain (most-tested first)."""
    return [dict(r) for r in conn.execute(
        """
        SELECT domain, wait_strategy, score, attempts
        FROM strategy_scores
        WHERE attempts >= ? AND score = (
            SELECT MAX(s2.score) FROM strategy_scores s2
            WHERE s2.domain = strategy_scores.domain AND s2.attempts >= ?
        )
        ORDER BY attempts DESC LIMIT 10
        """,
        (MIN_SAMPLES_FOR_TUNING, MIN_SAMPLES_FOR_TUNING),
    ).fetchall()]


def _tuned_domain_count(conn: sqlite3.Connection) -> int:
    """How many domains have enough samples to be considered tuned."""
    return conn.execute(
        "SELECT COUNT(*) as c FROM domain_params WHERE total_requests >= ?",
        (MIN_SAMPLES_FOR_TUNING,),
    ).fetchone()["c"]


def _success_rate_pct(volume: Dict[str, int]) -> float:
    """Success-rate percentage for the 24h window."""
    successes = volume["successes"]
    failures = volume["failures"]
    denom = successes + failures
    if denom <= 0:
        return 100
    return round(successes / denom * 100, 1)


def collect_insights(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Aggregate the insights report from all sub-queries."""
    now = time.time()
    day_ago = now - 86400
    hour_ago = now - 3600

    volume = _request_volume(conn, day_ago, hour_ago)
    return {
        "total_observations": volume["total"],
        "requests_last_hour": volume["recent"],
        "last_24h": {
            "successes": volume["successes"],
            "failures": volume["failures"],
            "success_rate": _success_rate_pct(volume),
        },
        "tuned_domains": _tuned_domain_count(conn),
        "top_domains": _top_domains(conn),
        "failing_domains": _failing_domains(conn, day_ago),
        "error_breakdown": _error_breakdown(conn, day_ago),
        "scrapers": _scraper_health_overview(conn),
        "disabled_scrapers": _disabled_scrapers(conn),
        "strategy_winners": _strategy_winners(conn),
        "feedback": _feedback_summary(conn),
    }
