"""Disabled-scraper probe helpers for ``intelligence.py``.

Splits :func:`probe_disabled_scrapers` (depth 4, cognitive 38) into single-
purpose async helpers. Each helper opens a fresh DB connection so the host
can stay flat.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.intelligence")

PROBE_INTERVAL_SECONDS = 300
PROBE_CONSECUTIVE_SUCCESSES = 3


@dataclass
class ProbeResult:
    """Outcome of one probe call."""

    success: bool = False
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class ProbeContext:
    """Per-call DB context passed to probe helpers."""

    lock: threading.Lock
    factory: Callable[[], sqlite3.Connection]


def fetch_disabled_scrapers(ctx: ProbeContext) -> List[sqlite3.Row]:
    """Fetch every currently-disabled scraper row."""
    with ctx.lock:
        conn = ctx.factory()
        try:
            return conn.execute(
                """
                SELECT scraper, disabled_at, last_probe_ts
                FROM scraper_health WHERE disabled = 1
                """
            ).fetchall()
        finally:
            conn.close()


def fetch_recent_probe_url(ctx: ProbeContext, scraper: str) -> Optional[str]:
    """Fetch the most recent URL we used with this scraper, or None."""
    with ctx.lock:
        conn = ctx.factory()
        try:
            row = conn.execute(
                """
                SELECT url FROM request_log
                WHERE scraper = ? ORDER BY ts DESC LIMIT 1
                """,
                (scraper,),
            ).fetchone()
            return row["url"] if row else None
        finally:
            conn.close()


async def call_probe_endpoint(probe_url: str) -> ProbeResult:
    """Hit the local /crawl endpoint with a probe payload."""
    result = ProbeResult()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:7337/crawl",
                json={"url": probe_url, "timeout": 15000, "extract_text": True},
            )
            payload = resp.json()
            result.success = payload.get("success", False)
            result.elapsed_ms = payload.get("elapsed_ms", 0)
            if not result.success:
                result.error = payload.get("error", "probe failed")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        result.error = str(exc)
    return result


def _consecutive_count(
    conn: sqlite3.Connection,
    scraper: str,
    success: bool,
) -> int:
    """Tally consecutive probe successes including this one (0 on failure)."""
    if not success:
        return 0
    rows = conn.execute(
        """
        SELECT success FROM probe_log
        WHERE scraper = ? ORDER BY ts DESC LIMIT ?
        """,
        (scraper, PROBE_CONSECUTIVE_SUCCESSES),
    ).fetchall()
    return sum(1 for p in rows if p["success"]) + 1


@dataclass(frozen=True)
class _ProbePersist:
    """Bundle for persist_probe_outcome to keep params <= 5."""

    scraper: str
    result: ProbeResult
    consecutive: int
    now: float


def _insert_probe_log(conn: sqlite3.Connection, payload: _ProbePersist) -> None:
    """Append the probe attempt to probe_log."""
    conn.execute(
        """
        INSERT INTO probe_log
        (ts, scraper, success, elapsed_ms, error, consecutive_successes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payload.now, payload.scraper,
            1 if payload.result.success else 0,
            payload.result.elapsed_ms, payload.result.error,
            payload.consecutive,
        ),
    )


def _touch_probe_ts(conn: sqlite3.Connection, scraper: str, now: float) -> None:
    """Update last_probe_ts for the scraper."""
    conn.execute(
        "UPDATE scraper_health SET last_probe_ts = ? WHERE scraper = ?",
        (now, scraper),
    )


def _maybe_re_enable(conn: sqlite3.Connection, payload: _ProbePersist) -> None:
    """Re-enable the scraper if it crossed PROBE_CONSECUTIVE_SUCCESSES."""
    if payload.consecutive >= PROBE_CONSECUTIVE_SUCCESSES:
        conn.execute(
            """
            UPDATE scraper_health SET
                disabled = 0, disabled_at = NULL, updated_at = ?
            WHERE scraper = ?
            """,
            (payload.now, payload.scraper),
        )
        logger.info(
            "AUTO-RE-ENABLED scraper '%s' after %d consecutive probe successes",
            payload.scraper, payload.consecutive,
        )
        return
    status = "SUCCESS" if payload.result.success else "FAILED"
    logger.info(
        "Probe %s for '%s' (%d/%d consecutive successes needed)",
        status, payload.scraper,
        payload.consecutive, PROBE_CONSECUTIVE_SUCCESSES,
    )


def persist_probe_outcome(ctx: ProbeContext, payload: _ProbePersist) -> None:
    """Persist one probe attempt: log row + last_probe_ts + maybe re-enable."""
    with ctx.lock:
        conn = ctx.factory()
        try:
            # Recompute consecutive against committed history.
            consecutive = _consecutive_count(conn, payload.scraper, payload.result.success)
            payload = _ProbePersist(
                scraper=payload.scraper,
                result=payload.result,
                consecutive=consecutive,
                now=payload.now,
            )
            _insert_probe_log(conn, payload)
            _touch_probe_ts(conn, payload.scraper, payload.now)
            _maybe_re_enable(conn, payload)
            conn.commit()
        finally:
            conn.close()


async def probe_one_scraper(
    ctx: ProbeContext,
    row: sqlite3.Row,
    now: float,
) -> None:
    """One full probe lifecycle: precondition checks, request, persist."""
    scraper = row["scraper"]
    last_probe = row["last_probe_ts"] or 0
    if now - last_probe < PROBE_INTERVAL_SECONDS:
        return
    logger.info("Probing disabled scraper '%s'", scraper)
    probe_url = fetch_recent_probe_url(ctx, scraper)
    if not probe_url:
        return
    result = await call_probe_endpoint(probe_url)
    persist_probe_outcome(
        ctx,
        _ProbePersist(scraper=scraper, result=result, consecutive=0, now=now),
    )


async def _safe_probe_one(ctx: ProbeContext, row: dict, now: float) -> None:
    """Probe one disabled scraper, swallowing recoverable errors."""
    try:
        await probe_one_scraper(ctx, row, now)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "probe_all_disabled iter '%s' failed: %s", row["scraper"], exc
        )
        return None  # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; per-row skip


async def probe_all_disabled(ctx: ProbeContext) -> None:
    """Top-level: probe every disabled scraper, swallow narrow errors."""
    try:
        rows = fetch_disabled_scrapers(ctx)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("probe_all_disabled fetch failed: %s", exc)
        return None  # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; nothing to probe
    now = time.time()
    for row in rows:
        await _safe_probe_one(ctx, row, now)
