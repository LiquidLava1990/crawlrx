"""
learning.py -- CrawlRX Self-Learning Engine
============================================
Closes the feedback loop: every request outcome feeds back into strategy
selection.

What it learns:
- Per-domain success rates, avg response times, common error types
- Per-scraper module success rates (which scrapers are breaking)
- Which wait strategies work best per domain
- Which stealth profiles reduce detection rates
- Optimal timeout/retry parameters per domain

Schema, recording, and endpoint code live in sibling modules:
- ``_learning_helpers``    -- DDL fragments + dataclasses + degradation pure helpers
- ``_learning_recording``  -- record_outcome / update_*  write paths
- ``_learning_insights``   -- get_insights query bundle
- ``_learning_endpoints``  -- FastAPI surface

This file owns the connection factory, the global lock, the ``record_*`` /
``get_*`` query API, and the public ``router`` re-exported from the
endpoints module.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from ._learning_endpoints import (
    LearningHandlers,
    attach_endpoints,
    make_db_access,
)
from ._learning_helpers import (
    FeedbackRecord,
    OutcomeRecord,
    apply_schema,
    collect_degradations,
)
from ._learning_insights import collect_insights
from ._learning_recording import (
    MIN_SAMPLES_FOR_TUNING,
    insert_request_log,
    update_domain_params,
    update_scraper_health,
    update_strategy_score,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .paths import env_path

logger = logging.getLogger("crawlrx.learning")

# DB path next to the existing cache DB unless the runtime provides a volume.
_DB_PATH = env_path("CRAWLRX_LEARNING_DB", "crawlrx_learning.db")

# Re-exported tuning bounds (kept here for backward compatibility -- callers
# elsewhere in the codebase import these names from ``learning``).
SCRAPER_DISABLE_THRESHOLD = 0.3
SCRAPER_PROBE_INTERVAL = 300
SCRAPER_RECOVERY_THRESHOLD = 0.6
MIN_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
MIN_RETRIES = 1
MAX_RETRIES = 5


class WaitStrategy(str, Enum):
    NETWORKIDLE = "networkidle"
    DOMCONTENTLOADED = "domcontentloaded"
    SETTLE_1500 = "settle_1500"
    SETTLE_3000 = "settle_3000"
    LOAD = "load"


class ErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    BOT_BLOCK = "bot_block"
    NETWORK = "network"
    SELECTOR_MISS = "selector_miss"
    CONTENT_EMPTY = "content_empty"
    CRASH = "crash"
    UNKNOWN = "unknown"


_ERROR_KEYWORDS: tuple[tuple[ErrorCategory, tuple[str, ...]], ...] = (
    (ErrorCategory.TIMEOUT, ("timeout", "timed out")),
    (ErrorCategory.BOT_BLOCK, ("cloudflare", "bot", "block", "captcha", "403")),
    (ErrorCategory.NETWORK, ("connection", "network", "dns", "refused")),
    (ErrorCategory.SELECTOR_MISS, ("selector", "matched 0", "matched nothing")),
    (ErrorCategory.CONTENT_EMPTY, ("empty", "no content")),
    (ErrorCategory.CRASH, ("crash", "exception", "error")),
)


def _categorize_error(error_str: Optional[str]) -> ErrorCategory:
    """Classify an error string into a category for pattern analysis."""
    if not error_str:
        return ErrorCategory.UNKNOWN
    lowered = error_str.lower()
    for category, keywords in _ERROR_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return category
    return ErrorCategory.UNKNOWN


def _extract_domain(url: str) -> str:
    """Extract bare domain from URL."""
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except BROWSER_SCRAPE_EXCEPTIONS:
        return url


# ---------------------------------------------------------------------------
# DB connection -- module-level singleton lock + factory
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


_schema_ready: list[str] = []  # DB paths whose schema has been applied this process


def _get_db() -> sqlite3.Connection:
    """Open a SQLite connection; the schema is applied on first use.

    Lazy schema creation lets library / CLI callers (no FastAPI lifespan)
    use the learning store without calling ``init_db`` first.
    """
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    if _DB_PATH not in _schema_ready:
        apply_schema(conn)
        _schema_ready.append(_DB_PATH)
    return conn


def init_db() -> None:
    """Create learning tables if they don't exist."""
    conn = _get_db()
    try:
        apply_schema(conn)
        logger.info("Learning DB initialized at %s", _DB_PATH)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording API -- thin orchestrator over ``_learning_recording``.
# ---------------------------------------------------------------------------

def _persist_outcome(
    conn: sqlite3.Connection,
    record: OutcomeRecord,
    domain: str,
    category_value: Optional[str],
) -> None:
    """Run the four DB writes that close out one outcome record."""
    insert_request_log(conn, record, domain, category_value)
    conn.commit()
    update_domain_params(conn, domain)
    if record.scraper:
        update_scraper_health(
            conn,
            record.scraper,
            record.success,
            record.elapsed_ms,
            category_value,
        )
    if record.wait_strategy:
        update_strategy_score(
            conn,
            domain,
            record.wait_strategy,
            record.success,
            record.elapsed_ms,
        )


def record_outcome(*args: object, **kwargs: object) -> None:
    """Record one outcome. Accepts OutcomeRecord positional or legacy kwargs."""
    record = _coerce_outcome(args, kwargs)
    try:
        domain = _extract_domain(record.url)
        category = _categorize_error(record.error) if record.error else None
        category_value = category.value if category else None
        with _db_lock:
            conn = _get_db()
            try:
                _persist_outcome(conn, record, domain, category_value)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Learning record failed (non-fatal): %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


