"""
search_media.py -- Bing Images/News/Videos search endpoints
Endpoints: POST /search/images, POST /search/news, POST /search/videos

Uses headless Playwright (fresh browser per request, NOT CDP)
to search Bing and extract media results.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._search_media_extractors import extract_images, extract_news, extract_videos
from ._search_media_runner import MediaSearchSpec, run_with_wall_timeout

logger = logging.getLogger("crawlrx.search_media")
router = APIRouter(prefix="/search", tags=["search"])

_WALL_TIMEOUT_S = 60
_BROWSER_TIMEOUT_MS = 30000


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class ImageSearchRequest(BaseModel):
    """Request model for POST /search/images."""

    query: str = Field(..., description="Search query string")
    count: int = Field(default=10, ge=1, le=50, description="Number of results to return")


class ImageResult(BaseModel):
    """Single image result."""

    title: str = Field(..., description="Image title/alt text")
    url: str = Field(..., description="Source page URL")
    thumbnail: str = Field(..., description="Thumbnail image URL")


class ImageSearchResponse(BaseModel):
    """Response model for POST /search/images."""

    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original search query")
    engine: str = Field(default="bing", description="Search engine used")
    results: List[ImageResult] = Field(default_factory=list, description="Image results")
    count: int = Field(default=0, description="Number of results returned")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class NewsSearchRequest(BaseModel):
    """Request model for POST /search/news."""

    query: str = Field(..., description="Search query string")
    count: int = Field(default=10, ge=1, le=50, description="Number of results to return")


class NewsResult(BaseModel):
    """Single news result."""

    title: str = Field(..., description="News article title")
    url: str = Field(..., description="Article URL")
    snippet: str = Field(default="", description="Article snippet/description")
    published: str = Field(default="", description="Published date/time")


class NewsSearchResponse(BaseModel):
    """Response model for POST /search/news."""

    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original search query")
    engine: str = Field(default="bing", description="Search engine used")
    results: List[NewsResult] = Field(default_factory=list, description="News results")
    count: int = Field(default=0, description="Number of results returned")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class VideoSearchRequest(BaseModel):
    """Request model for POST /search/videos."""

    query: str = Field(..., description="Search query string")
    count: int = Field(default=10, ge=1, le=50, description="Number of results to return")


class VideoResult(BaseModel):
    """Single video result."""

    title: str = Field(..., description="Video title")
    url: str = Field(..., description="Video URL")
    thumbnail: str = Field(..., description="Thumbnail image URL")
    duration: str = Field(default="", description="Video duration")


class VideoSearchResponse(BaseModel):
    """Response model for POST /search/videos."""

    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original search query")
    engine: str = Field(default="bing", description="Search engine used")
    results: List[VideoResult] = Field(default_factory=list, description="Video results")
    count: int = Field(default=0, description="Number of results returned")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _drop_internal_keys(raw: dict) -> dict:
    return {k: v for k, v in raw.items() if k != "elapsed_ms"}


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@router.post(
    "/images", response_model=ImageSearchResponse, summary="Search Bing Images",
)
async def search_images(request: ImageSearchRequest) -> ImageSearchResponse:
    """Search Bing Images and extract results."""
    spec = MediaSearchSpec(
        query=request.query,
        count=request.count,
        timeout=_BROWSER_TIMEOUT_MS,
        section="images",
        extractor=extract_images,
        not_found_label="image",
    )
    raw = await run_with_wall_timeout(spec, _WALL_TIMEOUT_S)
    if raw.get("results"):
        raw["results"] = [ImageResult(**img) for img in raw["results"]]
    return ImageSearchResponse(**_drop_internal_keys(raw))


@router.post(
    "/news", response_model=NewsSearchResponse, summary="Search Bing News",
)
async def search_news(request: NewsSearchRequest) -> NewsSearchResponse:
    """Search Bing News and extract results."""
    spec = MediaSearchSpec(
        query=request.query,
        count=request.count,
        timeout=_BROWSER_TIMEOUT_MS,
        section="news",
        extractor=extract_news,
        not_found_label="news article",
    )
    raw = await run_with_wall_timeout(spec, _WALL_TIMEOUT_S)
    if raw.get("results"):
        raw["results"] = [NewsResult(**n) for n in raw["results"]]
    return NewsSearchResponse(**_drop_internal_keys(raw))


@router.post(
    "/videos", response_model=VideoSearchResponse, summary="Search Bing Videos",
)
async def search_videos(request: VideoSearchRequest) -> VideoSearchResponse:
    """Search Bing Videos and extract results."""
    spec = MediaSearchSpec(
        query=request.query,
        count=request.count,
        timeout=_BROWSER_TIMEOUT_MS,
        section="videos",
        extractor=extract_videos,
        not_found_label="video",
    )
    raw = await run_with_wall_timeout(spec, _WALL_TIMEOUT_S)
    if raw.get("results"):
        raw["results"] = [VideoResult(**v) for v in raw["results"]]
    return VideoSearchResponse(**_drop_internal_keys(raw))
