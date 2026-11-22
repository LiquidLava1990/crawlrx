"""
intelligence.py -- CrawlRX Active Intelligence (Layer 2)
=========================================================
Layer 1 (learning.py) observes and tunes. This layer ACTS.

Splits across sibling modules:
- ``_intelligence_stealth``     -- best_stealth_profile resolver
- ``_intelligence_anomaly``     -- detect_anomalies fan-out helpers
- ``_intelligence_probe``       -- probe_disabled_scrapers async helpers
- ``_intelligence_experiment``  -- run_strategy_experiment helpers
- ``_intelligence_quality``     -- content quality + transfer + stealth + prune
- ``_intelligence_loop``        -- background loop + lifecycle
- ``_intelligence_endpoints``   -- FastAPI surface
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ._intelligence_anomaly import (
    make_anomaly_window,
    scan_domain_anomalies,
    scan_scraper_anomalies,
)
from ._intelligence_endpoints import (
    IntelHandlers,
    attach_endpoints,
    make_intel_db,
)
from ._intelligence_experiment import (
    ExperimentContext,
    ExperimentSetup,
    safe_run_experiment,
)
from ._intelligence_loop import (
    CYCLE_INTERVAL,
    CycleHooks,
    IntelLoopState,
    StartConfig,
    maybe_run_daily_prune,
    start_loop,
    stop_loop,
)
from ._intelligence_probe import (
    ProbeContext,
    probe_all_disabled,
)
from ._intelligence_quality import (
    QualityInputs,
    StealthOutcome,
    TransferContext,
    TransferRequest,
    assess_content,
    insert_assessment_row,
    persist_stealth_outcome,
    prune_intel_tables,
    transfer_knowledge,
)
from ._intelligence_stealth import best_stealth_profile
from ._learning_recording import MIN_SAMPLES_FOR_TUNING
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from ._learning_helpers import register_schema
from .learning import (
    _db_lock,
    _extract_domain,
    _get_db,
    get_domain_params,
)

logger = logging.getLogger("crawlrx.intelligence")

# Re-exported tuning constants kept here for backward compatibility.
EXPERIMENT_BATCH_SIZE = 3
EXPERIMENT_COOLDOWN = 1800
AB_STRATEGIES: List[str] = [
    "networkidle", "domcontentloaded", "settle_1500", "settle_3000", "load",
]
ANOMALY_WINDOW_HOURS = 2
ANOMALY_BASELINE_HOURS = 24
ANOMALY_DROP_THRESHOLD = 0.25
PROBE_INTERVAL_SECONDS = 300
PROBE_CONSECUTIVE_SUCCESSES = 3

SIMILAR_DOMAIN_CATEGORIES: Dict[str, List[str]] = {
    "ecommerce": [
        "amazon", "ebay", "etsy", "walmart", "target", "bestbuy",
        "costco", "homedepot", "shopify", "woocommerce",
    ],
    "real_estate": ["zillow", "realtor", "redfin", "trulia", "apartments"],
    "jobs": ["indeed", "linkedin", "glassdoor", "angellist"],
    "travel": ["airbnb", "booking", "expedia", "kayak", "tripadvisor"],
    "social": [
        "twitter", "instagram", "facebook", "tiktok", "youtube",
        "reddit", "pinterest",
    ],
    "search": ["google", "bing", "duckduckgo"],
}

_CYCLE_INTERVAL = CYCLE_INTERVAL
_loop_state = IntelLoopState()


# ---------------------------------------------------------------------------
# DB extensions (intelligence-specific tables)
# ---------------------------------------------------------------------------

_INTEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT NOT NULL,
    experiment_type TEXT NOT NULL,
    variant_a TEXT NOT NULL,
    variant_b TEXT NOT NULL,
    result_a_success INTEGER,
    result_a_elapsed_ms INTEGER,
    result_b_success INTEGER,
    result_b_elapsed_ms INTEGER,
    winner TEXT,
    applied INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT,
    scraper TEXT,
    anomaly_type TEXT NOT NULL,
    baseline_rate REAL,
    current_rate REAL,
    drop_pct REAL,
    resolved INTEGER DEFAULT 0,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS stealth_scores (
    domain TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    bot_blocks INTEGER DEFAULT 0,
    score REAL DEFAULT 0.5,
    updated_at REAL,
    PRIMARY KEY (domain, profile_hash)
);

CREATE TABLE IF NOT EXISTS probe_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scraper TEXT NOT NULL,
    success INTEGER NOT NULL,
    elapsed_ms INTEGER,
    error TEXT,
    consecutive_successes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS content_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    content_length INTEGER,
    useful_content_length INTEGER,
    quality_score REAL,
    is_captcha INTEGER DEFAULT 0,
    is_empty INTEGER DEFAULT 0,
    is_boilerplate INTEGER DEFAULT 0,
    auto_feedback_sent INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_experiments_domain ON experiments(domain, ts);
CREATE INDEX IF NOT EXISTS idx_anomalies_ts ON anomalies(ts);
CREATE INDEX IF NOT EXISTS idx_probe_scraper ON probe_log(scraper, ts);
CREATE INDEX IF NOT EXISTS idx_content_domain ON content_assessments(domain, ts);
"""


