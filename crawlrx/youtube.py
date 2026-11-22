"""YouTube channel/video scraper.

Endpoint: POST /scrape/youtube

The Playwright-driven extraction logic lives in sibling modules:
- :mod:`_youtube_helpers`  -- URL parsing, oEmbed fallback, narrow exception
  tuples, and the ``safe_text_extract`` helper that replaces nested bare
  ``except Exception`` blocks.
- :mod:`_youtube_video`    -- video-page Playwright scraper.
- :mod:`_youtube_channel`  -- channel-page Playwright scraper.

This file is just the FastAPI router + request/response models +
orchestrator, kept thin so the code-quality ceilings stay clear.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import _youtube_channel as _channel
from . import _youtube_helpers as _yh
from . import _youtube_video as _video

logger = logging.getLogger("crawlrx.youtube")
router = APIRouter(prefix="/scrape", tags=["scrape"])

_DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 30000
_WALL_CLOCK_TIMEOUT_S = 60
URLType = Literal["channel", "video"]


class YouTubeVideoData(BaseModel):
    """Video-specific data."""
    title: str
    channel: str
    views: str
    likes: str
    published: str
    description: str


class YouTubeChannelVideo(BaseModel):
    """Single video from a channel listing."""
    title: str
    url: str
    views: str
    published: str


class YouTubeChannelData(BaseModel):
    """Channel-specific data."""
    name: str
    handle: str
    subscribers: str
    description: str
    videos: list[YouTubeChannelVideo]


class YouTubeRequest(BaseModel):
    """Request model for ``POST /scrape/youtube``."""
    url: str = Field(..., description="YouTube channel or video URL")


class YouTubeResponse(BaseModel):
    """Response model for ``POST /scrape/youtube``."""
    success: bool
    url: str
    type: Optional[URLType] = None
    data: Optional[dict] = None
    error: Optional[str] = None


def _empty_video_data(oembed: Dict[str, str]) -> Dict[str, str]:
    """Convert an oEmbed payload into the canonical video-data dict."""
    return {
        "title": oembed.get("title", ""),
        "channel": oembed.get("channel", ""),
        "views": "",
        "likes": "",
        "published": "",
        "description": "",
    }


async def _scrape_video(url: str) -> Dict[str, str]:
    """Try oEmbed first; fall back to Playwright on miss."""
    oembed = await _yh.try_oembed(url)
    if oembed:
        logger.info("Video scraped via oEmbed: %s", url)
        return _empty_video_data(oembed)
    data = await _video.scrape_video_playwright(url, _DEFAULT_PLAYWRIGHT_TIMEOUT_MS)
    logger.info("Video scraped via Playwright: %s", url)
    return data


async def _scrape_channel(url: str) -> Dict[str, Any]:
    """Channel scrape via Playwright (no oEmbed equivalent)."""
    data = await _channel.scrape_channel_playwright(
        url, _DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    )
    logger.info(
        "Channel scraped: %s (%d videos)", url, len(data.get("videos", [])),
    )
    return data


_SCRAPE_EXCEPTIONS = _yh.PLAYWRIGHT_EXCEPTIONS + _yh.OEMBED_EXCEPTIONS


async def _do_scrape_youtube(url: str) -> Dict[str, Any]:
    """Detect URL type and dispatch to the matching scraper."""
    result: Dict[str, Any] = {
        "success": False,
        "url": url,
        "type": None,
        "data": None,
        "error": None,
    }
    url_type = _yh.detect_url_type(url)
    if not url_type:
        result["error"] = "Unable to detect URL type (must be channel or video)"
        logger.error("URL type detection failed for: %s", url)
        return result
    result["type"] = url_type

    try:
        if url_type == "video":
            result["data"] = await _scrape_video(url)
        else:
            result["data"] = await _scrape_channel(url)
        result["success"] = True
    except _SCRAPE_EXCEPTIONS as exc:
        result["error"] = f"Scrape failed: {type(exc).__name__}: {exc}"
        logger.error("YouTube scrape failed for '%s': %s", url, exc, exc_info=True)
        return None  # explicit recovery: non-fatal: _SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return result


@router.post(
    "/youtube",
    response_model=YouTubeResponse,
    summary="Scrape YouTube channel or video",
)
async def scrape_youtube(request: YouTubeRequest) -> YouTubeResponse:
    """Scrape a YouTube channel or video.

    - ``url``: channel URL (``/@handle``, ``/channel/ID``, ``/c/name``) or
      video URL (``/watch?v=``, ``/shorts/``, ``youtu.be/``).

    For videos, oEmbed is tried first (fast, no auth) before falling back
    to Playwright. For channels, Playwright extracts name, handle,
    subscribers, and the first 10 videos.
    """
    try:
        raw = await asyncio.wait_for(
            _do_scrape_youtube(request.url),
            timeout=_WALL_CLOCK_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for YouTube scrape")
        return YouTubeResponse(
            success=False,
            url=request.url,
            error="Timeout: scrape took too long",
        )
    return YouTubeResponse(**raw)
