"""Async/sync bridge utilities for CrawlRX.

Wraps a process-wide ``ThreadPoolExecutor`` so blocking calls (Playwright,
sync HTTP, BeautifulSoup parsing) never starve the FastAPI event loop.

Knobs:
  - ``CRAWLRX_WORKERS`` env var caps the pool size (default: 4).

Public surface:
  - ``run_sync``, ``run_sync_with_timeout`` -- bridge helpers.
  - ``timer_ms`` -- monotonic-ish elapsed-time helper.
"""
from __future__ import annotations

import asyncio
import atexit as _atexit
import functools
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

# ---------------------------------------------------------------------------
# THREAD POOL -- scales via CRAWLRX_WORKERS env var (default 4)
# ---------------------------------------------------------------------------
_MAX_WORKERS = int(os.getenv("CRAWLRX_WORKERS", "4"))
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="crawlrx")
_atexit.register(_executor.shutdown, wait=False)


async def run_sync(func: Callable, *args: object, **kwargs: object) -> object:
    """Run a blocking ``func`` in the shared thread pool."""
    loop = asyncio.get_running_loop()
    wrapped = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_executor, wrapped)


async def run_sync_with_timeout(
    func: Callable,
    *args: object,
    timeout_seconds: int = 60,
    **kwargs: object,
) -> object:
    """Run ``func`` in the pool with a hard wall-clock ``timeout_seconds``."""
    loop = asyncio.get_running_loop()
    wrapped = functools.partial(func, *args, **kwargs) if (args or kwargs) else func
    future = loop.run_in_executor(_executor, wrapped)
    return await asyncio.wait_for(future, timeout=timeout_seconds)


def timer_ms() -> float:
    """Return current wall-clock time in milliseconds."""
    return time.time() * 1000
