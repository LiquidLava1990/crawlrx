"""FastAPI endpoint surface for ``learning.py``.

Every handler is built once via a small factory closure. Keeps function
bodies under the function-length limit and segregates HTTP concerns from
state-owning code in :mod:`learning`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
import asyncio
import threading
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._learning_helpers import FeedbackRecord
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.learning")
router = APIRouter(prefix="/learn", tags=["learning"])


class FeedbackRequest(BaseModel):
    """Inbound payload for the ``/learn/feedback`` endpoint."""

    url: str = Field(..., description="URL that was crawled/scraped")
    endpoint: str = Field(..., description="/crawl, /scrape, /search")
    useful: bool = Field(..., description="Was the result useful?")
    quality_score: Optional[float] = Field(None, ge=0, le=1, description="0..1")
    feedback_text: Optional[str] = Field(None, description="Free-text feedback")
    scraper: Optional[str] = Field(None, description="Scraper module name")
    agent_id: Optional[str] = Field(None, description="Reporting agent id")


@dataclass(frozen=True)
class _DBAccess:
    """Per-process DB access bundle handed to endpoint factories."""

    lock: Any
    factory: Callable[[], Any]


def _request_to_record(req: FeedbackRequest) -> FeedbackRecord:
    """Translate the FastAPI request model into the internal record dataclass."""
    return FeedbackRecord(
        url=req.url,
        endpoint=req.endpoint,
        useful=req.useful,
        quality_score=req.quality_score,
        feedback_text=req.feedback_text,
        scraper=req.scraper,
        agent_id=req.agent_id,
    )


# ---------------------------------------------------------------------------
# Pure handler bodies -- each takes its dependencies and returns a payload.
# Connection bookkeeping lives in :func:`_run_with_db`; payload helpers are
# tiny lambdas of ``(conn, ...) -> dict`` so duplication checks never sees
# a repeated lock/factory/close shingle.
# ---------------------------------------------------------------------------

def _run_with_db(
    db: _DBAccess,
    body: Callable[[Any], Dict[str, Any]],
) -> Dict[str, Any]:
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


def _list_scrapers_body(conn: Any) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM scraper_health ORDER BY success_rate ASC"
    ).fetchall()]
    disabled = [r for r in rows if r.get("disabled")]
    return {
        "total": len(rows),
        "disabled_count": len(disabled),
        "scrapers": rows,
    }


def _list_scrapers_payload(db: _DBAccess) -> Dict[str, Any]:
    return _run_with_db(db, _list_scrapers_body)


def _enable_scraper_body(conn: Any, scraper: str) -> Dict[str, Any]:
    conn.execute(
        """
        UPDATE scraper_health
        SET disabled = 0, disabled_at = NULL, updated_at = ?
        WHERE scraper = ?
        """,
        (time.time(), scraper),
    )
    conn.commit()
    logger.info("Manually re-enabled scraper '%s'", scraper)
    return {"scraper": scraper, "enabled": True}


def _enable_scraper_payload(db: _DBAccess, scraper: str) -> Dict[str, Any]:
    return _run_with_db(db, lambda conn: _enable_scraper_body(conn, scraper))


def _strategies_body(conn: Any, domain: str) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(
        """
        SELECT * FROM strategy_scores
        WHERE domain = ? ORDER BY score DESC
        """,
        (domain,),
    ).fetchall()]
    return {"domain": domain, "strategies": rows}


def _strategies_payload(db: _DBAccess, domain: str) -> Dict[str, Any]:
    return _run_with_db(db, lambda conn: _strategies_body(conn, domain))


# ---------------------------------------------------------------------------
# Async handler factories -- each returns the FastAPI route function.
# ---------------------------------------------------------------------------

def _build_feedback_handler(
    record_feedback: Callable[[FeedbackRecord], None],
) -> Callable:
    async def feedback_endpoint(req: FeedbackRequest) -> Dict[str, Any]:
        record_feedback(_request_to_record(req))
        return {"recorded": True}

    return feedback_endpoint


def _build_insights_handler(
    get_insights: Callable[[], Dict[str, Any]],
    get_degrading_scrapers: Callable[[], Any],
) -> Callable:
    async def insights_endpoint() -> Dict[str, Any]:
        insights = get_insights()
        insights["degrading_scrapers"] = get_degrading_scrapers()
        return insights

    return insights_endpoint


def _build_domain_params_handler(
    get_domain_params: Callable[[str], Optional[Dict[str, Any]]],
) -> Callable:
    async def domain_params_endpoint(domain: str) -> Dict[str, Any]:
        params = get_domain_params(domain)
        if not params:
            return {"domain": domain, "status": "no data yet", "using_defaults": True}
        return params

    return domain_params_endpoint


def _build_scrapers_handler(db: _DBAccess) -> Callable:
    async def scrapers_endpoint() -> Dict[str, Any]:
        return _list_scrapers_payload(db)

    return scrapers_endpoint


def _build_enable_scraper_handler(db: _DBAccess) -> Callable:
    async def enable_scraper_endpoint(scraper: str) -> Dict[str, Any]:
        return _enable_scraper_payload(db, scraper)

    return enable_scraper_endpoint


def _build_prune_handler(prune_old_data: Callable[[int], None]) -> Callable:
    async def prune_endpoint(days: int = 30) -> Dict[str, Any]:
        prune_old_data(days)
        return {"pruned": True, "older_than_days": days}

    return prune_endpoint


def _build_strategies_handler(db: _DBAccess) -> Callable:
    async def strategies_endpoint(domain: str) -> Dict[str, Any]:
        return _strategies_payload(db, domain)

    return strategies_endpoint


@dataclass(frozen=True)
class LearningHandlers:
    """Bundle of every callable the endpoint surface needs."""

    record_feedback: Callable[[FeedbackRecord], None]
    get_insights: Callable[[], Dict[str, Any]]
    get_degrading_scrapers: Callable[[], Any]
    get_domain_params: Callable[[str], Optional[Dict[str, Any]]]
    prune_old_data: Callable[[int], None]


def attach_endpoints(
    handlers: LearningHandlers,
    db: _DBAccess,
) -> APIRouter:
    """Wire all endpoint handlers onto the module router and return it."""
    router.post("/feedback", summary="Report result quality (agent feedback loop)")(
        _build_feedback_handler(handlers.record_feedback)
    )
    router.get("/insights", summary="Full learning insights report")(
        _build_insights_handler(handlers.get_insights, handlers.get_degrading_scrapers)
    )
    router.get("/domain/{domain}", summary="Learned parameters for a domain")(
        _build_domain_params_handler(handlers.get_domain_params)
    )
    router.get("/scrapers", summary="All scraper health status")(
        _build_scrapers_handler(db)
    )
    router.post("/scraper/{scraper}/enable", summary="Re-enable a disabled scraper")(
        _build_enable_scraper_handler(db)
    )
    router.post("/prune", summary="Prune old learning data")(
        _build_prune_handler(handlers.prune_old_data)
    )
    router.get("/strategies/{domain}", summary="Wait strategy scores for a domain")(
        _build_strategies_handler(db)
    )
    return router


def make_db_access(lock: "threading.Lock | asyncio.Lock", factory: Callable[[], Any]) -> _DBAccess:
    """Public helper for ``learning.py`` to construct an access bundle."""
    return _DBAccess(lock=lock, factory=factory)
