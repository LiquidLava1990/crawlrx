"""
search_deep.py -- /search/deep and /search/news (Google News) endpoints
CrawlRX "Search Hacker" mode -- Google dork query builder, multi-source merge, smart expansion.

Endpoints:
  POST /search/deep   -- dork query builder + Google + DDG multi-source merge + scoring
  POST /search/news   -- Google News tab scraping (last 7 days)

Design rules (coder-standards):
  - No f-string logging
  - Browser/page/context closed in finally blocks (page -> context, headless browser only)
  - All imports at module top
  - No silent catches
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from fastapi import APIRouter

from ._search_deep_browser import search_google_dork, search_google_news
from ._search_deep_helpers import (
    DDGS_AVAILABLE,
    DeepSearchInputs,
    build_dork_query,
    ddg_news_search,
    ddg_text_search,
    expand_query,
    merge_and_score,
)
from ._search_deep_scoring import score_result
from ._search_helpers import DorkParams, SEARCH_NET_EXCEPTIONS, SERP_BROWSER_EXCEPTIONS
from .models import (
    DeepSearchRequest,
    DeepSearchResponse,
    DeepSearchResult,
    NewsDeepRequest,
    NewsDeepResponse,
    NewsDeepResult,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.search_deep")
router = APIRouter(prefix="/search", tags=["search-deep"])

_MIN_RESULTS_FOR_SUCCESS = 5


# ─────────────────────────────────────────────
# Public dork query builder (kept for back-compat callers)
# ─────────────────────────────────────────────

def build_dork_query_simple(query: str, params: DorkParams) -> str:
    """Build a Google dork query string from a ``DorkParams`` bundle."""
    return build_dork_query(query, params)


# ─────────────────────────────────────────────
# Concurrent Google + DDG fetch
# ─────────────────────────────────────────────

async def _fetch_google_and_ddg(
    dork_query: str, num_results: int, timeout: int,
) -> tuple[List[dict], List[dict]]:
    """Run Google dork SERP and DDG text in parallel; return both result lists."""
    google_task = asyncio.create_task(
        search_google_dork(dork_query, num_results, timeout),
    )
    ddg_task = asyncio.create_task(
        asyncio.to_thread(ddg_text_search, dork_query, num_results),
    )
    google_results, ddg_results = await asyncio.gather(
        google_task, ddg_task, return_exceptions=True,
    )
    return (
        _flatten_or_empty(google_results, "google leg"),
        _flatten_or_empty(ddg_results, "ddg leg"),
    )


def _flatten_or_empty(value: Any, label: str) -> List[dict]:
    if isinstance(value, BaseException):
        logger.warning("%s exception: %s", label, type(value).__name__)
        return []
    return value or []


# ─────────────────────────────────────────────
# Smart expansion loop
# ─────────────────────────────────────────────

def _expansion_strategies(
    original_query: str, site_filter: Optional[str],
) -> List[str]:
    """Build the ordered list of expansion queries to try after a thin SERP."""
    strategies: List[str] = list(expand_query(original_query))
    if site_filter:
        strategies.append(original_query)
    if not original_query.startswith('"'):
        strategies.append('"%s"' % original_query)
    return strategies


def _append_new_records(
    merged: List[dict], extras: List[dict],
) -> int:
    """Append unseen-URL records to ``merged``; return count added."""
    existing = {item["url"] for item in merged}
    added = 0
    for item in extras:
        url = item.get("url", "")
        if not url or url in existing:
            continue
        merged.append(item)
        existing.add(url)
        added += 1
    return added


async def _run_expansion(
    merged: List[dict],
    strategies: List[str],
    num_results: int,
    timeout: int,
    original_query: str,
) -> None:
    """Run extra queries, merging new records until threshold reached."""
    for strat_query in strategies:
        if len(merged) >= _MIN_RESULTS_FOR_SUCCESS:
            break
        google_extra, ddg_extra = await _fetch_google_and_ddg(
            strat_query, num_results, timeout,
        )
        extra_merged = merge_and_score(
            google_extra, ddg_extra, original_query, score_result,
        )
        added = _append_new_records(merged, extra_merged)
        logger.info(
            "Expansion '%s' added %d new results (total %d)",
            strat_query, added, len(merged),
        )


async def _search_with_expansion(
    inputs: DeepSearchInputs,
) -> tuple[List[dict], str]:
    """Run primary dork SERP + DDG; expand on thin response."""
    dork_query = build_dork_query(inputs.query, inputs.dork_params)
    google_results, ddg_results = await _fetch_google_and_ddg(
        dork_query, inputs.num_results, inputs.timeout,
    )
    merged = merge_and_score(
        google_results, ddg_results, inputs.query, score_result,
    )
    if len(merged) >= _MIN_RESULTS_FOR_SUCCESS:
        return merged, dork_query
    logger.info(
        "Only %d results from primary query -- expanding with synonyms/variations",
        len(merged),
    )
    strategies = _expansion_strategies(inputs.query, inputs.dork_params.site_filter)
    await _run_expansion(merged, strategies, inputs.num_results, inputs.timeout, inputs.query)
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged, dork_query


# ─────────────────────────────────────────────
# News merge (Google News + DDG fallback + scoring)
# ─────────────────────────────────────────────

def _score_news_records(records: List[dict], query: str) -> List[dict]:
    """Attach a relevance score to each news record and sort descending."""
    scored: List[dict] = []
    for item in records:
        proxy = {
            "title": item.get("headline", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
        }
        item["score"] = score_result(query, proxy, item.get("date"))
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


async def _run_news_search(query: str, num_results: int, timeout: int) -> dict:
    """Google News with DDG news fallback, scored + capped."""
    start = timer_ms()
    raw = await search_google_news(query, num_results, timeout)
    if not raw and DDGS_AVAILABLE:
        logger.info("Google News empty -- trying DDG news for '%s'", query)
        raw.extend(await asyncio.to_thread(ddg_news_search, query, num_results))
    scored = _score_news_records(raw, query)
    return {
        "success": bool(scored),
        "query": query,
        "results": scored[:num_results],
        "total_found": len(scored),
        "elapsed_ms": int(timer_ms() - start),
        "error": None if scored else "No news results found",
    }


# ─────────────────────────────────────────────
# Deep search orchestration
# ─────────────────────────────────────────────

def _request_to_inputs(
    request: DeepSearchRequest, timeout: int,
) -> DeepSearchInputs:
    return DeepSearchInputs(
        query=request.query,
        dork_params=DorkParams(
            site_filter=request.site_filter,
            file_type=request.file_type,
            exact_phrase=request.exact_phrase,
            exclude_terms=request.exclude_terms,
            inurl=request.inurl,
            intitle=request.intitle,
        ),
        num_results=request.num_results,
        timeout=timeout,
    )


def _shape_deep_results(merged: List[dict], num_results: int) -> List[dict]:
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet"),
            "date": r.get("date"),
            "source": r.get("source", ""),
            "score": r.get("score", 0.0),
        }
        for r in merged[:num_results]
    ]


async def _run_deep_search(inputs: DeepSearchInputs) -> dict:
    start = timer_ms()
    merged, final_query = await _search_with_expansion(inputs)
    return {
        "success": True,
        "query": inputs.query,
        "dork_query": final_query,
        "results": _shape_deep_results(merged, inputs.num_results),
        "total_found": len(merged),
        "elapsed_ms": int(timer_ms() - start),
        "error": None,
    }


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

_DEEP_HANDLER_EXCEPTIONS = (
    *SERP_BROWSER_EXCEPTIONS, *SEARCH_NET_EXCEPTIONS, asyncio.TimeoutError,
)


def _empty_deep_response(query: str, error_message: str) -> DeepSearchResponse:
    return DeepSearchResponse(
        success=False,
        query=query,
        dork_query="",
        results=[],
        total_found=0,
        elapsed_ms=0,
        error=error_message,
    )


def _empty_news_response(query: str, error_message: str) -> NewsDeepResponse:
    return NewsDeepResponse(
        success=False,
        query=query,
        results=[],
        total_found=0,
        elapsed_ms=0,
        error=error_message,
    )


@router.post(
    "/deep",
    response_model=DeepSearchResponse,
    summary="Deep search with Google dork query builder + multi-source merge",
    tags=["search-deep"],
)
async def search_deep(request: DeepSearchRequest) -> DeepSearchResponse:
    """Search-hacker mode: Google + DDG, dorked + scored + expanded."""
    timeout = max(request.timeout, 1000)
    inputs = _request_to_inputs(request, timeout)
    try:
        raw = await _run_deep_search(inputs)
    except _DEEP_HANDLER_EXCEPTIONS as exc:
        logger.error(
            "Deep search route error for '%s': %s", request.query, type(exc).__name__,
        )
        return _empty_deep_response(
            request.query, "Internal error: %s" % type(exc).__name__,
        )
    raw["results"] = [DeepSearchResult(**r) for r in raw.get("results", [])]
    return DeepSearchResponse(**raw)


@router.post(
    "/news/google",
    response_model=NewsDeepResponse,
    summary="Google News scraping -- last 7 days",
    tags=["search-deep"],
)
async def search_news_google(request: NewsDeepRequest) -> NewsDeepResponse:
    """Scrape Google News (with DDG fallback) for the last 7 days."""
    timeout = max(request.timeout, 1000)
    try:
        raw = await _run_news_search(request.query, request.num_results, timeout)
    except _DEEP_HANDLER_EXCEPTIONS as exc:
        logger.error(
            "News search route error for '%s': %s",
            request.query, type(exc).__name__,
        )
        return _empty_news_response(
            request.query, "Internal error: %s" % type(exc).__name__,
        )
    raw["results"] = [NewsDeepResult(**r) for r in raw.get("results", [])]
    return NewsDeepResponse(**raw)
