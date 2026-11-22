"""FastAPI endpoint surface for ``intelligence.py``.

Each route reads a slice of the intelligence DB. Splitting them out keeps the
host file under the file-size limit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

router = APIRouter(prefix="/intel", tags=["intelligence"])


@dataclass(frozen=True)
class _IntelDB:
    """DB access bundle handed to endpoint factories."""

    lock: Any
    factory: Callable[[], Any]
    cycle_interval: int


@dataclass(frozen=True)
class IntelHandlers:
    """Bundle of every callable the intelligence endpoint surface needs."""

    is_running: Callable[[], bool]
    run_strategy_experiment: Callable[[str], Any]
    transfer_domain_knowledge: Callable[[str], Optional[Dict[str, Any]]]
    get_best_stealth_profile: Callable[[str], Optional[str]]


def _run_with_db(db: _IntelDB, body: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    """Acquire the DB lock + connection, run ``body``, close, swallow narrow errors."""
    try:
        with db.lock:
            conn = db.factory()
            try:
                return body(conn)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Status endpoint -- one helper per query keeps it under the LOC ceiling.
# ---------------------------------------------------------------------------

def _query_recent_anomalies(conn: Any, day_ago: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM anomalies WHERE ts > ? ORDER BY ts DESC LIMIT 20",
        (day_ago,),
    ).fetchall()]


def _query_recent_experiments(conn: Any, day_ago: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM experiments WHERE ts > ? ORDER BY ts DESC LIMIT 10",
        (day_ago,),
    ).fetchall()]


def _query_probe_summary(conn: Any, day_ago: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT scraper,
               COUNT(*) as total_probes,
               SUM(success) as successes,
               MAX(consecutive_successes) as max_consecutive
        FROM probe_log WHERE ts > ?
        GROUP BY scraper
        """,
        (day_ago,),
    ).fetchall()]


def _query_quality(conn: Any, day_ago: float) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) as total_assessed,
            SUM(is_captcha) as captcha_pages,
            SUM(is_empty) as empty_pages,
            SUM(is_boilerplate) as boilerplate_pages,
            ROUND(AVG(quality_score), 2) as avg_quality
        FROM content_assessments WHERE ts > ?
        """,
        (day_ago,),
    ).fetchone()
    return dict(row) if row else {}


def _query_stealth_top(conn: Any) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT domain, profile_hash, score, attempts, bot_blocks
        FROM stealth_scores
        WHERE attempts >= 5
        ORDER BY score DESC LIMIT 10
        """
    ).fetchall()]


def _status_body(conn: Any, is_running: Callable[[], bool], cycle_interval: int) -> Dict[str, Any]:
    day_ago = time.time() - 86400
    anomalies = _query_recent_anomalies(conn, day_ago)
    experiments = _query_recent_experiments(conn, day_ago)
    return {
        "running": is_running(),
        "cycle_interval_seconds": cycle_interval,
        "anomalies": {"count": len(anomalies), "recent": anomalies},
        "experiments": {"count": len(experiments), "recent": experiments},
        "probes": _query_probe_summary(conn, day_ago),
        "content_quality": _query_quality(conn, day_ago),
        "stealth_leaderboard": _query_stealth_top(conn),
    }


def _build_status_handler(db: _IntelDB, is_running: Callable[[], bool]) -> Callable:
    async def status_endpoint() -> Dict[str, Any]:
        return _run_with_db(
            db, lambda conn: _status_body(conn, is_running, db.cycle_interval)
        )

    return status_endpoint


# ---------------------------------------------------------------------------
# Other endpoints
# ---------------------------------------------------------------------------

def _anomalies_body(conn: Any) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM anomalies WHERE resolved = 0 ORDER BY ts DESC LIMIT 50"
    ).fetchall()]
    return {"count": len(rows), "anomalies": rows}


def _resolve_anomaly_body(conn: Any, anomaly_id: int) -> Dict[str, Any]:
    conn.execute(
        "UPDATE anomalies SET resolved = 1, resolved_at = ? WHERE id = ?",
        (time.time(), anomaly_id),
    )
    conn.commit()
    return {"resolved": True}


def _experiments_body(conn: Any) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments ORDER BY ts DESC LIMIT 50"
    ).fetchall()]
    return {"count": len(rows), "experiments": rows}


def _build_anomalies_handler(db: _IntelDB) -> Callable:
    async def anomalies_endpoint() -> Dict[str, Any]:
        return _run_with_db(db, _anomalies_body)

    return anomalies_endpoint


