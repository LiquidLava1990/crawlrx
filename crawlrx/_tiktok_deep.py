"""TikTokApi-backed deep-profile fetch + sync wrappers.

Owns the davidteather TikTokApi session lifecycle and produces the
pydantic responses consumed by the ``/deep-profile`` and
``/niche-analyze`` endpoints.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from TikTokApi import TikTokApi

from . import _tiktok_aggregates as _agg
from . import _tiktok_niche as _niche
from .models import (
    TikTokDeepProfileResponse,
    TikTokNicheAnalyzeResponse,
)

logger = logging.getLogger("crawlrx.tiktok.deep")


# Narrow exception tuple for the deep-profile loop -- replaces every bare
# `except Exception` block the previous implementation carried.
TIKTOKAPI_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    asyncio.TimeoutError,
    TimeoutError,
)


def _profile_dict(user_info: Dict[str, Any]) -> Dict[str, Any]:
    """Project the TikTokApi userInfo blob to the canonical profile shape."""
    user_data = user_info.get("userInfo", {}).get("user", {})
    user_stats = user_info.get("userInfo", {}).get("stats", {})
    user_stats_v2 = user_info.get("userInfo", {}).get("statsV2", {})
    return {
        "uniqueId": user_data.get("uniqueId"),
        "nickname": user_data.get("nickname"),
        "signature": user_data.get("signature"),
        "id": user_data.get("id"),
        "secUid": user_data.get("secUid"),
        "verified": user_data.get("verified"),
        "privateAccount": user_data.get("privateAccount"),
        "avatarLarger": user_data.get("avatarLarger"),
        "followerCount": user_stats.get("followerCount"),
        "followingCount": user_stats.get("followingCount"),
        "friendCount": user_stats.get("friendCount"),
        "heart": user_stats.get("heart"),
        "heartCount": user_stats.get("heartCount"),
        "videoCount": user_stats.get("videoCount"),
        "diggCount": user_stats.get("diggCount"),
        "statsV2": user_stats_v2,
    }


def _safe_int(val: Any) -> int:
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


def _video_dict_of(video: Any) -> Dict[str, Any]:
    """Read TikTokApi ``video.as_dict`` (method or property) defensively."""
    attr = getattr(video, "as_dict", None)
    if callable(attr):
        return attr() or {}
    return attr or {}


def _hashtags_of(video: Any) -> List[str]:
    out: List[str] = []
    for tag in getattr(video, "hashtags", []):
        if hasattr(tag, "title"):
            out.append(tag.title)
        elif isinstance(tag, dict) and "title" in tag:
            out.append(tag["title"])
    return out


def _sound_of(video: Any) -> Optional[Dict[str, Any]]:
    sound = getattr(video, "sound", None)
    if not sound:
        return None
    sd = sound if isinstance(sound, dict) else getattr(sound, "__dict__", {})
    if not isinstance(sd, dict):
        return None
    return {
        "id": sd.get("id"),
        "title": sd.get("title"),
        "author": sd.get("author"),
        "is_original": sd.get("is_original"),
    }


def _engagement_rate(views: int, likes: int, comments: int, shares: int) -> float:
    if views <= 0:
        return 0.0
    return round((likes + comments + shares) / views * 100, 4)


def _build_video_record(video: Any) -> Dict[str, Any]:
    """Build one canonical video record from a TikTokApi video object."""
    stats = getattr(video, "stats", {})
    if not isinstance(stats, dict):
        stats = {}
    views = _safe_int(stats.get("playCount"))
    likes = _safe_int(stats.get("diggCount"))
    comments = _safe_int(stats.get("commentCount"))
    shares = _safe_int(stats.get("shareCount"))
    create_time = getattr(video, "create_time", None)
    ts_unix = create_time if isinstance(create_time, int) else None
    ts_human = datetime.fromtimestamp(ts_unix).isoformat() if ts_unix else None
    desc_blob = _video_dict_of(video)
    desc = desc_blob.get("desc", "") if isinstance(desc_blob, dict) else ""
    return {
        "id": getattr(video, "id", None),
        "url": getattr(video, "url", None),
        "description": desc,
        "create_time_unix": ts_unix,
        "create_time_human": ts_human,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "hashtags": _hashtags_of(video),
        "sound": _sound_of(video),
        "engagement_rate": _engagement_rate(views, likes, comments, shares),
    }


def _safe_build_video_record(video: Any) -> Optional[Dict[str, Any]]:
    """Build one video record, returning None on TikTokApi recoverable error."""
    try:
        return _build_video_record(video)
    except TIKTOKAPI_EXCEPTIONS as exc:
        logger.warning(
            "TikTokApi: skipping video due to %s: %s", type(exc).__name__, exc,
        )
        # explicit recovery: TIKTOKAPI_EXCEPTIONS non-fatal; caller skips video
        return None


async def _collect_videos(user: Any, max_videos: int) -> List[Dict[str, Any]]:
    """Iterate ``user.videos`` and return up to ``max_videos`` records."""
    out: List[Dict[str, Any]] = []
    async for video in user.videos(count=max_videos):
        if len(out) >= max_videos:
            break
        record = _safe_build_video_record(video)
        if record is None:
            continue
        out.append(record)
        if len(out) % 10 == 0:
            logger.info("TikTokApi: collected %d videos", len(out))
    return out


async def close_quietly(closer: Callable[[], Awaitable[Any]], *, where: str) -> None:
    """Run a finalizer coroutine; never raise."""
    try:
        await closer()
    except TIKTOKAPI_EXCEPTIONS as exc:
        logger.debug("Suppressed %s in %s: %s", type(exc).__name__, where, exc)


async def fetch_deep_profile(username: str, max_videos: int) -> Dict[str, Any]:
    """Async fetch via TikTokApi; partial-result aware."""
    api = TikTokApi()
    profile: Dict[str, Any] = {}
    videos: List[Dict[str, Any]] = []
    try:
        logger.info(
            "TikTokApi: creating session for @%s (max_videos=%d)",
            username, max_videos,
        )
        await api.create_sessions(
            ms_tokens=[None],
            num_sessions=1,
            browser="chromium",
            headless=False,
            sleep_after=5,
        )
        user = api.user(username=username)
        profile = _profile_dict(await user.info())
        logger.info(
            "TikTokApi: profile fetched, %s followers", profile.get("followerCount"),
        )
        videos = await _collect_videos(user, max_videos)
        logger.info(
            "TikTokApi: done, %d videos collected for @%s", len(videos), username,
        )
        return {"profile": profile, "videos": videos, "error": None}
    except TIKTOKAPI_EXCEPTIONS as exc:
        logger.error("TikTokApi: fatal error for @%s: %s", username, exc)
        return {
            "profile": profile,
            "videos": videos,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        await close_quietly(api.close_sessions, where="api.close_sessions")
        await close_quietly(api.stop_playwright, where="api.stop_playwright")


def _run_async(
    coro_factory: Callable[[], Awaitable[Dict[str, Any]]], where: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run ``coro_factory()`` in a fresh loop; return ``(result, error_str)``."""
    try:
        return asyncio.run(coro_factory()), None
    except TIKTOKAPI_EXCEPTIONS as exc:
        logger.error("%s sync wrapper error: %s", where, exc)
        return None, f"{type(exc).__name__}: {exc}"


