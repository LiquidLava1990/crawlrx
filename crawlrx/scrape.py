"""scrape.py -- /scrape router (thin orchestrator).

The full scrape pipeline lives in three sibling modules:
- ``_scrape_pre_gates``: URL safety, robots, breaker, quality
- ``_scrape_attempt``: Playwright per-attempt flow + retry loop
- ``_smart_fetch_runner``: HTTP-only smart_fetch tier + html-chunk extractor

This file owns:
- the FastAPI router + ``scrape()`` request handler
- the wall-clock guard (``_execute_scrape_with_timeout``)
- back-compat re-exports so ``crawlrx.batch`` and tests keep importing
  ``_do_scrape``, ``ScrapeAttemptParams``, ``close_attempt``, etc. from
  ``crawlrx.scrape``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter

from .cache import get_cache
from .config import (
    CRAWLRX_IGNORE_ROBOTS,  # noqa: F401  -- pinned by public API snapshot
    USER_AGENTS,  # noqa: F401  -- pinned by public API snapshot
)
from .learning import record_outcome
from .models import ScrapeItem, ScrapeRequest, ScrapeResponse
from .scrape_site_health import record_scrape_site_outcome
from ._scrape_attempt import (
    ScrapeAttemptParams,
    _do_scrape,
    _do_scrape_attempt,
    _make_scrape_context,
    _try_one_scrape_attempt,
    close_attempt,
)
from ._scrape_pre_gates import (
    _check_scrape_robots,
    _maybe_override_scrape_quality,
    _record_scrape_outcome,
    _resolve_pre_scrape_gates,
    _resolve_scrape_breaker,
    _resolve_scrape_breaker_or_open,
    _scrape_blocked_url_response,
    _scrape_circuit_open_response,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
    PARSE_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
    PLAYWRIGHT_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
)
from ._smart_fetch_runner import (
    _scrape_via_smart_fetch,
    _smart_fetch_eligible,
)
from .captcha_handoff import handoff_wait_seconds

logger = logging.getLogger("crawlrx.scrape")
router = APIRouter(prefix="", tags=["scrape"])


async def _execute_scrape_with_timeout(
    request: ScrapeRequest, timeout: int, scroll: bool, wall_timeout: int, breaker: object,
) -> tuple[Optional[dict], Optional[ScrapeResponse]]:
    """Run _do_scrape under wall-clock guard; return (raw, abort_response)."""
    try:
        raw = await asyncio.wait_for(
            _do_scrape(
                request.url, request.selector, request.selectors,
                request.extract or "all", request.wait_for, timeout,
                request.javascript, request.user_agent, request.headers,
                scroll, request.click_selector, request.agent,
                request.structured_data, request.stealth,
                request.js_inject, request.js_inject_timeout,
                request.wait_for_text, request.wait_for_gone,
                request.wait_for_timeout_extra,
                request.screenshot_selector, request.shadow_selector,
            ),
            timeout=wall_timeout,
        )
        return raw, None
    except asyncio.TimeoutError:
        breaker.record_failure()
        logger.error("Wall-clock timeout (%ds) exceeded for scrape: %s", wall_timeout, request.url)
        return None, ScrapeResponse(
            success=False, url=request.url,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
            error_type="timeout", elapsed_ms=wall_timeout * 1000,
        )


@router.post("/scrape", response_model=ScrapeResponse, summary="Targeted element extraction")
async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    """Render a page and extract targeted content (selectors/selector/whole)."""
    cache = get_cache()
    cache_key = (
        cache.make_key("/scrape", request.model_dump()) if request.cache else None
    )
    early = await _resolve_pre_scrape_gates(request, cache, cache_key)
    if early is not None:
        return early
    timeout = request.timeout if request.timeout >= 1000 else request.timeout * 1000
    scroll = request.scroll_to_bottom or request.scroll
    breaker_block, breaker = _resolve_scrape_breaker_or_open(request.url)
    if breaker_block is not None:
        return breaker_block
    wall_timeout = timeout // 1000 + 10 + handoff_wait_seconds()
    raw, abort = await _execute_scrape_with_timeout(
        request, timeout, scroll, wall_timeout, breaker,
    )
    if abort is not None:
        record_scrape_site_outcome(request.url, abort.model_dump())
        return abort
    if raw is None:
        raise RuntimeError(
            "_execute_scrape_with_timeout returned no abort AND no raw result; "
            "contract violation -- exactly one must be non-None"
        )
    _record_scrape_outcome(raw, breaker)
    if request.cache and raw.get("success") and cache_key:
        cache.set(cache_key, raw, ttl_override=request.cache_ttl)
    raw["cache_hit"] = False
    _maybe_override_scrape_quality(raw, request.url)
    record_scrape_site_outcome(request.url, raw)
    record_outcome(
        url=request.url, endpoint="/scrape",
        success=raw.get("success", False),
        elapsed_ms=raw.get("elapsed_ms", 0),
        status_code=raw.get("status_code"), error=raw.get("error"),
        wait_strategy=request.wait_for, timeout_used=timeout,
        cache_hit=False,
    )
    raw["results"] = [ScrapeItem(**r) for r in raw.get("results", [])]
    return ScrapeResponse(**raw)
