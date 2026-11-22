"""Content-quality + transfer-learning helpers for ``intelligence.py``."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.intelligence")

MIN_USEFUL_CONTENT_LENGTH = 200

CAPTCHA_KEYWORDS: List[str] = [
    "captcha", "challenge", "verify you are human", "are you a robot",
    "please complete the security check", "cf-browser-verification",
    "access denied", "blocked", "unusual traffic", "automated access",
    "enable javascript", "enable cookies", "cloudflare",
    "just a moment", "checking your browser", "security check",
    "pardon our interruption", "please verify",
]
EMPTY_PAGE_KEYWORDS: List[str] = [
    "page not found", "404", "error occurred", "something went wrong",
    "this page isn't available", "no longer available", "moved permanently",
    "under construction", "coming soon",
]


# ---------------------------------------------------------------------------
# Content quality
# ---------------------------------------------------------------------------

@dataclass
class _Assessment:
    """Mutable assessment record built up across the quality checks."""

    quality_score: float = 1.0
    is_captcha: bool = False
    is_empty: bool = False
    is_boilerplate: bool = False
    content_length: int = 0
    useful_content_length: int = 0

    def to_payload(self) -> Dict[str, Any]:
        return {
            "quality_score": self.quality_score,
            "is_captcha": self.is_captcha,
            "is_empty": self.is_empty,
            "is_boilerplate": self.is_boilerplate,
            "content_length": self.content_length,
            "useful_content_length": self.useful_content_length,
            "issues": list(self.issues),
        }

    issues: List[str] = None  # type: ignore[assignment] because dataclass placeholder; populated by _new_assessment factory immediately after construction


def _new_assessment() -> _Assessment:
    record = _Assessment()
    record.issues = []
    return record


def _strip_boilerplate(content: str) -> str:
    """Remove common boilerplate phrases + collapse whitespace."""
    if not content:
        return ""
    text = re.sub(
        r"(cookie|privacy|terms|copyright|©|all rights reserved|"
        r"subscribe|newsletter|sign up|log in|sign in).*?\n",
        "",
        content,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip()


def _score_by_useful_length(useful_len: int) -> float:
    """Map useful-content-length to a 0..1 quality score."""
    if useful_len > 2000:
        return 0.9
    if useful_len > 500:
        return 0.7
    if useful_len > MIN_USEFUL_CONTENT_LENGTH:
        return 0.5
    return 0.3


def _check_captcha(record: _Assessment, content_lower: str) -> bool:
    """Returns True if captcha pattern fired and short-circuited."""
    hits = sum(1 for kw in CAPTCHA_KEYWORDS if kw in content_lower)
    if hits < 2:
        return False
    record.quality_score = 0.0
    record.is_captcha = True
    record.issues.append("captcha_detected")
    return True


def _check_empty(record: _Assessment, content_lower: str, content_len: int) -> bool:
    """Returns True if error/empty-page pattern fired."""
    hits = sum(1 for kw in EMPTY_PAGE_KEYWORDS if kw in content_lower)
    if hits < 2 or content_len >= 1000:
        return False
    record.quality_score = 0.1
    record.is_empty = True
    record.issues.append("error_page")
    return True


def _apply_boilerplate_score(
    record: _Assessment,
    useful_len: int,
    raw_html_len: int,
) -> None:
    """Set the quality score based on useful content vs raw HTML ratio."""
    if useful_len < MIN_USEFUL_CONTENT_LENGTH and raw_html_len > 5000:
        record.quality_score = 0.2
        record.is_boilerplate = True
        record.issues.append("mostly_boilerplate")
        return
    if raw_html_len <= 0:
        return
    ratio = useful_len / max(raw_html_len, 1)
    if ratio < 0.05 and raw_html_len > 10000:
        record.quality_score = 0.3
        record.is_boilerplate = True
        record.issues.append("low_content_ratio")
        return
    record.quality_score = _score_by_useful_length(useful_len)


@dataclass(frozen=True)
class QualityInputs:
    """Inputs for :func:`assess_content`."""

    html: Optional[str]
    text: Optional[str]
    markdown: Optional[str]
    url: str


def assess_content(inputs: QualityInputs) -> Dict[str, Any]:
    """Heuristic quality assessment of crawled content."""
    record = _new_assessment()
    content = inputs.text or inputs.markdown or ""
    raw_html = inputs.html or ""
    if not content and not raw_html:
        record.quality_score = 0.0
        record.is_empty = True
        record.issues.append("no_content")
        return record.to_payload()
    record.content_length = len(raw_html)
    content_lower = content.lower() if content else raw_html.lower()
    if _check_captcha(record, content_lower):
        return record.to_payload()
    if _check_empty(record, content_lower, len(content)):
        return record.to_payload()
    useful = _strip_boilerplate(content)
    record.useful_content_length = len(useful)
    _apply_boilerplate_score(record, len(useful), len(raw_html))
    return record.to_payload()


# ---------------------------------------------------------------------------
# Transfer learning -- reuses an :class:`AssessmentSink` (DB writer) for
# clean separation. The intelligence host owns the connection factory.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransferContext:
    """DB context for transfer_domain_knowledge."""

    lock: threading.Lock
    factory: Callable[[], sqlite3.Connection]
    min_samples: int


def _avg_transfer(
    conn: sqlite3.Connection,
    similar_domains: List[str],
    min_samples: int,
) -> Optional[Dict[str, Any]]:
    """Average learned params across similar-domain rows, or None if empty."""
    like_conditions = " OR ".join("domain LIKE ?" for _ in similar_domains)
    like_params = [f"%{d}%" for d in similar_domains]
    rows = conn.execute(
        f"""
        SELECT recommended_timeout_ms, recommended_retries,
               recommended_wait_strategy, avg_success_rate,
               avg_response_ms, p95_response_ms
        FROM domain_params
        WHERE ({like_conditions}) AND total_requests >= ?
        """,
        [*like_params, min_samples],
    ).fetchall()
    if not rows:
        return None
    avg_timeout = int(sum(r["recommended_timeout_ms"] for r in rows) / len(rows))
    avg_retries = round(sum(r["recommended_retries"] for r in rows) / len(rows))
    strategies = [r["recommended_wait_strategy"] for r in rows]
    best_strategy = max(set(strategies), key=strategies.count)
    return {
        "row_count": len(rows),
        "recommended_timeout_ms": avg_timeout,
        "recommended_retries": avg_retries,
        "recommended_wait_strategy": best_strategy,
    }


@dataclass(frozen=True)
class TransferRequest:
    """Bundled inputs for :func:`transfer_knowledge`."""

    target_domain: str
    similar_domains: List[str]
    category: str


def transfer_knowledge(
    ctx: TransferContext,
    request: TransferRequest,
) -> Optional[Dict[str, Any]]:
    """Run the SQL fan-out + assemble the transfer payload."""
    if not request.similar_domains:
        return None
    try:
        with ctx.lock:
            conn = ctx.factory()
            try:
                stats = _avg_transfer(conn, request.similar_domains, ctx.min_samples)
            finally:
                conn.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("transfer_knowledge failed: %s", exc)
        return None
    if not stats:
        return None
    transferred = {
        "source": f"transfer_from_{request.category}",
        "source_domains": stats["row_count"],
        "recommended_timeout_ms": stats["recommended_timeout_ms"],
        "recommended_retries": stats["recommended_retries"],
        "recommended_wait_strategy": stats["recommended_wait_strategy"],
        "confidence": min(0.8, stats["row_count"] / 5),
    }
    logger.info(
        "Transferred knowledge from %s category (%d domains) to %s",
        request.category, stats["row_count"], request.target_domain,
    )
    return transferred


# ---------------------------------------------------------------------------
# Persistence helpers (assessment, stealth outcome) -- pure functions.
# ---------------------------------------------------------------------------

def insert_assessment_row(
    conn: sqlite3.Connection,
    domain: str,
    url: str,
    assessment: Dict[str, Any],
) -> None:
    """Append one row to ``content_assessments``."""
    conn.execute(
        """
        INSERT INTO content_assessments
        (ts, domain, url, content_length, useful_content_length,
         quality_score, is_captcha, is_empty, is_boilerplate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(), domain, url[:500],
            assessment.get("content_length", 0),
            assessment.get("useful_content_length", 0),
            assessment.get("quality_score", 0),
            1 if assessment.get("is_captcha") else 0,
            1 if assessment.get("is_empty") else 0,
            1 if assessment.get("is_boilerplate") else 0,
        ),
    )


