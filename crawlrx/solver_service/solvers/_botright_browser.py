"""Browser-pool primitives for the Botright solver.

Owns the lifecycle of the per-solver browser pool: lazy client creation,
slot acquire/release, stale-slot eviction, and graceful pool drain. The
orchestrator (``stealth_solver.py``) holds the shared mutable state
(``_browser_pool``, ``_browser_in_use``, ``_browser_last_used``) and
delegates here so the orchestrator stays at coordination level only.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Protocol, Tuple

from ._solver_helpers import BROAD_EXC as _BROAD_EXC

# Resolve `core.sentinel_errors` exactly the way the original solver did so
# emit() routes through the canonical Sentinel channel.
try:
    from core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:  # pragma: no cover - last-resort shim
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode = _E()  # type: ignore[assignment]  because sentinel ErrCode is unavailable; _E() shim returns ERR_UNKNOWN for any attr

    def emit(*a: object, **kw: object) -> None:  # noqa: D401, ANN001
        """No-op emit when Sentinel error bus is unavailable."""
        return None


try:
    from solver_base import SolverError  # noqa: E402
except ImportError:  # package layout (release tree)
    from ..solver_base import SolverError  # type: ignore[no-redef]  # noqa: E402


logger = logging.getLogger(__name__)


class _BotrightBrowser(Protocol):
    """Minimum surface needed from a single pooled Botright browser handle."""

    async def close(self) -> None: ...


class _BotrightClient(Protocol):
    """Minimum surface this module needs from the underlying Botright client."""

    async def new_browser(self) -> _BotrightBrowser: ...
    async def close(self) -> None: ...


class _PoolHost(Protocol):
    """Subset of ``StealthSolver`` state the browser helpers read/write.

    Declared as a Protocol so this module avoids a circular import on the
    concrete solver class while still type-checking field access.
    """

    max_sessions: int
    headless: bool
    _botright_client: Optional[_BotrightClient]
    _browser_pool: List[Any]
    _pool_lock: asyncio.Lock
    _pool_semaphore: asyncio.Semaphore
    _browser_in_use: "dict[int, bool]"
    _browser_last_used: "dict[int, float]"


async def get_botright_client(
    host: _PoolHost,
    botright_module: Optional[Any],
    botright_available: bool,
) -> _BotrightClient:
    """Lazy-init the stealth client: own engine by default, botright by opt-in.

    ``CRAWLRX_STEALTH_ENGINE=own`` (default) builds the in-house engine --
    no botright dependency. ``=botright`` keeps the legacy lane available
    while it exists (strangler fig), with the same historical error when the
    pip package is missing.
    """
    if host._botright_client is not None:
        return host._botright_client
    import os

    lane = os.getenv("CRAWLRX_STEALTH_ENGINE", "own").strip().lower()
    if lane != "botright":
        try:
            from .._stealth_engine import StealthEngine
        except ImportError:  # flat layout (master tree)
            from _stealth_engine import StealthEngine

        host._botright_client = StealthEngine(headless=host.headless)
        logger.debug("Own stealth engine initialized (lane=own)")
        return host._botright_client
    if not botright_available or botright_module is None:
        raise SolverError(
            "ERROR_BOTRIGHT_NOT_INSTALLED",
            "Botright lane requested (CRAWLRX_STEALTH_ENGINE=botright) but the "
            "pip botright package is not installed. "
            "Run: pip install botright && playwright install "
            "(or unset CRAWLRX_STEALTH_ENGINE to use the own engine)",
        )
    host._botright_client = await botright_module.Botright(headless=host.headless)
    logger.debug("Botright client initialized (lane=botright)")
    return host._botright_client


def _take_free_slot(host: _PoolHost) -> Optional[Tuple[Any, int]]:
    """Return the first un-leased pool slot or ``None`` if the pool is busy."""
    for idx, browser in enumerate(host._browser_pool):
        if browser is None:
            continue
        if host._browser_in_use.get(idx, False):
            continue
        host._browser_in_use[idx] = True
        host._browser_last_used[idx] = time.time()
        logger.debug("Reusing browser from pool (index %s)", idx)
        return browser, idx
    return None


async def _grow_pool(
    host: _PoolHost,
    botright_module: Optional[Any],
    botright_available: bool,
) -> Optional[Tuple[Any, int]]:
    """Add one new browser to the pool if it has not hit ``max_sessions``."""
    if sum(browser is not None for browser in host._browser_pool) >= host.max_sessions:
        return None
    client = await get_botright_client(host, botright_module, botright_available)
    browser = await client.new_browser()
    try:
        idx = host._browser_pool.index(None)
    except ValueError:
        idx = len(host._browser_pool)
        host._browser_pool.append(browser)
    else:
        host._browser_pool[idx] = browser
    host._browser_in_use[idx] = True
    host._browser_last_used[idx] = time.time()
    logger.info(
        "Created new browser (index %s, pool size: %s)",
        idx, len(host._browser_pool),
    )
    return browser, idx


async def acquire_browser(
    host: _PoolHost,
    botright_module: Optional[Any],
    botright_available: bool,
) -> Tuple[Any, int]:
    """Acquire a browser from the pool or create / wait for one."""
    await host._pool_semaphore.acquire()
    try:
        async with host._pool_lock:
            slot = _take_free_slot(host)
            if slot is not None:
                return slot
            grown = await _grow_pool(host, botright_module, botright_available)
            if grown is not None:
                return grown
        raise SolverError("ERROR_POOL_EXHAUSTED", "Failed to acquire browser from pool")
    except BaseException:
        host._pool_semaphore.release()
        raise


async def release_browser(host: _PoolHost, idx: int) -> None:
    """Release a browser back to the pool."""
    async with host._pool_lock:
        if host._browser_in_use.get(idx, False):
            host._browser_in_use[idx] = False
            host._browser_last_used[idx] = time.time()
            host._pool_semaphore.release()
            logger.debug("Released browser (index %s)", idx)


def _stale_indices(host: _PoolHost, max_age: int) -> List[int]:
    """Pure: return pool indices whose browsers are idle past ``max_age``."""
    now = time.time()
    stale: List[int] = []
    for idx in range(len(host._browser_pool) - 1, -1, -1):
        if host._browser_pool[idx] is None:
            continue
        last_used = host._browser_last_used.get(idx, 0)
        in_use = host._browser_in_use.get(idx, False)
        if not in_use and (now - last_used) > max_age:
            stale.append(idx)
    return stale


async def _evict_stale(host: _PoolHost, idx: int, max_age: int) -> None:
    """Close + remove one browser slot, swallowing recoverable shutdown errors."""
    try:
        async with host._pool_lock:
            if (idx >= len(host._browser_pool) or host._browser_pool[idx] is None
                    or host._browser_in_use.get(idx, False)
                    or time.time() - host._browser_last_used.get(idx, 0) <= max_age):
                return
            browser = host._browser_pool[idx]
            await browser.close()
            host._browser_pool[idx] = None
            host._browser_in_use.pop(idx, None)
            host._browser_last_used.pop(idx, None)
        logger.info("Closed stale browser (index %s)", idx)
    except asyncio.CancelledError:
        raise
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Error closing stale browser %s: %s", idx, e)
        return  # explicit recovery: stale eviction is best-effort; non-fatal


async def cleanup_stale(host: _PoolHost, max_age: int) -> None:
    """Clean up browsers that haven't been used recently."""
    async with host._pool_lock:
        idxs = _stale_indices(host, max_age)
    for idx in idxs:
        await _evict_stale(host, idx, max_age)