def do_deep_profile(
    username: str, max_videos: int, timeout_ms: int,
) -> TikTokDeepProfileResponse:
    """Sync wrapper: TikTokApi fetch -> aggregates -> response model."""
    started = time.time()
    result, error = _run_async(
        lambda: fetch_deep_profile(username, max_videos), where="deep-profile",
    )
    elapsed = int((time.time() - started) * 1000)
    if result is None:
        return TikTokDeepProfileResponse(
            success=False, username=username, error=error or "", elapsed_ms=elapsed,
        )
    if result.get("error") and not result.get("videos"):
        return TikTokDeepProfileResponse(
            success=False, username=username, error=result["error"], elapsed_ms=elapsed,
        )
    aggregates = _agg.compute_deep_aggregates(result["videos"])
    return TikTokDeepProfileResponse(
        success=True,
        username=username,
        scraped_at=datetime.now().isoformat(),
        profile=result["profile"],
        videos=result["videos"],
        aggregates=aggregates,
        elapsed_ms=elapsed,
        error=result.get("error"),
    )


def do_niche_analyze(
    username: str, max_videos: int, timeout_ms: int,
) -> TikTokNicheAnalyzeResponse:
    """Sync wrapper: deep fetch -> aggregates -> niche scoring -> response."""
    started = time.time()
    result, error = _run_async(
        lambda: fetch_deep_profile(username, max_videos), where="niche-analyze",
    )
    elapsed = int((time.time() - started) * 1000)
    if result is None:
        return TikTokNicheAnalyzeResponse(
            success=False, username=username, error=error or "", elapsed_ms=elapsed,
        )
    if result.get("error") and not result.get("videos"):
        return TikTokNicheAnalyzeResponse(
            success=False, username=username, error=result["error"], elapsed_ms=elapsed,
        )

    videos = result["videos"]
    profile = result["profile"]
    aggregates = _agg.compute_deep_aggregates(videos)
    raw_scores, detected = _niche.build_niche_scores(videos, aggregates)
    top_scores = (
        [s for s in raw_scores if s["niche"] in detected][:5]
        if detected else raw_scores[:3]
    )
    summary = _niche.summarize_niche(
        aggregates["adjusted_avg_views"],
        aggregates["avg_engagement_rate"],
        top_scores,
    )
    return TikTokNicheAnalyzeResponse(
        success=True,
        username=username,
        current_profile={
            "followers": profile.get("followerCount", 0) or 0,
            "avg_views": aggregates["adjusted_avg_views"],
            "engagement_rate": aggregates["avg_engagement_rate"],
            "videos_analyzed": len(videos),
        },
        detected_niches=detected,
        niche_scores=top_scores,
        summary=summary,
        elapsed_ms=elapsed,
    )
