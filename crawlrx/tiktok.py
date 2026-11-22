"""TikTok scraper for CrawlRX.

Endpoints:
- ``POST /scrape/tiktok/profile``        -- profile stats
- ``POST /scrape/tiktok/videos``         -- video list with stats
- ``POST /scrape/tiktok/video``          -- single video detail
- ``POST /scrape/tiktok/trending``       -- trending hashtags / sounds
- ``POST /scrape/tiktok``                -- legacy combined endpoint
- ``POST /scrape/tiktok/deep-profile``   -- TikTokApi-driven deep scrape
- ``POST /scrape/tiktok/niche-analyze``  -- niche fit scoring

The Playwright-driven SIGI extraction lives in :mod:`_tiktok_extractor` +
:mod:`_tiktok_scrapers`. Deep-profile / niche logic lives in
:mod:`_tiktok_deep` + :mod:`_tiktok_aggregates` + :mod:`_tiktok_niche`.
This file is the FastAPI router with thin async-timeout wrappers, kept
small so the code-quality ceilings stay clear.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Awaitable, Callable, TypeVar

from fastapi import APIRouter

from . import _tiktok_deep as _deep
from . import _tiktok_extractor as _ex
from . import _tiktok_scrapers as _sc
from .models import (
    TikTokDeepProfileRequest,
    TikTokDeepProfileResponse,
    TikTokNicheAnalyzeRequest,
    TikTokNicheAnalyzeResponse,
    TikTokProfileRequest,
    TikTokProfileResponse,
    TikTokRequest,
    TikTokResponse,
    TikTokTrendingRequest,
    TikTokTrendingResponse,
    TikTokVideoRequest,
    TikTokVideoResponse,
    TikTokVideosRequest,
    TikTokVideosResponse,
)

logger = logging.getLogger("crawlrx.tiktok")
router = APIRouter(prefix="/scrape/tiktok", tags=["tiktok"])

_T = TypeVar("_T")

# Re-export selected helpers so callers importing from
# ``crawlrx.tiktok`` keep working after the split.
parse_count = _ex.parse_count
extract_hashtags = _ex.extract_hashtags
extract_video_id_from_url = _ex.extract_video_id_from_url


async def _run_in_executor(
    sync_fn: Callable[..., _T], /, *args, timeout_s: int,
) -> _T:
    """Run a blocking sync function on the default executor with timeout."""
    loop = asyncio.get_running_loop()
    fn = functools.partial(sync_fn, *args)
    return await asyncio.wait_for(
        loop.run_in_executor(None, fn),
        timeout=timeout_s,
    )


def _executor_timeout_s(timeout_ms: int, *, base_min_s: int, padding_s: int) -> int:
    """Convert a millisecond budget to a wall-clock executor timeout."""
    return max(timeout_ms // 1000, base_min_s) + padding_s


# ---------------------------------------------------------------------------
# Sync wrappers around the async scraper coroutines
# ---------------------------------------------------------------------------

def _do_scrape_profile(username: str, timeout_ms: int) -> TikTokProfileResponse:
    """Run :func:`_tiktok_scrapers.scrape_profile` in a fresh event loop."""
    return asyncio.run(_sc.scrape_profile(username, timeout_ms))


def _do_scrape_videos(
    username: str, limit: int, timeout_ms: int,
) -> TikTokVideosResponse:
    """Run :func:`_tiktok_scrapers.scrape_videos` in a fresh event loop."""
    return asyncio.run(_sc.scrape_videos(username, limit, timeout_ms))


def _do_scrape_video(
    url: str | None, video_id: str | None, timeout_ms: int,
) -> TikTokVideoResponse:
    """Run :func:`_tiktok_scrapers.scrape_video` in a fresh event loop."""
    return asyncio.run(_sc.scrape_video(url, video_id, timeout_ms))


def _do_scrape_trending(limit: int, timeout_ms: int) -> TikTokTrendingResponse:
    """Run :func:`_tiktok_scrapers.scrape_trending` in a fresh event loop."""
    return asyncio.run(_sc.scrape_trending(limit, timeout_ms))


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

async def _wrap_request(
    sync_fn: Callable[..., _T],
    *args,
    timeout_ms: int,
    base_min_s: int = 10,
    padding_s: int = 5,
    where: str,
    failure: Callable[[str], _T],
) -> _T:
    """Shared error envelope around a sync run-in-executor call."""
    timeout_s = _executor_timeout_s(
        timeout_ms, base_min_s=base_min_s, padding_s=padding_s,
    )
    try:
        return await _run_in_executor(sync_fn, *args, timeout_s=timeout_s)
    except (asyncio.TimeoutError, RuntimeError, OSError) as exc:
        logger.error("[TikTok] %s request failed: %s", where, exc)
        return failure(f"{type(exc).__name__}: {exc}")


@router.post(
    "/profile",
    response_model=TikTokProfileResponse,
    summary="Scrape TikTok profile",
)
async def scrape_tiktok_profile(
    request: TikTokProfileRequest,
) -> TikTokProfileResponse:
    """Scrape a TikTok profile for stats and info.

    - ``username``: TikTok handle without ``@``.
    - ``timeout``: request timeout in milliseconds (default 30000).

    Uses a CDP browser on port 9223 with existing cookies for authentication.
    """
    return await _wrap_request(
        _do_scrape_profile,
        request.username,
        request.timeout,
        timeout_ms=request.timeout,
        where="profile",
        failure=lambda err: TikTokProfileResponse(
            success=False, error=f"Request failed: {err}", elapsed_ms=request.timeout,
        ),
    )


@router.post(
    "/videos",
    response_model=TikTokVideosResponse,
    summary="Get TikTok videos",
)
async def scrape_tiktok_videos(
    request: TikTokVideosRequest,
) -> TikTokVideosResponse:
    """Get the video list from a TikTok profile."""
    return await _wrap_request(
        _do_scrape_videos,
        request.username,
        request.limit,
        request.timeout,
        timeout_ms=request.timeout,
        padding_s=10,
        where="videos",
        failure=lambda err: TikTokVideosResponse(
            success=False,
            username=request.username,
            error=f"Request failed: {err}",
            elapsed_ms=request.timeout,
        ),
    )


@router.post(
    "/video",
    response_model=TikTokVideoResponse,
    summary="Get TikTok video details",
)
async def scrape_tiktok_video(
    request: TikTokVideoRequest,
) -> TikTokVideoResponse:
    """Get detailed info for a single TikTok video."""
    return await _wrap_request(
        _do_scrape_video,
        request.url,
        request.video_id,
        request.timeout,
        timeout_ms=request.timeout,
        where="video",
        failure=lambda err: TikTokVideoResponse(
            success=False, error=f"Request failed: {err}", elapsed_ms=request.timeout,
        ),
    )


@router.post(
    "/trending",
    response_model=TikTokTrendingResponse,
    summary="Get trending hashtags/sounds",
)
async def scrape_tiktok_trending(
    request: TikTokTrendingRequest,
) -> TikTokTrendingResponse:
    """Get trending hashtags and sounds from the TikTok Discover page."""
    return await _wrap_request(
        _do_scrape_trending,
        request.limit,
        request.timeout,
        timeout_ms=request.timeout,
        where="trending",
        failure=lambda err: TikTokTrendingResponse(
            success=False, error=f"Request failed: {err}", elapsed_ms=request.timeout,
        ),
    )


# ---------------------------------------------------------------------------
# Combined backwards-compat endpoint
# ---------------------------------------------------------------------------

def _do_scrape_tiktok_legacy(
    username: str, include_videos: bool, max_videos: int, timeout_ms: int,
) -> TikTokResponse:
    """Compose profile + (optional) video list into the legacy response shape."""
    started = time.time()
    profile_result = _do_scrape_profile(username, timeout_ms)
    if not profile_result.success or profile_result.profile is None:
        return TikTokResponse(
            success=False,
            username=username,
            error=profile_result.error,
            elapsed_ms=profile_result.elapsed_ms,
        )
    profile = profile_result.profile
    videos = []
    if include_videos:
        videos_result = _do_scrape_videos(username, max_videos, timeout_ms)
        if videos_result.success:
            videos = videos_result.videos
    return TikTokResponse(
        success=True,
        username=username,
        display_name=profile.display_name,
        bio=profile.bio,
        followers=profile.followers,
        following=profile.following,
        likes=profile.total_likes,
        video_count=profile.video_count,
        videos=videos,
        elapsed_ms=int((time.time() - started) * 1000),
    )


@router.post(
    "",
    response_model=TikTokResponse,
    summary="Scrape TikTok profile (legacy)",
    include_in_schema=False,
)
async def scrape_tiktok_legacy(request: TikTokRequest) -> TikTokResponse:
    """Backwards-compatible combined endpoint (use :func:`scrape_tiktok_profile`)."""
    return await _wrap_request(
        _do_scrape_tiktok_legacy,
        request.username,
        request.include_videos,
        request.max_videos,
        request.timeout,
        timeout_ms=request.timeout,
        padding_s=10,
        where="legacy",
        failure=lambda err: TikTokResponse(
            success=False,
            username=request.username,
            error=f"Request failed: {err}",
            elapsed_ms=request.timeout,
        ),
    )


# ---------------------------------------------------------------------------
# Deep-profile + niche analysis (TikTokApi-driven)
# ---------------------------------------------------------------------------

async def _await_deep(
    coro: Awaitable[_T], *, timeout_ms: int, where: str, failure: Callable[[str], _T],
) -> _T:
    """Wait on a deep-profile coroutine with a generous wall-clock timeout."""
    timeout_s = _executor_timeout_s(timeout_ms, base_min_s=60, padding_s=60)
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except (asyncio.TimeoutError, RuntimeError, OSError) as exc:
        logger.error("%s endpoint error: %s", where, exc)
        return failure(f"{type(exc).__name__}: {exc}")


@router.post(
    "/deep-profile",
    response_model=TikTokDeepProfileResponse,
    summary="Deep TikTok profile scrape",
)
async def scrape_tiktok_deep_profile(
    request: TikTokDeepProfileRequest,
) -> TikTokDeepProfileResponse:
    """Full deep scrape via TikTokApi (davidteather).

    Returns profile data, up to ``max_videos`` records with full stats, and
    aggregate analytics (IQR-adjusted view averages, hashtag frequencies,
    best posting times, sounds). Opens a visible Chromium window -- typical
    runtime 45-120 seconds.
    """
    return await _await_deep(
        asyncio.to_thread(
            _deep.do_deep_profile,
            request.username, request.max_videos, request.timeout,
        ),
        timeout_ms=request.timeout,
        where="deep-profile",
        failure=lambda err: TikTokDeepProfileResponse(
            success=False,
            username=request.username,
            error=err,
            elapsed_ms=request.timeout,
        ),
    )


@router.post(
    "/niche-analyze",
    response_model=TikTokNicheAnalyzeResponse,
    summary="TikTok niche analysis",
)
async def scrape_tiktok_niche_analyze(
    request: TikTokNicheAnalyzeRequest,
) -> TikTokNicheAnalyzeResponse:
    """Score an account's niche fit across 15 content categories.

    Pulls real video data via TikTokApi, scores against keyword maps and
    2026 view benchmarks, returns ranked recommendations with fit scores.
    """
    return await _await_deep(
        asyncio.to_thread(
            _deep.do_niche_analyze,
            request.username, request.max_videos, request.timeout,
        ),
        timeout_ms=request.timeout,
        where="niche-analyze",
        failure=lambda err: TikTokNicheAnalyzeResponse(
            success=False,
            username=request.username,
            error=err,
            elapsed_ms=request.timeout,
        ),
    )
