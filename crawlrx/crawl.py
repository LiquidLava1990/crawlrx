"""crawl.py -- /crawl router (thin orchestrator).

The full crawl pipeline lives in three sibling modules:
- ``_crawl_pre_gates``: URL safety, robots, learning, breaker, quality
- ``_crawl_attempt``: Playwright per-attempt flow + retry loop
- ``_crawl4ai_runner``: alternative crawl4ai engine

This file owns:
- the FastAPI router + ``crawl()`` request handler
- the wall-clock guard (``_execute_crawl_with_timeout``)
- back-compat re-exports so ``crawlrx.batch`` and tests keep importing
  ``_do_crawl``, ``CrawlAttemptParams``, ``close_attempt``, etc. from
  ``crawlrx.crawl``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter

from .cache import get_cache
from .config import USER_AGENTS  # noqa: F401  -- pinned by public API snapshot
from .learning import record_outcome
from .models import CrawlRequest, CrawlResponse
from ._crawl4ai_runner import (
    _crawl4ai_extract,
    _crawl4ai_payload,
    _crawl4ai_render_page,
    _crawl4ai_screenshot,
    _crawl4ai_to_response,
    _crawl_via_crawl4ai,
)
from ._crawl_attempt import (
    CrawlAttemptParams,
    _do_crawl,
    _do_crawl_attempt,
    _make_crawl_context,
    _try_one_crawl_attempt,
    close_attempt,
)
from ._crawl_pre_gates import (
    _apply_learning_overrides,
    _blocked_url_response,
    _cache_hit_response,
    _check_robots,
    _circuit_open_response,
    _maybe_override_quality,
    _record_breaker_outcome,
    _resolve_all_pre_crawl_gates,
    _resolve_breaker,
    _resolve_breaker_or_circuit_open,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
    PARSE_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
    PLAYWRIGHT_EXCEPTIONS,  # noqa: F401  -- pinned by public API snapshot
)
from .captcha_handoff import handoff_wait_seconds

logger = logging.getLogger("crawlrx.crawl")
router = APIRouter(prefix="", tags=["crawl"])


async def _execute_crawl_with_timeout(
    request: CrawlRequest, timeout: int, wall_timeout: int, breaker: object,
) -> tuple[Optional[dict], Optional[CrawlResponse]]:
    """Run _do_crawl under a wall-clock guard; return (raw, abort_response)."""
    try:
        raw = await asyncio.wait_for(
            _do_crawl(
                request.url, request.wait_for, timeout, request.javascript,
                request.extract_links or request.include_links,
                request.extract_text, request.extract_markdown, request.include_images,
                request.user_agent, request.headers, request.screenshot, request.scroll,
                request.agent, request.structured_data, request.stealth, request.readable,
                request.same_domain_only, request.max_links, request.max_images,
                request.js_inject, request.js_inject_timeout,
                request.wait_for_text, request.wait_for_gone, request.wait_for_timeout_extra,
            ),
            timeout=wall_timeout,
        )
        return raw, None
    except asyncio.TimeoutError:
        breaker.record_failure()
        logger.error("Wall-clock timeout (%ds) exceeded for crawl: %s", wall_timeout, request.url)
        return None, CrawlResponse(
            success=False, url=request.url,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
            error_type="timeout", elapsed_ms=wall_timeout * 1000,
        )


@router.post("/crawl", response_model=CrawlResponse, summary="Crawl a URL with Playwright")
async def crawl(request: CrawlRequest) -> CrawlResponse:
    """Render a URL via Playwright; return HTML/text/markdown/links/images/screenshot."""
    cache = get_cache()
    cache_key = (
        cache.make_key("/crawl", request.model_dump()) if request.cache else None
    )
    early = await _resolve_all_pre_crawl_gates(request, cache, cache_key)
    if early is not None:
        return early
    timeout = request.timeout if request.timeout >= 1000 else request.timeout * 1000
    timeout = _apply_learning_overrides(request, timeout)
    breaker_block, _domain, breaker = _resolve_breaker_or_circuit_open(request.url)
    if breaker_block is not None:
        return breaker_block
    wall_timeout = timeout // 1000 + 10 + handoff_wait_seconds()
    raw, abort = await _execute_crawl_with_timeout(
        request, timeout, wall_timeout, breaker,
    )
    if abort is not None:
        return abort
    if raw is None:
        raise RuntimeError(
            "_execute_crawl_with_timeout returned no abort AND no raw result; "
            "contract violation -- exactly one must be non-None"
        )
    _record_breaker_outcome(raw, breaker)
    if request.cache and raw.get("success") and cache_key:
        cache.set(cache_key, raw, ttl_override=request.cache_ttl)
    raw["cache_hit"] = False
    _maybe_override_quality(raw, request.url)
    record_outcome(
        url=request.url, endpoint="/crawl",
        success=raw.get("success", False),
        elapsed_ms=raw.get("elapsed_ms", 0),
        status_code=raw.get("status_code"), error=raw.get("error"),
        wait_strategy=request.wait_for, timeout_used=timeout,
        cache_hit=False, content_length=len(raw.get("html", "") or ""),
    )
    return CrawlResponse(**raw)
