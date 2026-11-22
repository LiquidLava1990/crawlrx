"""A/B experiment helpers for ``intelligence.py``.

Splits :func:`run_strategy_experiment` (82 LOC, cognitive 20) into pure
helpers + a thin orchestrator. Selection logic, request execution, winner
calculation, and persistence each get their own function.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.intelligence")


@dataclass(frozen=True)
class ExperimentContext:
    """Per-call DB context handed to experiment helpers."""

    lock: threading.Lock
    factory: Callable[[], sqlite3.Connection]


def _fetch_test_url(
    ctx: ExperimentContext,
    domain: str,
) -> Tuple[Optional[str], Dict[str, int]]:
    """Pull the most recent successful URL for the domain + per-strategy attempt counts."""
    with ctx.lock:
        conn = ctx.factory()
        try:
            url_row = conn.execute(
                """
                SELECT url FROM request_log
                WHERE domain = ? AND success = 1
                ORDER BY ts DESC LIMIT 1
                """,
                (domain,),
            ).fetchone()
            if not url_row:
                return None, {}
            existing_rows = conn.execute(
                "SELECT wait_strategy, attempts FROM strategy_scores WHERE domain = ?",
                (domain,),
            ).fetchall()
        finally:
            conn.close()
    return (
        url_row["url"],
        {r["wait_strategy"]: r["attempts"] for r in existing_rows},
    )


def _pick_variant_b(
    ab_strategies: List[str],
    existing_strategies: Dict[str, int],
) -> str:
    """Pick variant B: prefer untested strategies, then least-tested."""
    untested = [s for s in ab_strategies if s not in existing_strategies]
    if untested:
        return random.choice(untested)
    return min(existing_strategies, key=existing_strategies.get)


def _resolve_variant_a(
    domain: str,
    get_domain_params: Callable[[str], Optional[Dict[str, Any]]],
) -> str:
    """Variant A is the current best wait strategy (or the default)."""
    params = get_domain_params(domain)
    if params:
        return params.get("recommended_wait_strategy", "networkidle")
    return "networkidle"


def _resolve_distinct_pair(
    *,
    variant_a: str,
    variant_b: str,
    ab_strategies: List[str],
) -> Tuple[str, Optional[str]]:
    """Ensure variants A and B differ; otherwise pick a random alternative."""
    if variant_a != variant_b:
        return variant_a, variant_b
    alternatives = [s for s in ab_strategies if s != variant_a]
    if not alternatives:
        return variant_a, None
    return variant_a, random.choice(alternatives)


def _decide_winner(
    variant_a: str,
    variant_b: str,
    result_a: Dict[str, Any],
    result_b: Dict[str, Any],
) -> Optional[str]:
    """Faster successful variant wins; one-success-only takes the win."""
    if result_a["success"] and not result_b["success"]:
        return variant_a
    if result_b["success"] and not result_a["success"]:
        return variant_b
    if result_a["success"] and result_b["success"]:
        return variant_a if result_a["elapsed_ms"] <= result_b["elapsed_ms"] else variant_b
    return None


@dataclass(frozen=True)
class _ExperimentRow:
    """Row to persist into the ``experiments`` table."""

    domain: str
    variant_a: str
    variant_b: str
    result_a: Dict[str, Any]
    result_b: Dict[str, Any]
    winner: Optional[str]


def _persist_experiment(ctx: ExperimentContext, row: _ExperimentRow) -> None:
    """Write the experiment row + commit."""
    with ctx.lock:
        conn = ctx.factory()
        try:
            conn.execute(
                """
                INSERT INTO experiments
                (ts, domain, experiment_type, variant_a, variant_b,
                 result_a_success, result_a_elapsed_ms,
                 result_b_success, result_b_elapsed_ms,
                 winner, applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), row.domain, "wait_strategy",
                    row.variant_a, row.variant_b,
                    1 if row.result_a["success"] else 0, row.result_a["elapsed_ms"],
                    1 if row.result_b["success"] else 0, row.result_b["elapsed_ms"],
                    row.winner, 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _log_experiment_outcome(row: _ExperimentRow) -> None:
    """Single info log line per experiment."""
    if row.winner:
        logger.info(
            "Experiment result: '%s' wins on %s (A: %s/%dms, B: %s/%dms)",
            row.winner, row.domain,
            "OK" if row.result_a["success"] else "FAIL", row.result_a["elapsed_ms"],
            "OK" if row.result_b["success"] else "FAIL", row.result_b["elapsed_ms"],
        )
        return
    state = "failed" if not row.result_a["success"] else "tied"
    logger.info("Experiment inconclusive on %s (both %s)", row.domain, state)


@dataclass(frozen=True)
class ExperimentInputs:
    """Pre-flight inputs for :func:`run_experiment_loop`."""

    domain: str
    test_url: str
    variant_a: str
    variant_b: str


async def run_experiment_loop(
    ctx: ExperimentContext,
    inputs: ExperimentInputs,
    request_runner: Callable[[str, str], Any],
) -> Dict[str, Any]:
    """Run both variants, decide a winner, persist + log."""
    logger.info(
        "A/B experiment on %s: '%s' vs '%s'",
        inputs.domain, inputs.variant_a, inputs.variant_b,
    )
    result_a = await request_runner(inputs.test_url, inputs.variant_a)
    await asyncio.sleep(2)
    result_b = await request_runner(inputs.test_url, inputs.variant_b)
    winner = _decide_winner(
        inputs.variant_a, inputs.variant_b, result_a, result_b
    )
    row = _ExperimentRow(
        domain=inputs.domain,
        variant_a=inputs.variant_a,
        variant_b=inputs.variant_b,
        result_a=result_a,
        result_b=result_b,
        winner=winner,
    )
    _persist_experiment(ctx, row)
    _log_experiment_outcome(row)
    return {
        "domain": inputs.domain,
        "variant_a": inputs.variant_a,
        "variant_b": inputs.variant_b,
        "result_a": result_a,
        "result_b": result_b,
        "winner": winner,
    }


@dataclass(frozen=True)
class ExperimentSetup:
    """Bundle for :func:`prepare_experiment` to keep the host body flat."""

    ctx: ExperimentContext
    domain: str
    ab_strategies: List[str]
    get_domain_params: Callable[[str], Optional[Dict[str, Any]]]


def prepare_experiment(setup: ExperimentSetup) -> Optional[ExperimentInputs]:
    """All non-IO selection logic for an experiment, in one returnable bundle."""
    test_url, existing = _fetch_test_url(setup.ctx, setup.domain)
    if not test_url:
        return None
    variant_b = _pick_variant_b(setup.ab_strategies, existing)
    variant_a = _resolve_variant_a(setup.domain, setup.get_domain_params)
    variant_a, variant_b = _resolve_distinct_pair(
        variant_a=variant_a,
        variant_b=variant_b,
        ab_strategies=setup.ab_strategies,
    )
    if not variant_b:
        return None
    return ExperimentInputs(
        domain=setup.domain,
        test_url=test_url,
        variant_a=variant_a,
        variant_b=variant_b,
    )


async def safe_run_experiment(
    setup: ExperimentSetup,
    request_runner: Callable[[str, str], Any],
) -> Optional[Dict[str, Any]]:
    """Top-level: prepare + run + return None on narrow failure."""
    try:
        inputs = prepare_experiment(setup)
        if inputs is None:
            return None
        return await run_experiment_loop(setup.ctx, inputs, request_runner)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "run_strategy_experiment failed for %s: %s", setup.domain, exc
        )
        return None
