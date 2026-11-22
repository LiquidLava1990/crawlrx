"""
search_fusion.py -- /search/fusion router
Multi-source parallel search with deduplication and upgraded scoring.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._search_fusion_helpers import (
    ScoreInputs,
    collect_outcomes,
    deduplicate_results,
    score_result,
)

logger = logging.getLogger("crawlrx.search_fusion")
router = APIRouter(prefix="/search", tags=["search"])

logger.info("[OK] search_fusion module loaded")


_VALID_SOURCES = frozenset({"ddg", "bing"})


# ─────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────

class FusionRequest(BaseModel):
    """Request model for POST /search/fusion."""

    query: str = Field(..., min_length=1, description="Search query string")
    count: int = Field(default=10, ge=1, le=50, description="Number of results to return")
    sources: List[str] = Field(
        default_factory=lambda: ["ddg", "bing"],
        description="Search engines to use: ddg, bing",
    )
    min_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum score filter")
    dedupe: bool = Field(default=True, description="Enable URL-based deduplication")


class FusionResult(BaseModel):
    """Single fused search result."""

    title: str = Field(..., description="Result title")
    url: str = Field(..., description="Result URL")
    snippet: Optional[str] = Field(default=None, description="Result snippet/description")
    position: int = Field(default=0, description="Original position from source")
    source: str = Field(default="", description="Source engine that returned this result")
    score: float = Field(default=0.0, description="Composite relevance score (0.0-1.0)")
    date: Optional[str] = Field(default=None, description="Publication or indexed date")


class FusionResponse(BaseModel):
    """Response model for POST /search/fusion."""

    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original query")
    results: List[FusionResult] = Field(
        default_factory=list, description="Merged, deduped, rescored results",
    )
    sources_used: List[str] = Field(
        default_factory=list, description="Sources that were queried",
    )
    total: int = Field(default=0, description="Total results returned")
    fusion_stats: Dict[str, Any] = Field(
        default_factory=dict, description="Stats per source and dedup info",
    )
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _select_sources(raw: List[str]) -> List[str]:
    """Lowercase + strip, then keep only known sources."""
    return [
        candidate
        for candidate in (s.lower().strip() for s in raw)
        if candidate in _VALID_SOURCES
    ]


def _no_sources_response(query: str) -> FusionResponse:
    return FusionResponse(
        success=False,
        query=query,
        results=[],
        sources_used=[],
        total=0,
        fusion_stats={},
        elapsed_ms=0,
        error="No valid sources specified. Valid: %s" % sorted(_VALID_SOURCES),
    )


def _all_failed_response(
    query: str,
    sources_used: List[str],
    stats: Dict[str, int],
    errors: List[str],
    elapsed_ms: int,
) -> FusionResponse:
    return FusionResponse(
        success=False,
        query=query,
        results=[],
        sources_used=sources_used,
        total=0,
        fusion_stats={**stats, "dupes_removed": 0},
        elapsed_ms=elapsed_ms,
        error="All sources failed. Errors: %s" % "; ".join(errors),
    )


def _apply_scores(items: List[Dict[str, Any]], query: str) -> None:
    for result in items:
        result["score"] = score_result(
            ScoreInputs(
                title=result.get("title", ""),
                url=result.get("url", ""),
                snippet=result.get("snippet"),
                query=query,
                date=result.get("date"),
            )
        )


def _filter_and_sort(
    items: List[Dict[str, Any]], min_score: float, count: int,
) -> List[Dict[str, Any]]:
    if min_score > 0:
        items = [r for r in items if r.get("score", 0) >= min_score]
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return items[:count]


def _to_fusion_results(items: List[Dict[str, Any]]) -> List[FusionResult]:
    return [
        FusionResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=r.get("snippet"),
            position=r.get("position", 0),
            source=r.get("source", ""),
            score=r.get("score", 0.0),
            date=r.get("date"),
        )
        for r in items
    ]


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────

@router.post(
    "/fusion",
    response_model=FusionResponse,
    summary="Multi-source parallel search fusion",
)
def search_fusion(request: FusionRequest) -> FusionResponse:
    """Run sources in parallel, merge, dedupe, rescore, return top ``count`` (sync)."""
    start_time = time.time()
    sources_to_run = _select_sources(request.sources)
    if not sources_to_run:
        return _no_sources_response(request.query)

    all_results, stats, sources_used, errors = collect_outcomes(
        sources_to_run, request.query, request.count,
    )
    if not all_results:
        return _all_failed_response(
            request.query,
            sources_used,
            stats,
            errors,
            int((time.time() - start_time) * 1000),
        )

    _apply_scores(all_results, request.query)

    dupes_removed = 0
    if request.dedupe:
        all_results, dupes_removed = deduplicate_results(
            all_results, keep_highest_score=True,
        )

    final = _filter_and_sort(all_results, request.min_score, request.count)
    elapsed_ms = int((time.time() - start_time) * 1000)
    return FusionResponse(
        success=True,
        query=request.query,
        results=_to_fusion_results(final),
        sources_used=sources_used,
        total=len(final),
        fusion_stats={**stats, "dupes_removed": dupes_removed},
        elapsed_ms=elapsed_ms,
        error=None if not errors else "Partial errors: %s" % "; ".join(errors),
    )
