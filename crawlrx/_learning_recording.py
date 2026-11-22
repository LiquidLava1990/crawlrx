"""Recording side of ``learning.py`` -- write paths only.

Splits the body of :func:`record_outcome` and the three ``_update_*`` helpers
out of ``learning.py`` so each function stays under the function-length limit and
``learning.py`` stays under the file_too_large ceiling.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ._learning_helpers import OutcomeRecord
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.learning")

# Tuning bounds re-imported here so call sites stay in this module.
MIN_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
MIN_RETRIES = 1
MAX_RETRIES = 5
MIN_SAMPLES_FOR_TUNING = 10
SCRAPER_DISABLE_THRESHOLD = 0.3

# Error category names that drive retry tuning.
TIMEOUT_CATEGORY = "timeout"
BOT_BLOCK_CATEGORY = "bot_block"


@dataclass(frozen=True)
class _OutcomeFacts:
    """Per-call facts shared across the three ``_update_*`` helpers."""

    success: bool
    elapsed_ms: int
    now: float


def insert_request_log(
    conn: sqlite3.Connection,
    record: OutcomeRecord,
    domain: str,
    error_category: Optional[str],
) -> None:
    """Append one row to ``request_log``."""
    conn.execute(
        """
        INSERT INTO request_log
        (ts, domain, url, endpoint, scraper, success, elapsed_ms,
         status_code, error_category, error_text, wait_strategy,
         stealth_profile, timeout_used, retries_used, cache_hit, content_length)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(), domain, record.url, record.endpoint, record.scraper,
            1 if record.success else 0, record.elapsed_ms, record.status_code,
            error_category,
            record.error[:500] if record.error else None,
            record.wait_strategy, record.stealth_profile, record.timeout_used,
            record.retries_used, 1 if record.cache_hit else 0, record.content_length,
        ),
    )


def _domain_window_rows(conn: sqlite3.Connection, domain: str) -> list:
    """Pull the last 200 cache-miss requests for a domain in the last 24h."""
    window = time.time() - 86400
    return conn.execute(
        """
        SELECT success, elapsed_ms, error_category
        FROM request_log
        WHERE domain = ? AND ts > ? AND cache_hit = 0
        ORDER BY ts DESC LIMIT 200
        """,
        (domain, window),
    ).fetchall()


def _summarize_window(rows: list) -> Dict[str, Any]:
    """Compute success rate, avg/p95 elapsed, and dominant error from rows."""
    total = len(rows)
    successes = sum(1 for r in rows if r["success"])
    success_rate = successes / total if total > 0 else 1.0
    elapsed = [r["elapsed_ms"] for r in rows if r["elapsed_ms"] and r["elapsed_ms"] > 0]
    avg_ms = sum(elapsed) / len(elapsed) if elapsed else 0
    p95_ms = (
        sorted(elapsed)[int(len(elapsed) * 0.95)] if len(elapsed) >= 5 else avg_ms
    )
    error_counts: Dict[str, int] = {}
    for r in rows:
        if r["error_category"]:
            error_counts[r["error_category"]] = error_counts.get(r["error_category"], 0) + 1
    dominant = max(error_counts, key=error_counts.get) if error_counts else None
    return {
        "total": total,
        "success_rate": success_rate,
        "avg_ms": avg_ms,
        "p95_ms": p95_ms,
        "dominant_error": dominant,
    }


def _recommend_retries(dominant_error: Optional[str], success_rate: float) -> int:
    """Pick a retry count from observed error pattern + success rate."""
    if dominant_error == TIMEOUT_CATEGORY and success_rate < 0.7:
        return min(MAX_RETRIES, 4)
    if dominant_error == BOT_BLOCK_CATEGORY:
        return min(MAX_RETRIES, 2)  # blocks won't yield to retries
    if success_rate > 0.95:
        return MIN_RETRIES
    return 3


def _recommend_timeout(p95_ms: float) -> int:
    """Clamp p95 + 50% headroom into the configured timeout band."""
    if p95_ms <= 0:
        return 30000
    return int(min(MAX_TIMEOUT_MS, max(MIN_TIMEOUT_MS, p95_ms * 1.5)))