def _coerce_outcome(args: tuple, kwargs: dict) -> OutcomeRecord:
    """Adapt either ``OutcomeRecord`` positional or legacy kwargs into a record."""
    if len(args) == 1 and isinstance(args[0], OutcomeRecord):
        return args[0]
    if args:
        raise TypeError(
            "record_outcome accepts only OutcomeRecord positional or kwargs"
        )
    return OutcomeRecord(**kwargs)


def record_feedback(*args: object, **kwargs: object) -> None:
    """Record feedback from an agent. Accepts FeedbackRecord or legacy kwargs."""
    record = _coerce_feedback(args, kwargs)
    try:
        domain = _extract_domain(record.url)
        with _db_lock:
            conn = _get_db()
            try:
                _insert_feedback_row(conn, record, domain)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Feedback record failed: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


def _coerce_feedback(args: tuple, kwargs: dict) -> FeedbackRecord:
    """Adapt either ``FeedbackRecord`` positional or legacy kwargs into a record."""
    if len(args) == 1 and isinstance(args[0], FeedbackRecord):
        return args[0]
    if args:
        raise TypeError(
            "record_feedback accepts only FeedbackRecord positional or kwargs"
        )
    return FeedbackRecord(**kwargs)


def _insert_feedback_row(
    conn: sqlite3.Connection,
    record: FeedbackRecord,
    domain: str,
) -> None:
    """Persist one feedback row + commit."""
    conn.execute(
        """
        INSERT INTO feedback
        (ts, domain, url, endpoint, scraper, useful,
         quality_score, feedback_text, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(), domain, record.url, record.endpoint,
            record.scraper, 1 if record.useful else 0,
            record.quality_score, record.feedback_text,
            record.agent_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Querying -- recommendation API
# ---------------------------------------------------------------------------

def get_domain_params(domain: str) -> Optional[Dict[str, Any]]:
    """Get learned parameters for a domain. Returns None if no data yet."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                row = conn.execute(
                    "SELECT * FROM domain_params WHERE domain = ?", (domain,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("get_domain_params failed: %s", exc)
        return None


def get_recommended_wait_strategy(domain: str) -> Optional[str]:
    """Get the best wait strategy for a domain based on historical data."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                row = conn.execute(
                    """
                    SELECT wait_strategy, score FROM strategy_scores
                    WHERE domain = ? AND attempts >= ?
                    ORDER BY score DESC LIMIT 1
                    """,
                    (domain, MIN_SAMPLES_FOR_TUNING),
                ).fetchone()
                return row["wait_strategy"] if row else None
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None


def is_scraper_disabled(scraper: str) -> bool:
    """Check if a scraper has been auto-disabled due to low success rate."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                return _check_scraper_disabled(conn, scraper)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS:
        return False


def _check_scraper_disabled(conn: sqlite3.Connection, scraper: str) -> bool:
    """Sub-helper that flattens the disabled-state lookup to depth 2."""
    row = conn.execute(
        "SELECT disabled, disabled_at FROM scraper_health WHERE scraper = ?",
        (scraper,),
    ).fetchone()
    if not row or not row["disabled"]:
        return False
    disabled_at = row["disabled_at"]
    if disabled_at and (time.time() - disabled_at) > SCRAPER_PROBE_INTERVAL:
        return False  # Allow probe request through
    return True


def get_recommended_timeout(domain: str) -> int:
    """Get learned timeout for a domain, or default."""
    params = get_domain_params(domain)
    if params and params.get("total_requests", 0) >= MIN_SAMPLES_FOR_TUNING:
        return params["recommended_timeout_ms"]
    return 30000


def get_recommended_retries(domain: str) -> int:
    """Get learned retry count for a domain, or default."""
    params = get_domain_params(domain)
    if params and params.get("total_requests", 0) >= MIN_SAMPLES_FOR_TUNING:
        return params["recommended_retries"]
    return 3


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def get_insights() -> Dict[str, Any]:
    """Generate a full learning insights report."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                return collect_insights(conn)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("get_insights failed: %s", exc)
        return {"error": str(exc)}


def get_degrading_scrapers() -> List[Dict[str, Any]]:
    """Find scrapers whose success rate is declining (early warning system)."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                scraper_rows = conn.execute(
                    "SELECT scraper FROM scraper_health WHERE total_requests >= ?",
                    (MIN_SAMPLES_FOR_TUNING,),
                ).fetchall()
                return collect_degradations(conn, scraper_rows)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("get_degrading_scrapers failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def prune_old_data(days: int = 30) -> None:
    """Remove request_log + feedback entries older than N days."""
    try:
        cutoff = time.time() - (days * 86400)
        with _db_lock:
            conn = _get_db()
            try:
                deleted = conn.execute(
                    "DELETE FROM request_log WHERE ts < ?", (cutoff,)
                ).rowcount
                conn.execute("DELETE FROM feedback WHERE ts < ?", (cutoff,))
                conn.commit()
                logger.info(
                    "Pruned %d old request_log entries (>%d days)", deleted, days
                )
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("prune_old_data failed: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


# ---------------------------------------------------------------------------
# Endpoint surface -- routes wired in the sibling endpoints module.
# ---------------------------------------------------------------------------

router = attach_endpoints(
    LearningHandlers(
        record_feedback=record_feedback,
        get_insights=get_insights,
        get_degrading_scrapers=get_degrading_scrapers,
        get_domain_params=get_domain_params,
        prune_old_data=prune_old_data,
    ),
    make_db_access(_db_lock, _get_db),
)