def _build_resolve_anomaly_handler(db: _IntelDB) -> Callable:
    async def resolve_anomaly_endpoint(anomaly_id: int) -> Dict[str, Any]:
        return _run_with_db(db, lambda conn: _resolve_anomaly_body(conn, anomaly_id))

    return resolve_anomaly_endpoint


def _build_experiments_handler(db: _IntelDB) -> Callable:
    async def experiments_endpoint() -> Dict[str, Any]:
        return _run_with_db(db, _experiments_body)

    return experiments_endpoint


def _build_trigger_experiment_handler(
    run_strategy_experiment: Callable[[str], Any],
) -> Callable:
    async def trigger_experiment_endpoint(domain: str) -> Dict[str, Any]:
        result = await run_strategy_experiment(domain)
        if result:
            return result
        return {"error": "No suitable URL found for domain or experiment failed"}

    return trigger_experiment_endpoint


def _quality_body(conn: Any, domain: str) -> Dict[str, Any]:
    summary = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            ROUND(AVG(quality_score), 2) as avg_quality,
            SUM(is_captcha) as captcha_count,
            SUM(is_empty) as empty_count,
            SUM(is_boilerplate) as boilerplate_count,
            ROUND(AVG(useful_content_length), 0) as avg_useful_chars
        FROM content_assessments WHERE domain = ?
        """,
        (domain,),
    ).fetchone()
    recent = [dict(r) for r in conn.execute(
        """
        SELECT ts, url, quality_score, is_captcha, is_empty, is_boilerplate,
               content_length, useful_content_length
        FROM content_assessments WHERE domain = ?
        ORDER BY ts DESC LIMIT 20
        """,
        (domain,),
    ).fetchall()]
    return {
        "domain": domain,
        "summary": dict(summary) if summary else {},
        "recent": recent,
    }


def _build_quality_handler(db: _IntelDB) -> Callable:
    async def quality_endpoint(domain: str) -> Dict[str, Any]:
        return _run_with_db(db, lambda conn: _quality_body(conn, domain))

    return quality_endpoint


def _build_transfer_handler(
    transfer_domain_knowledge: Callable[[str], Optional[Dict[str, Any]]],
) -> Callable:
    async def transfer_endpoint(domain: str) -> Dict[str, Any]:
        transferred = transfer_domain_knowledge(domain)
        if transferred:
            return transferred
        return {"domain": domain, "status": "no transferable knowledge available"}

    return transfer_endpoint


def _stealth_body(
    conn: Any,
    domain: str,
    get_best_stealth_profile: Callable[[str], Optional[str]],
) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM stealth_scores WHERE domain = ? ORDER BY score DESC",
        (domain,),
    ).fetchall()]
    best = get_best_stealth_profile(domain)
    return {"domain": domain, "best_profile": best, "profiles": rows}


def _build_stealth_handler(
    db: _IntelDB,
    get_best_stealth_profile: Callable[[str], Optional[str]],
) -> Callable:
    async def stealth_endpoint(domain: str) -> Dict[str, Any]:
        return _run_with_db(
            db, lambda conn: _stealth_body(conn, domain, get_best_stealth_profile)
        )

    return stealth_endpoint


def make_intel_db(lock: "threading.Lock | asyncio.Lock", factory: Callable[[], Any], cycle_interval: int) -> _IntelDB:
    """Public factory for building the DB access bundle."""
    return _IntelDB(lock=lock, factory=factory, cycle_interval=cycle_interval)


def attach_endpoints(
    handlers: IntelHandlers,
    db: _IntelDB,
) -> APIRouter:
    """Wire all intelligence endpoints onto the module router and return it."""
    router.get("/status", summary="Intelligence engine status")(
        _build_status_handler(db, handlers.is_running)
    )
    router.get("/anomalies", summary="Active anomalies")(
        _build_anomalies_handler(db)
    )
    router.post("/anomalies/{anomaly_id}/resolve", summary="Mark anomaly as resolved")(
        _build_resolve_anomaly_handler(db)
    )
    router.get("/experiments", summary="Recent A/B experiments")(
        _build_experiments_handler(db)
    )
    router.post("/experiment/{domain}", summary="Trigger an A/B experiment on a domain")(
        _build_trigger_experiment_handler(handlers.run_strategy_experiment)
    )
    router.get("/quality/{domain}", summary="Content quality report for a domain")(
        _build_quality_handler(db)
    )
    router.get("/transfer/{domain}", summary="Check cross-domain knowledge transfer")(
        _build_transfer_handler(handlers.transfer_domain_knowledge)
    )
    router.get("/stealth/{domain}", summary="Stealth profile effectiveness for a domain")(
        _build_stealth_handler(db, handlers.get_best_stealth_profile)
    )
    return router