def _best_wait_strategy(conn: sqlite3.Connection, domain: str) -> str:
    """Return the best-scoring wait strategy for a domain, defaulting to networkidle."""
    row = conn.execute(
        """
        SELECT wait_strategy FROM strategy_scores
        WHERE domain = ? AND attempts >= ?
        ORDER BY score DESC LIMIT 1
        """,
        (domain, MIN_SAMPLES_FOR_TUNING),
    ).fetchone()
    return row["wait_strategy"] if row else "networkidle"


def update_domain_params(conn: sqlite3.Connection, domain: str) -> None:
    """Recalculate domain parameters from recent request history."""
    rows = _domain_window_rows(conn, domain)
    if not rows:
        return
    summary = _summarize_window(rows)
    recommended_timeout = _recommend_timeout(summary["p95_ms"])
    recommended_retries = _recommend_retries(
        summary["dominant_error"], summary["success_rate"]
    )
    recommended_wait = _best_wait_strategy(conn, domain)
    conn.execute(
        """
        INSERT INTO domain_params
        (domain, recommended_timeout_ms, recommended_retries, recommended_wait_strategy,
         avg_success_rate, avg_response_ms, p95_response_ms, total_requests,
         dominant_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            recommended_timeout_ms = excluded.recommended_timeout_ms,
            recommended_retries = excluded.recommended_retries,
            recommended_wait_strategy = excluded.recommended_wait_strategy,
            avg_success_rate = excluded.avg_success_rate,
            avg_response_ms = excluded.avg_response_ms,
            p95_response_ms = excluded.p95_response_ms,
            total_requests = excluded.total_requests,
            dominant_error = excluded.dominant_error,
            updated_at = excluded.updated_at
        """,
        (
            domain, recommended_timeout, recommended_retries, recommended_wait,
            round(summary["success_rate"], 4), round(summary["avg_ms"], 2),
            round(summary["p95_ms"], 2), summary["total"],
            summary["dominant_error"], time.time(),
        ),
    )
    conn.commit()


@dataclass(frozen=True)
class _DisableContext:
    """Bundled inputs for the auto-disable rule."""

    scraper: str
    total: int
    rate: float
    disabled: int
    now: float


def _maybe_disable_scraper(ctx: _DisableContext) -> tuple[int, Optional[float]]:
    """Apply the auto-disable rule. Returns ``(disabled_flag, disabled_at)``."""
    if ctx.disabled:
        return 1, None  # caller already has disabled_at
    if ctx.total < MIN_SAMPLES_FOR_TUNING * 2:
        return 0, None
    if ctx.rate >= SCRAPER_DISABLE_THRESHOLD:
        return 0, None
    logger.warning(
        "AUTO-DISABLED scraper '%s': success rate %.1f%% (%d total)",
        ctx.scraper, ctx.rate * 100, ctx.total,
    )
    return 1, ctx.now


@dataclass(frozen=True)
class _ScraperUpdate:
    """Bundled args for the scraper_health UPDATE path."""

    scraper: str
    error_value: Optional[str]
    facts: _OutcomeFacts


def _update_existing_scraper_health(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    update: _ScraperUpdate,
) -> None:
    """Apply UPDATE statement for an existing scraper_health row."""
    facts = update.facts
    total = row["total_requests"] + 1
    sc = row["success_count"] + (1 if facts.success else 0)
    rate = sc / total if total > 0 else 1.0
    prev_avg = row["avg_response_ms"] or 0
    avg_ms = (
        ((prev_avg * row["total_requests"]) + facts.elapsed_ms) / total
        if facts.elapsed_ms
        else prev_avg
    )
    flag, new_disabled_at = _maybe_disable_scraper(_DisableContext(
        scraper=update.scraper,
        total=total,
        rate=rate,
        disabled=row["disabled"],
        now=facts.now,
    ))
    disabled_at = new_disabled_at if flag and not row["disabled"] else row["disabled_at"]
    error_to_persist = (
        update.error_value if update.error_value and not facts.success else None
    )
    conn.execute(
        """
        UPDATE scraper_health SET
            total_requests = ?, success_count = ?, success_rate = ?,
            avg_response_ms = ?,
            last_success_ts = CASE WHEN ? THEN ? ELSE last_success_ts END,
            last_failure_ts = CASE WHEN ? THEN ? ELSE last_failure_ts END,
            disabled = ?, disabled_at = ?,
            dominant_error = COALESCE(?, dominant_error),
            updated_at = ?
        WHERE scraper = ?
        """,
        (
            total, sc, round(rate, 4), round(avg_ms, 2),
            facts.success, facts.now, not facts.success, facts.now,
            flag, disabled_at,
            error_to_persist,
            facts.now, update.scraper,
        ),
    )


