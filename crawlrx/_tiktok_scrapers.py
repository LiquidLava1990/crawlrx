"""TikTok per-endpoint scrapers (sibling module).

Each ``scrape_*`` returns the matching pydantic response model. The shared
:func:`_open_session` helper isolates the Playwright lifecycle so the
public scrapers stay below the code-quality ceilings.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote

from playwright.async_api import async_playwright

from . import _tiktok_extractor as _ex
from .models import (
    TikTokProfile,
    TikTokProfileResponse,
    TikTokTrendingItem,
    TikTokTrendingResponse,
    TikTokVideo,
    TikTokVideoDetail,
    TikTokVideoResponse,
    TikTokVideosResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import ensure_cdp_browser

logger = logging.getLogger("crawlrx.tiktok.scrapers")
TIKTOK_CDP_PORT = 9223


@dataclass
class _Session:
    """Tuple bundle of Playwright handles + start timestamp."""

    page: Any
    start_time: float


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


@asynccontextmanager
async def _open_session() -> AsyncIterator[_Session]:
    """Open a CDP-attached Playwright page; close handles in ``finally``."""
    await ensure_cdp_browser(TIKTOK_CDP_PORT)
    browser = ctx = page = None
    started = time.time()
    async with async_playwright() as p:
        try:
            browser, ctx, page = await make_stealth_context(p)
            yield _Session(page=page, start_time=started)
        finally:
            await close_stealth_context(browser, ctx, page)


def _profile_from_user(user: Dict[str, Any], username: str, url: str) -> TikTokProfile:
    """Build a TikTokProfile from extracted user-data dict."""
    return TikTokProfile(
        username=user.get("uniqueId") or username,
        display_name=user.get("nickname") or username,
        bio=user.get("signature", ""),
        followers=str(user.get("followerCount", "")),
        following=str(user.get("followingCount", "")),
        total_likes=str(user.get("heartCount", "")),
        video_count=str(user.get("videoCount", "")),
        verified=user.get("verified", False),
        profile_url=url,
        avatar_url=(
            user.get("avatarLarger")
            or user.get("avatarMedium")
            or user.get("avatarThumb", "")
        ),
    )


@dataclass(frozen=True)
class _NavSpec:
    """Navigation + settle parameters for a SIGI extraction."""

    url: str
    timeout_ms: int
    wait_until: str = "domcontentloaded"
    pause_ms: int = 3000


async def _navigate_and_extract(
    session: _Session, spec: _NavSpec,
) -> Dict[str, Any]:
    """Common navigate / settle / SIGI-extract step shared by profile + videos."""
    page = session.page
    await page.goto(spec.url, wait_until=spec.wait_until, timeout=spec.timeout_ms)
    await page.wait_for_timeout(spec.pause_ms)
    return await _ex.extract_sigi_state_data(page)


async def scrape_profile(username: str, timeout_ms: int) -> TikTokProfileResponse:
    """Scrape a TikTok profile via SIGI_STATE / UDR extraction."""
    url = f"https://www.tiktok.com/@{quote(username)}"
    try:
        async with _open_session() as session:
            logger.info("[TikTok] Navigating to profile: %s", url)
            data = await _navigate_and_extract(
                session,
                _NavSpec(
                    url=url,
                    timeout_ms=timeout_ms,
                    wait_until="networkidle",
                    pause_ms=1500,
                ),
            )
            if not data.get("sigi_found"):
                logger.warning("[TikTok] SIGI_STATE not found in page")
                return TikTokProfileResponse(
                    success=False,
                    error="Could not extract profile data - SIGI_STATE not found",
                    elapsed_ms=_elapsed_ms(session.start_time),
                )
            user = data.get("user")
            if not user:
                return TikTokProfileResponse(
                    success=False,
                    error="Could not extract user data from page",
                    elapsed_ms=_elapsed_ms(session.start_time),
                )
            profile = _profile_from_user(user, username, url)
            logger.info(
                "[TikTok] scraped profile @%s: %s followers",
                username, profile.followers,
            )
            return TikTokProfileResponse(
                success=True,
                profile=profile,
                elapsed_ms=_elapsed_ms(session.start_time),
            )
    except _ex.SCRAPE_EXCEPTIONS as exc:
        logger.error("[TikTok] error scraping profile @%s: %s", username, exc)
        return TikTokProfileResponse(
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=0,
        )


async def _scroll_for_videos(
    page: Any, all_videos: List[Dict[str, Any]], limit: int,
) -> List[Dict[str, Any]]:
    """Scroll the profile page until ``limit`` videos load (or attempts run out)."""
    if len(all_videos) >= limit:
        return all_videos
    attempts = min((limit - len(all_videos)) // 6 + 2, 8)
    for i in range(attempts):
        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(1000)
        data = await _ex.extract_sigi_state_data(page)
        new_videos = data.get("videos", [])
        if len(new_videos) > len(all_videos):
            all_videos = new_videos
            logger.debug("[TikTok] %d videos after scroll %d", len(all_videos), i + 1)
    return all_videos


def _video_common_fields(v: Dict[str, Any]) -> Dict[str, Any]:
    """Shared fields between TikTokVideo and TikTokVideoDetail."""
    return {
        "video_id": v.get("video_id", ""),
        "caption": v.get("caption", ""),
        "views": v.get("views", ""),
        "likes": v.get("likes", ""),
        "comments": v.get("comments", ""),
        "shares": v.get("shares", ""),
        "post_date": v.get("post_date", ""),
        "duration": v.get("duration", ""),
        "hashtags": v.get("hashtags", []),
    }


def _video_from_dict(v: Dict[str, Any], username: str) -> TikTokVideo:
    return TikTokVideo(
        url=f"https://www.tiktok.com/@{username}/video/{v.get('video_id', '')}",
        **_video_common_fields(v),
    )


async def scrape_videos(
    username: str, limit: int, timeout_ms: int,
) -> TikTokVideosResponse:
    """Scrape ``limit`` videos for ``username``; auto-scroll to load more."""
    url = f"https://www.tiktok.com/@{quote(username)}"
    try:
        async with _open_session() as session:
            page = session.page
            logger.info("[TikTok] scraping videos for @%s, limit=%d", username, limit)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(3000)

            data = await _ex.extract_sigi_state_data(page)
            all_videos = await _scroll_for_videos(
                page, data.get("videos", []), limit,
            )
            videos = [_video_from_dict(v, username) for v in all_videos[:limit]]
            elapsed = _elapsed_ms(session.start_time)
            logger.info("[TikTok] scraped %d videos from @%s", len(videos), username)
            return TikTokVideosResponse(
                success=True,
                username=username,
                videos=videos,
                count=len(videos),
                elapsed_ms=elapsed,
            )
    except _ex.SCRAPE_EXCEPTIONS as exc:
        logger.error("[TikTok] error scraping videos for @%s: %s", username, exc)
        return TikTokVideosResponse(
            success=False,
            username=username,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=0,
        )


def _resolve_video_target(
    url: Optional[str], video_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(target_url, video_id)`` from one or both inputs, or (None, None)."""
    if url and not video_id:
        return url, _ex.extract_video_id_from_url(url)
    if url:
        return url, video_id
    if video_id:
        return f"https://www.tiktok.com/video/{video_id}", video_id
    return None, None


