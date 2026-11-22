"""pinterest.py -- Pinterest scraper.

Endpoints:
  POST /scrape/pinterest/search  -- search Pinterest pins
  POST /scrape/pinterest/profile -- scrape a Pinterest profile
  POST /scrape/pinterest/board   -- scrape pins from a board

Uses Playwright CDP. Pinterest is JS-heavy. Stealth delays: 500-1500ms.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, List, Optional

from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._pinterest_helpers import (
    PINTEREST_BASE_URL,
    collect_board_pins,
    collect_boards,
    collect_pins,
    populate_profile_basics,
    populate_profile_counts,
    random_delay,
    scroll_grid,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.pinterest")
router = APIRouter(prefix="/scrape/pinterest", tags=["pinterest"])


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class PinterestPin(BaseModel):
    """Single Pinterest pin."""

    title: Optional[str] = Field(default=None, description="Pin title")
    description: Optional[str] = Field(default=None, description="Pin description")
    image_url: Optional[str] = Field(default=None, description="Pin image URL")
    pin_url: Optional[str] = Field(default=None, description="Pin URL")
    pinner_name: Optional[str] = Field(default=None, description="Pinner name")
    board_name: Optional[str] = Field(default=None, description="Board name")
    save_count: Optional[str] = Field(default=None, description="Number of saves")
    link_url: Optional[str] = Field(default=None, description="External link URL")


class PinterestBoard(BaseModel):
    """Pinterest board info."""

    name: str = Field(..., description="Board name")
    pin_count: Optional[int] = Field(default=None, description="Number of pins")


class PinterestProfile(BaseModel):
    """Pinterest profile data."""

    username: str = Field(..., description="Username")
    display_name: Optional[str] = Field(default=None, description="Display name")
    bio: Optional[str] = Field(default=None, description="Profile bio")
    followers: Optional[str] = Field(default=None, description="Followers count")
    following: Optional[str] = Field(default=None, description="Following count")
    monthly_views: Optional[str] = Field(default=None, description="Monthly views")
    boards: List[PinterestBoard] = Field(default_factory=list)
    pin_count: Optional[int] = Field(default=None, description="Total pins")


class PinterestSearchRequest(BaseModel):
    """Request model for POST /scrape/pinterest/search."""

    query: str = Field(..., description="Search query")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum results to return")


class PinterestProfileRequest(BaseModel):
    """Request model for POST /scrape/pinterest/profile."""

    username: str = Field(..., description="Pinterest username (without @)")


class PinterestBoardRequest(BaseModel):
    """Request model for POST /scrape/pinterest/board."""

    url: str = Field(..., description="Pinterest board URL")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum pins to return")


class PinterestSearchResponse(BaseModel):
    """Response model for POST /scrape/pinterest/search."""

    success: bool
    pins: List[PinterestPin] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class PinterestProfileResponse(BaseModel):
    """Response model for POST /scrape/pinterest/profile."""

    success: bool
    profile: Optional[PinterestProfile] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


class PinterestBoardResponse(BaseModel):
    """Response model for POST /scrape/pinterest/board."""

    success: bool
    board_name: Optional[str] = None
    description: Optional[str] = None
    follower_count: Optional[str] = None
    pins: List[PinterestPin] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

async def _wait_for_pins(page: Any) -> None:
    """Soft-wait for at least one pin card to render."""
    try:
        await page.wait_for_selector(
            "[data-test-id='pin'], .Pin", timeout=15000
        )
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Pinterest pins")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal


async def _run_in_stealth_page(url: str, navigate: Any) -> Any:
    """Open a stealth Playwright page, navigate to ``url``, run ``navigate(page)``."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Pinterest] Navigating to: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return await navigate(page)
    finally:
        await close_stealth_context(browser, ctx, page)


async def _navigate_search(page: Any, limit: int) -> List[PinterestPin]:
    """Navigate-and-extract closure for the pins search endpoint."""
    await random_delay(1000, 2000)
    await _wait_for_pins(page)
    await scroll_grid(page, repeats=5)
    return await collect_pins(page, limit, PinterestPin)