# Lazily opened learning DBs (CLI, library use) create these tables too.
register_schema(_INTEL_SCHEMA)


def init_intelligence_db() -> None:
    """Create layer-2 tables if they don't exist."""
    conn = _get_db()
    try:
        conn.executescript(_INTEL_SCHEMA)
        conn.commit()
        logger.info("Intelligence DB tables initialized")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Content self-assessment + stealth tracking (thin DB orchestrators)
# ---------------------------------------------------------------------------

def assess_content_quality(
    html: Optional[str],
    text: Optional[str],
    markdown: Optional[str],
    url: str,
) -> Dict[str, Any]:
    """Auto-assess whether extracted content is useful or garbage."""
    domain = _extract_domain(url)
    payload = assess_content(QualityInputs(html=html, text=text, markdown=markdown, url=url))
    _record_assessment(domain, url, payload)
    return payload


def _record_assessment(domain: str, url: str, assessment: Dict[str, Any]) -> None:
    """Persist content assessment to DB."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                insert_assessment_row(conn, domain, url, assessment)
                conn.commit()
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("_record_assessment failed: %s", exc)


def record_stealth_outcome(
    domain: str,
    profile_hash: str,
    success: bool,
    was_bot_blocked: bool = False,
) -> None:
    """Track which stealth fingerprint profiles work per domain."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                persist_stealth_outcome(
                    conn,
                    StealthOutcome(
                        domain=domain,
                        profile_hash=profile_hash,
                        success=success,
                        was_bot_blocked=was_bot_blocked,
                    ),
                )
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("record_stealth_outcome failed: %s", exc)


def get_best_stealth_profile(domain: str) -> Optional[str]:
    """Get the best-performing stealth profile hash for a domain."""
    similar = SIMILAR_DOMAIN_CATEGORIES.get(_get_domain_category(domain) or "", [])
    try:
        with _db_lock:
            conn = _get_db()
            try:
                return best_stealth_profile(conn, domain, similar)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None


# ---------------------------------------------------------------------------
# Cross-domain transfer learning
# ---------------------------------------------------------------------------

def _get_domain_category(domain: str) -> Optional[str]:
    """Find which category a domain belongs to."""
    base = domain.replace(".com", "").replace(".co", "").replace(".org", "")
    for category, domains in SIMILAR_DOMAIN_CATEGORIES.items():
        for d in domains:
            if d in base:
                return category
    return None


def transfer_domain_knowledge(target_domain: str) -> Optional[Dict[str, Any]]:
    """Transfer learned params from similar domains to ``target_domain``."""
    params = get_domain_params(target_domain)
    if params and params.get("total_requests", 0) >= MIN_SAMPLES_FOR_TUNING:
        return None
    category = _get_domain_category(target_domain)
    if not category:
        return None
    similar = SIMILAR_DOMAIN_CATEGORIES.get(category, [])
    return transfer_knowledge(
        TransferContext(
            lock=_db_lock,
            factory=_get_db,
            min_samples=MIN_SAMPLES_FOR_TUNING,
        ),
        TransferRequest(
            target_domain=target_domain,
            similar_domains=similar,
            category=category,
        ),
    )


# ---------------------------------------------------------------------------
# Anomaly detection / probe / experiment orchestrators
# ---------------------------------------------------------------------------