def _insert_new_scraper_health(
    conn: sqlite3.Connection,
    scraper: str,
    facts: _OutcomeFacts,
) -> None:
    """First-time scraper_health row for a previously-unseen scraper."""
    conn.execute(
        """
        INSERT INTO scraper_health
        (scraper, total_requests, success_count, success_rate, avg_response_ms,
         last_success_ts, last_failure_ts, updated_at)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            scraper,
            1 if facts.success else 0,
            1.0 if facts.success else 0.0,
            facts.elapsed_ms or 0,
            facts.now if facts.success else None,
            facts.now if not facts.success else None,
            facts.now,
        ),
    )


def update_scraper_health(
    conn: sqlite3.Connection,
    scraper: str,
    success: bool,
    elapsed_ms: int,
    error_value: Optional[str],
) -> None:
    """Update scraper module health tracking."""
    facts = _OutcomeFacts(success=success, elapsed_ms=elapsed_ms, now=time.time())
    row = conn.execute(
        "SELECT * FROM scraper_health WHERE scraper = ?", (scraper,)
    ).fetchone()
    if row:
        update = _ScraperUpdate(
            scraper=scraper, error_value=error_value, facts=facts
        )
        _update_existing_scraper_health(conn, row, update)
    else:
        _insert_new_scraper_health(conn, scraper, facts)
    conn.commit()


@dataclass(frozen=True)
class _StrategyKey:
    """Domain + wait strategy pair shared across both branches."""

    domain: str
    wait_strategy: str


def _update_existing_strategy_score(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    key: _StrategyKey,
    facts: _OutcomeFacts,
) -> None:
    """Apply UPDATE for an existing strategy_scores row."""
    attempts = row["attempts"] + 1
    successes = row["successes"] + (1 if facts.success else 0)
    prev_avg = row["avg_elapsed_ms"] or 0
    avg_ms = (
        ((prev_avg * row["attempts"]) + facts.elapsed_ms) / attempts
        if facts.elapsed_ms
        else prev_avg
    )
    success_rate = successes / attempts
    speed_factor = max(0.1, 1.0 - (avg_ms / MAX_TIMEOUT_MS))
    score = success_rate * 0.7 + speed_factor * 0.3  # reliability > speed
    conn.execute(
        """
        UPDATE strategy_scores SET
            attempts = ?, successes = ?, avg_elapsed_ms = ?,
            score = ?, updated_at = ?
        WHERE domain = ? AND wait_strategy = ?
        """,
        (
            attempts, successes, round(avg_ms, 2),
            round(score, 4), facts.now, key.domain, key.wait_strategy,
        ),
    )


def _insert_new_strategy_score(
    conn: sqlite3.Connection,
    key: _StrategyKey,
    facts: _OutcomeFacts,
) -> None:
    """First-time strategy_scores row for a domain/strategy pair."""
    score = 0.5 + (0.3 if facts.success else -0.2)
    conn.execute(
        """
        INSERT INTO strategy_scores
        (domain, wait_strategy, attempts, successes, avg_elapsed_ms, score, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (
            key.domain, key.wait_strategy,
            1 if facts.success else 0,
            facts.elapsed_ms or 0,
            round(score, 4),
            facts.now,
        ),
    )


def update_strategy_score(
    conn: sqlite3.Connection,
    domain: str,
    wait_strategy: str,
    success: bool,
    elapsed_ms: int,
) -> None:
    """Update wait strategy effectiveness score for a domain."""
    facts = _OutcomeFacts(success=success, elapsed_ms=elapsed_ms, now=time.time())
    key = _StrategyKey(domain=domain, wait_strategy=wait_strategy)
    row = conn.execute(
        "SELECT * FROM strategy_scores WHERE domain = ? AND wait_strategy = ?",
        (domain, wait_strategy),
    ).fetchone()
    if row:
        _update_existing_strategy_score(conn, row, key, facts)
    else:
        _insert_new_strategy_score(conn, key, facts)
    conn.commit()
