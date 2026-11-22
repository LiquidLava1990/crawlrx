"""Background intelligence loop + lifecycle for ``intelligence.py``."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.intelligence")

CYCLE_INTERVAL = 60
INITIAL_DELAY_SECONDS = 10


@dataclass
class IntelLoopState:
    """Mutable lifecycle state for the intelligence background loop."""

    task: Optional[asyncio.Task] = None
    running: bool = False
    last_prune_day: Optional[str] = None


@dataclass(frozen=True)
class CycleHooks:
    """Per-cycle work delegated to the host module."""

    detect_anomalies: Callable[[], List[Any]]
    probe_disabled: Callable[[], Awaitable[None]]
    pick_experiment_domain: Callable[[], Optional[str]]
    run_strategy_experiment: Callable[[str], Awaitable[Any]]
    daily_prune: Callable[[], None]


async def run_cycle(hooks: CycleHooks) -> None:
    """One cycle of the intelligence loop."""
    cycle_start = time.time()
    try:
        anomalies = hooks.detect_anomalies()
        await hooks.probe_disabled()
        exp_domain = hooks.pick_experiment_domain()
        if exp_domain:
            await hooks.run_strategy_experiment(exp_domain)
        hooks.daily_prune()
        elapsed = int((time.time() - cycle_start) * 1000)
        if anomalies:
            logger.info(
                "Intelligence cycle complete in %dms: %d anomalies",
                elapsed, len(anomalies),
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Intelligence cycle failed: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


async def _safe_run_cycle(hooks: CycleHooks) -> None:
    """Run one intelligence cycle, swallowing recoverable scrape failures."""
    try:
        await run_cycle(hooks)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Intelligence loop error: %s", exc)
        return None  # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; loop continues


async def loop_forever(state: IntelLoopState, hooks: CycleHooks) -> None:
    """Background loop that runs intelligence cycles."""
    logger.info("Intelligence loop started (cycle every %ds)", CYCLE_INTERVAL)
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while state.running:
        await _safe_run_cycle(hooks)
        await asyncio.sleep(CYCLE_INTERVAL)
    logger.info("Intelligence loop stopped")


def maybe_run_daily_prune(
    state: IntelLoopState,
    learning_prune: Callable[[int], None],
    intel_prune: Callable[[], None],
) -> None:
    """Run both prune flows once per calendar day."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.last_prune_day == today:
        return
    state.last_prune_day = today
    learning_prune(30)
    intel_prune()


@dataclass(frozen=True)
class StartConfig:
    """Bundle for :func:`start_loop` to keep param count small."""

    init_db: Callable[[], None]
    hooks: CycleHooks


def start_loop(state: IntelLoopState, config: StartConfig) -> None:
    """Start the background intelligence loop."""
    if state.running:
        return
    config.init_db()
    state.running = True
    try:
        state.task = asyncio.get_running_loop().create_task(
            loop_forever(state, config.hooks)
        )
    except RuntimeError:
        logger.warning(
            "[intelligence] start_intelligence() called outside event loop"
        )
        state.running = False
        return None  # explicit recovery: non-fatal: RuntimeError is recorded but does not abort the caller
    logger.info("Intelligence engine started")


def stop_loop(state: IntelLoopState) -> None:
    """Stop the background intelligence loop."""
    state.running = False
    if state.task:
        state.task.cancel()
        state.task = None
    logger.info("Intelligence engine stopped")