async def _search_pins(
    request: PinterestSearchRequest,
) -> PinterestSearchResponse:
    """Search Pinterest pins using Playwright CDP."""
    start = timer_ms()
    search_url = (
        f"{PINTEREST_BASE_URL}/search/pins/"
        f"?q={urllib.parse.quote_plus(request.query)}"
    )
    try:
        pins = await _run_in_stealth_page(
            search_url, lambda page: _navigate_search(page, request.limit)
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Pinterest search error: %s", exc)
        return PinterestSearchResponse(
            success=False, error=str(exc), elapsed_ms=elapsed,
        )
    elapsed = int(timer_ms() - start)
    logger.info(
        "Pinterest search returned %d pins in %dms", len(pins), elapsed
    )
    return PinterestSearchResponse(
        success=True, pins=pins, count=len(pins), elapsed_ms=elapsed,
    )


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

async def _wait_for_profile(page: Any) -> None:
    """Soft-wait for the profile name to render."""
    try:
        await page.wait_for_selector(
            "h1, [data-test-id='profile-name']", timeout=15000
        )
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Pinterest profile")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal


async def _navigate_profile(page: Any, username: str) -> PinterestProfile:
    """Navigate-and-extract closure for the profile endpoint."""
    await random_delay(1500, 2500)
    await _wait_for_profile(page)
    profile = PinterestProfile(username=username)
    await populate_profile_basics(page, profile)
    await populate_profile_counts(page, profile)
    profile.boards = await collect_boards(page, PinterestBoard)
    return profile


async def _scrape_profile(username: str) -> Optional[PinterestProfile]:
    """Scrape a Pinterest profile using Playwright CDP."""
    profile_url = f"{PINTEREST_BASE_URL}/{username}/"
    try:
        return await _run_in_stealth_page(
            profile_url, lambda page: _navigate_profile(page, username)
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Pinterest profile scrape error: %s", exc)
        return None


# --------------------------------------------------------------------------
# Board
# --------------------------------------------------------------------------

async def _board_header(page: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(board_name, description, follower_count)`` from header DOM."""
    name_el = await page.query_selector("h1") or await page.query_selector(
        "[data-test-id='board-name']"
    )
    name = (await name_el.inner_text()).strip() if name_el else None

    desc_el = await page.query_selector("[data-test-id='board-description']")
    description = (await desc_el.inner_text()).strip() if desc_el else None

    follower_el = await page.query_selector("text=/follower/i")
    follower_count = None
    if follower_el is not None:
        from ._pinterest_helpers import parse_count
        follower_count = parse_count((await follower_el.inner_text()).strip())
    return name, description, follower_count


async def _navigate_board(page: Any, limit: int) -> tuple:
    """Navigate-and-extract closure for the board endpoint."""
    await random_delay(1500, 2500)
    await _wait_for_pins(page)
    board_name, description, follower_count = await _board_header(page)
    await scroll_grid(page, repeats=5)
    pins = await collect_board_pins(page, limit, PinterestPin, board_name)
    return board_name, description, follower_count, pins


async def _scrape_board(url: str, limit: int) -> PinterestBoardResponse:
    """Scrape pins from a Pinterest board using Playwright CDP."""
    start = timer_ms()
    try:
        board_name, description, follower_count, pins = await _run_in_stealth_page(
            url, lambda page: _navigate_board(page, limit)
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Pinterest board scrape error: %s", exc)
        return PinterestBoardResponse(
            success=False, error=str(exc), elapsed_ms=elapsed,
        )
    elapsed = int(timer_ms() - start)
    logger.info(
        "Pinterest board scrape returned %d pins in %dms", len(pins), elapsed
    )
    return PinterestBoardResponse(
        success=True,
        board_name=board_name,
        description=description,
        follower_count=follower_count,
        pins=pins,
        count=len(pins),
        elapsed_ms=elapsed,
    )


# --------------------------------------------------------------------------
# API endpoints
# --------------------------------------------------------------------------

@router.post("/search", response_model=PinterestSearchResponse)
async def pinterest_search(
    request: PinterestSearchRequest,
) -> PinterestSearchResponse:
    """Search Pinterest pins."""
    return await _search_pins(request)


@router.post("/profile", response_model=PinterestProfileResponse)
async def pinterest_profile(
    request: PinterestProfileRequest,
) -> PinterestProfileResponse:
    """Scrape a Pinterest profile."""
    start = timer_ms()
    profile = await _scrape_profile(request.username)
    elapsed = int(timer_ms() - start)
    if profile:
        return PinterestProfileResponse(
            success=True, profile=profile, elapsed_ms=elapsed,
        )
    return PinterestProfileResponse(
        success=False,
        error="Failed to scrape profile",
        elapsed_ms=elapsed,
    )


@router.post("/board", response_model=PinterestBoardResponse)
async def pinterest_board(
    request: PinterestBoardRequest,
) -> PinterestBoardResponse:
    """Scrape pins from a Pinterest board."""
    return await _scrape_board(request.url, request.limit)