async def _close_pages_in_context(pages: list) -> None:
    """Close every page in one browser context, swallowing per-page errors."""
    for page in pages:
        try:
            await page.close()
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _close_pages_for_browser(idx: int, browser: Any) -> None:
    """Close every page belonging to one browser entry in the pool."""
    try:
        contexts = browser.contexts if hasattr(browser, "contexts") else []
        for context in contexts:
            pages = context.pages if hasattr(context, "pages") else []
            await _close_pages_in_context(pages)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.debug("Error closing pages for browser %s: %s", idx, e)


async def close_all_pages(host: _PoolHost) -> None:
    """Close every page across every browser context in the pool."""
    async with host._pool_lock:
        for idx, browser in enumerate(host._browser_pool):
            if browser is not None:
                await _close_pages_for_browser(idx, browser)


async def _safe_close_pool_browser(idx: int, browser: Any) -> None:
    """Close one pooled browser, swallowing recoverable shutdown errors."""
    try:
        await browser.close()
        logger.debug("Closed browser %s", idx)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Error closing browser %s: %s", idx, e)
        return  # explicit recovery: pool shutdown is best-effort; non-fatal


async def close_all_browsers(host: _PoolHost) -> None:
    """Close every browser instance and clear the pool bookkeeping."""
    async with host._pool_lock:
        for idx, browser in enumerate(host._browser_pool):
            if browser is not None:
                await _safe_close_pool_browser(idx, browser)
        host._browser_pool.clear()
        host._browser_in_use.clear()
        host._browser_last_used.clear()


async def close_botright_client(host: _PoolHost) -> None:
    """Close the underlying Botright client if one was created."""
    if not host._botright_client:
        return
    try:
        await host._botright_client.close()
        host._botright_client = None
        logger.debug("Botright client closed")
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Error closing Botright client: %s", e)
        return  # explicit recovery: client shutdown is best-effort; non-fatal


__all__ = [
    "acquire_browser",
    "release_browser",
    "cleanup_stale",
    "close_all_pages",
    "close_all_browsers",
    "close_botright_client",
    "get_botright_client",
]
