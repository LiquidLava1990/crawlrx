"""
search.py -- /search router
SERP scraping via Playwright. Supports Google, Bing, DuckDuckGo, SearXNG.

## Hardened Fallback Chain 
Execution order: SearXNG -> Bing -> DuckDuckGo -> Google
- If any engine fails/blocks/returns 0 results -> auto-try next
- Never return empty results if ANY engine works
- 3 retries per engine with exponential backoff (1s, 3s, 9s)
- Results deduplicated by URL when multiple engines contribute
- Cache: 1-hour SQLite cache (crawlrx_cache.db) per query+engine
- Anti-detection: UA rotated per request, 0.5-2s random delay between pages

## SearXNG Setup (optional self-hosted fallback):
  1. Install Docker: https://docs.docker.com/get-docker/
  2. docker run -d --name searxng -p 8888:8080 searxng/searxng:latest
  3. Verify: curl http://localhost:8888/search?q=test&format=json  # TODO: use service_registry
  Set SEARXNG_URL env var if using a non-default port/host.

CDP browser pool (browser_pool.py) is used for Playwright calls --
we never open new headless browsers; we reuse the per-thread pool.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ._search_cache import cache_get, cache_init, cache_set, dedup_results, make_cache_key
from ._search_content import fetch_content_for_result
from ._search_engines import (
    DDGS_AVAILABLE,
    _DDGS,
    EngineSearchParams,
    engine_search_with_retry,
)
from ._search_helpers import SEARCH_NET_EXCEPTIONS
from .learning import record_outcome
from .models import SearchRequest, SearchResponse, SearchResult
from .utils import timer_ms

logger = logging.getLogger("crawlrx.search")
router = APIRouter(prefix="", tags=["search"])

# Canonical fallback order. The local SearXNG instance is first because its
# engine set is curated in crawlrx/settings.yml to avoid CAPTCHA-heavy defaults.
_ENGINE_ORDER = ("searxng", "bing", "duckduckgo", "google")

# Quick-search timeout (seconds)
_QUICK_TIMEOUT_S = 5.0


# Ensure cache table exists at import time.
try:
    cache_init()
except SEARCH_NET_EXCEPTIONS as _ce:
    logger.warning("Cache init failed (non-fatal): %s", type(_ce).__name__)


# ─────────────────────────────────────────────
# Engine chain helpers
# ─────────────────────────────────────────────

def _build_engine_chain(requested: str) -> List[str]:
    """Return the canonical engine fallback chain starting with the request."""
    if requested not in _ENGINE_ORDER:
        requested = _ENGINE_ORDER[0]
    return [requested] + [e for e in _ENGINE_ORDER if e != requested]


def _normalize_timeout(raw: int) -> int:
    """Treat values <1000 as seconds, otherwise milliseconds."""
    return raw if raw >= 1000 else raw * 1000


def _params_from_request(request: SearchRequest, timeout_ms: int) -> EngineSearchParams:
    """Bundle ``EngineSearchParams`` from an inbound ``SearchRequest``."""
    return EngineSearchParams(
        num_results=request.num_results,
        timeout_ms=timeout_ms,
        language=request.language,
        country=request.country,
        agent=request.agent,
    )


async def _try_engine_chain(
    chain: List[str],
    query: str,
    params: EngineSearchParams,
) -> dict:
    """Iterate engines until one returns a non-empty success."""
    fallback: dict = {
        "success": False, "results": [], "engine": chain[0], "query": query,
    }
    for engine in chain:
        logger.info("Trying engine '%s' for query '%s'", engine, query)
        result = await engine_search_with_retry(engine, query, params)
        if result.get("success") and result.get("results"):
            logger.info(
                "Engine '%s' succeeded with %d results for '%s'",
                engine, len(result["results"]), query,
            )
            return result
        logger.warning(
            "Engine '%s' failed/empty for '%s': %s",
            engine, query, result.get("error"),
        )
        fallback = result
    return fallback


def _cached_response(cached: dict, query: str) -> SearchResponse:
    """Build the cached-hit response payload."""
    results = [SearchResult(**r) for r in cached["results"]]
    return SearchResponse(
        success=True,
        query=query,
        engine=cached["engine"] + " (cached)",
        results=results,
        total_results=str(len(results)),
        elapsed_ms=0,
    )


async def _fetch_content_for_results(
    items: List[dict], timeout_ms: int, agent: Optional[str],
) -> List[dict]:
    """Run per-result content fetches concurrently, dropping any exceptions."""
    content_timeout = min(timeout_ms, 15000)
    tasks = [fetch_content_for_result(r, content_timeout, agent) for r in items]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[dict] = []
    for i, item in enumerate(raw):
        if isinstance(item, BaseException):
            logger.warning(
                "Content fetch exception for result %d: %s", i, type(item).__name__,
            )
            continue
        out.append(item)
    return out


# ─────────────────────────────────────────────
# FastAPI route
# ─────────────────────────────────────────────

@router.post(
    "/search",
    response_model=SearchResponse,
    summary="SERP scraping -- hardened multi-engine fallback",
)
async def search(request: SearchRequest) -> SearchResponse:
    """Search with auto-fallback: SearXNG -> Bing -> DuckDuckGo -> Google."""
    timeout_ms = _normalize_timeout(request.timeout)
    cache_key = make_cache_key(
        request.query, request.engine, request.num_results,
        request.language, request.country,
    )
    cached = await asyncio.to_thread(cache_get, cache_key)
    if cached:
        return _cached_response(cached, request.query)

    requested = request.engine if request.engine in _ENGINE_ORDER else "google"
    chain = _build_engine_chain(requested)
    params = _params_from_request(request, timeout_ms)

    start = timer_ms()
    raw = await _try_engine_chain(chain, request.query, params)
    deduped = dedup_results(raw.get("results", []))
    if deduped:
        await asyncio.to_thread(
            cache_set, cache_key, deduped, raw.get("engine", requested),
        )

    if request.include_content and deduped:
        logger.info("Fetching content for %d search results", len(deduped))
        deduped = await _fetch_content_for_results(
            deduped, timeout_ms, request.agent,
        )

    results = [SearchResult(**r) for r in deduped]
    elapsed = int(timer_ms() - start)
    record_outcome(
        url="search://" + request.query[:100],
        endpoint="/search",
        success=bool(results),
        elapsed_ms=elapsed,
        error=raw.get("error") if not results else None,
        scraper="search_" + raw.get("engine", requested),
        cache_hit=False,
    )
    return SearchResponse(
        success=bool(results),
        query=request.query,
        engine=raw.get("engine", requested),
        results=results,
        total_results=str(len(results)) if results else None,
        elapsed_ms=elapsed,
        error=raw.get("error") if not results else None,
    )


# ─────────────────────────────────────────────
# Quick search endpoint (DDGS-only, 5s timeout)
# ─────────────────────────────────────────────

class QuickSearchRequest(BaseModel):
    """Request model for GET /search/quick."""

    query: str
    num_results: int = 10


class QuickSearchResponse(BaseModel):
    """Response model for GET /search/quick."""

    success: bool
    query: str
    results: List[SearchResult]
    elapsed_ms: int
    error: Optional[str] = None


def _ddgs_quick_items(query: str, num_results: int) -> List[dict]:
    """Run ddgs blocking call and shape results into the canonical schema."""
    with _DDGS() as ddgs:  # type: ignore[misc] because ddgs has no public stubs; context-manager protocol verified at runtime
        raw = list(ddgs.text(query, max_results=num_results, region="en-US"))
    items: List[dict] = []
    for i, item in enumerate(raw, 1):
        items.append({
            "title": item.get("title", ""),
            "url": item.get("href", item.get("url", "")),
            "snippet": item.get("body", item.get("snippet", "")),
            "position": i,
            "content": None,
        })
    return items


def _quick_response(
    *,
    query: str,
    items: List[dict],
    start: float,
    error_message: Optional[str] = None,
) -> QuickSearchResponse:
    """Build a ``QuickSearchResponse`` from intermediate items."""
    results = [SearchResult(**r) for r in items]
    return QuickSearchResponse(
        success=bool(results) and error_message is None,
        query=query,
        results=results,
        elapsed_ms=int(timer_ms() - start),
        error=error_message,
    )


@router.get(
    "/search/quick",
    response_model=QuickSearchResponse,
    summary="Fast DDGS search (5s timeout)",
)
def search_quick(query: str, num_results: int = 10) -> QuickSearchResponse:
    """Quick search using DuckDuckGo (ddgs) with 5-second timeout (sync route)."""
    start = timer_ms()
    if not DDGS_AVAILABLE or _DDGS is None:
        return _quick_response(
            query=query, items=[], start=start,
            error_message="ddgs library not installed -- run: pip install ddgs",
        )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_ddgs_quick_items, query, num_results)
        try:
            items = future.result(timeout=_QUICK_TIMEOUT_S)
        except FutureTimeout:
            return _quick_response(
                query=query, items=[], start=start,
                error_message="Timeout: search exceeded 5 seconds",
            )
        except SEARCH_NET_EXCEPTIONS as exc:
            return _quick_response(
                query=query, items=[], start=start,
                error_message="ddgs net error: %s" % type(exc).__name__,
            )
    return _quick_response(query=query, items=items, start=start)


# ─────────────────────────────────────────────
# Back-compat shims for callers that imported the old private names
# (``main.py``, ``search_fusion.py``, ``ecommerce_search.py``).
# Routes through the canonical ``_search_engines`` implementations.
# ─────────────────────────────────────────────

async def _do_search_ddgs(
    query: str,
    num_results: int,
    language: Optional[str] = None,
    country: Optional[str] = None,
) -> dict:
    """Compat wrapper -- delegates to ``_search_engines.do_search_ddgs``."""
    from ._search_engines import do_search_ddgs
    return await do_search_ddgs(query, num_results, language, country)


_PRIVATE_HELPERS = (_do_search_ddgs,)