# ---------------------------------------------------------------------------
# Stealth-score persistence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StealthOutcome:
    """Bundle for :func:`persist_stealth_outcome`."""

    domain: str
    profile_hash: str
    success: bool
    was_bot_blocked: bool


def _existing_stealth_score(
    row: sqlite3.Row,
    outcome: StealthOutcome,
) -> tuple[int, int, int, float]:
    """Recompute attempts/successes/blocks/score for an existing row."""
    attempts = row["attempts"] + 1
    successes = row["successes"] + (1 if outcome.success else 0)
    blocks = row["bot_blocks"] + (1 if outcome.was_bot_blocked else 0)
    sr = successes / attempts
    block_penalty = blocks / attempts
    score = max(0.0, min(1.0, sr * 0.8 - block_penalty * 0.5))
    return attempts, successes, blocks, score


def persist_stealth_outcome(
    conn: sqlite3.Connection,
    outcome: StealthOutcome,
) -> None:
    """Insert/update one stealth_scores row."""
    row = conn.execute(
        "SELECT * FROM stealth_scores WHERE domain = ? AND profile_hash = ?",
        (outcome.domain, outcome.profile_hash),
    ).fetchone()
    now = time.time()
    if row:
        attempts, successes, blocks, score = _existing_stealth_score(row, outcome)
        conn.execute(
            """
            UPDATE stealth_scores SET
                attempts = ?, successes = ?, bot_blocks = ?,
                score = ?, updated_at = ?
            WHERE domain = ? AND profile_hash = ?
            """,
            (
                attempts, successes, blocks,
                round(score, 4), now,
                outcome.domain, outcome.profile_hash,
            ),
        )
    else:
        score = 0.8 if outcome.success else 0.2
        conn.execute(
            """
            INSERT INTO stealth_scores
            (domain, profile_hash, attempts, successes, bot_blocks, score, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                outcome.domain, outcome.profile_hash,
                1 if outcome.success else 0,
                1 if outcome.was_bot_blocked else 0,
                round(score, 4), now,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Intel-table maintenance
# ---------------------------------------------------------------------------

def prune_intel_tables(
    conn: sqlite3.Connection,
    cutoff_seconds: int = 30 * 86400,
) -> None:
    """Drop intel-table rows older than ``cutoff_seconds`` (hardcoded DELETEs)."""
    cutoff = time.time() - cutoff_seconds
    conn.execute("DELETE FROM experiments WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM anomalies WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM probe_log WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM content_assessments WHERE ts < ?", (cutoff,))
    conn.commit()