def detect_anomalies() -> List[Dict[str, Any]]:
    """Compare recent success rates against baselines, fire anomalies."""
    anomalies: List[Dict[str, Any]] = []
    try:
        window = make_anomaly_window(ANOMALY_WINDOW_HOURS, ANOMALY_BASELINE_HOURS)
        with _db_lock:
            conn = _get_db()
            try:
                anomalies.extend(scan_domain_anomalies(conn, window))
                anomalies.extend(scan_scraper_anomalies(conn, window))
                conn.commit()
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("detect_anomalies failed: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    _log_anomalies(anomalies)
    return anomalies


def _log_anomalies(anomalies: List[Dict[str, Any]]) -> None:
    """Single-log warning per detected anomaly."""
    if not anomalies:
        return
    logger.warning("ANOMALIES DETECTED: %d issues found", len(anomalies))
    for a in anomalies:
        target = a.get("domain") or a.get("scraper")
        logger.warning(
            "  [%s] %s: %.1f%% -> %.1f%% (down %.1f%%)",
            a["anomaly_type"], target,
            a["baseline_rate"], a["current_rate"], a["drop_pct"],
        )


async def probe_disabled_scrapers() -> None:
    """Probe every disabled scraper; re-enable on consecutive successes."""
    await probe_all_disabled(ProbeContext(lock=_db_lock, factory=_get_db))


def pick_experiment_domain() -> Optional[str]:
    """Pick a domain to experiment on (low success + cooldown elapsed)."""
    try:
        cooldown_cutoff = time.time() - EXPERIMENT_COOLDOWN
        with _db_lock:
            conn = _get_db()
            try:
                row = conn.execute(
                    """
                    SELECT dp.domain, dp.avg_success_rate, dp.total_requests
                    FROM domain_params dp
                    WHERE dp.total_requests >= ?
                      AND dp.avg_success_rate < 0.9
                      AND dp.domain NOT IN (
                          SELECT domain FROM experiments WHERE ts > ?
                      )
                    ORDER BY dp.avg_success_rate ASC
                    LIMIT 1
                    """,
                    (MIN_SAMPLES_FOR_TUNING, cooldown_cutoff),
                ).fetchone()
                return row["domain"] if row else None
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None


async def run_strategy_experiment(domain: str) -> Optional[Dict[str, Any]]:
    """A/B test wait strategies on a domain via the experiment helper."""
    return await safe_run_experiment(
        ExperimentSetup(
            ctx=ExperimentContext(lock=_db_lock, factory=_get_db),
            domain=domain,
            ab_strategies=AB_STRATEGIES,
            get_domain_params=get_domain_params,
        ),
        _run_experiment_request,
    )


async def _run_experiment_request(url: str, wait_strategy: str) -> Dict[str, Any]:
    """Execute a single experiment request variant."""
    try:
        import httpx
        wait_for = wait_strategy if wait_strategy in (
            "networkidle", "domcontentloaded", "load"
        ) else None
        payload: Dict[str, Any] = {
            "url": url, "timeout": 15000,
            "extract_text": True, "extract_markdown": False, "cache": False,
        }
        if wait_for:
            payload["wait_for"] = wait_for
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("http://localhost:7337/crawl", json=payload)
            result = resp.json()
            return {
                "success": result.get("success", False),
                "elapsed_ms": result.get("elapsed_ms", 0),
                "content_length": len(result.get("text", "") or ""),
            }
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        return {
            "success": False, "elapsed_ms": 0,
            "content_length": 0, "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Background loop wrappers (state owned in this module, logic in _intelligence_loop)
# ---------------------------------------------------------------------------

def _daily_prune() -> None:
    """Run both prune flows once per calendar day."""
    from .learning import prune_old_data
    maybe_run_daily_prune(_loop_state, prune_old_data, _safe_prune_intel)


def _safe_prune_intel() -> None:
    """Run intel-table prune with narrow exception handling."""
    try:
        with _db_lock:
            conn = _get_db()
            try:
                prune_intel_tables(conn)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Daily prune failed: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


def _build_cycle_hooks() -> CycleHooks:
    """Snapshot the host-side callables used by one intelligence cycle."""
    return CycleHooks(
        detect_anomalies=detect_anomalies,
        probe_disabled=probe_disabled_scrapers,
        pick_experiment_domain=pick_experiment_domain,
        run_strategy_experiment=run_strategy_experiment,
        daily_prune=_daily_prune,
    )


def start_intelligence() -> None:
    """Start the background intelligence loop."""
    start_loop(
        _loop_state,
        StartConfig(init_db=init_intelligence_db, hooks=_build_cycle_hooks()),
    )


def stop_intelligence() -> None:
    """Stop the background intelligence loop."""
    stop_loop(_loop_state)


def _is_running() -> bool:
    return _loop_state.running


# ---------------------------------------------------------------------------
# Endpoint surface
# ---------------------------------------------------------------------------

router = attach_endpoints(
    IntelHandlers(
        is_running=_is_running,
        run_strategy_experiment=run_strategy_experiment,
        transfer_domain_knowledge=transfer_domain_knowledge,
        get_best_stealth_profile=get_best_stealth_profile,
    ),
    make_intel_db(_db_lock, _get_db, _CYCLE_INTERVAL),
)