def _video_detail_from_dict(
    v: Dict[str, Any], target_url: str, video_id: Optional[str],
) -> TikTokVideoDetail:
    fields = _video_common_fields(v)
    fields["video_id"] = v.get("video_id") or video_id
    fields["caption"] = v.get("caption", "")[:150]
    return TikTokVideoDetail(
        url=target_url,
        full_caption=v.get("caption", ""),
        thumbnail_url=v.get("cover_url"),
        music=None,
        author=None,
        **fields,
    )


async def scrape_video(
    url: Optional[str], video_id: Optional[str], timeout_ms: int,
) -> TikTokVideoResponse:
    """Scrape a single TikTok video (either by URL or video_id)."""
    target_url, resolved_id = _resolve_video_target(url, video_id)
    if not target_url:
        return TikTokVideoResponse(
            success=False,
            error="Either url or video_id must be provided",
            elapsed_ms=0,
        )

    try:
        async with _open_session() as session:
            page = session.page
            logger.info("[TikTok] scraping video: %s", target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(3000)

            data = await _ex.extract_sigi_state_data(page)
            videos = data.get("videos", [])
            if not videos:
                return TikTokVideoResponse(
                    success=False,
                    error="Could not extract video data from SIGI_STATE",
                    elapsed_ms=_elapsed_ms(session.start_time),
                )
            video_data = next(
                (v for v in videos if v.get("video_id") == resolved_id),
                videos[0],
            )
            return TikTokVideoResponse(
                success=True,
                video=_video_detail_from_dict(video_data, target_url, resolved_id),
                elapsed_ms=_elapsed_ms(session.start_time),
            )
    except _ex.SCRAPE_EXCEPTIONS as exc:
        logger.error("[TikTok] error scraping video %s: %s", resolved_id or url, exc)
        return TikTokVideoResponse(
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=0,
        )


async def scrape_trending(limit: int, timeout_ms: int) -> TikTokTrendingResponse:
    """Scrape trending hashtags / sounds from the Discover page."""
    try:
        async with _open_session() as session:
            page = session.page
            logger.info("[TikTok] scraping trending page")
            await page.goto(
                "https://www.tiktok.com/discover",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            await page.wait_for_timeout(3000)
            items_data = await _ex.extract_trending_items(page, limit)
            items = [TikTokTrendingItem(**item) for item in items_data[:limit]]
            return TikTokTrendingResponse(
                success=True,
                items=items,
                count=len(items),
                elapsed_ms=_elapsed_ms(session.start_time),
            )
    except _ex.SCRAPE_EXCEPTIONS as exc:
        logger.error("[TikTok] error scraping trending: %s", exc)
        return TikTokTrendingResponse(
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=0,
        )
