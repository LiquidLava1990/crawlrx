"""twitter.py -- Twitter/X Scraper.

Endpoints:
  POST /scrape/twitter/profile
  POST /scrape/twitter/tweets
  POST /scrape/twitter/search

Uses Playwright CDP to scrape Twitter/X profiles and tweets.
Browser already logged into X via CDP port 9223.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS
from ._twitter_helpers import (
    EndpointEnvelope,
    SCRAPE_BUBBLE_FAILURES,
    collect_tweets_via_scroll,
    elapsed_ms_since,
    extract_tweet_dict,
    navigate_with_pacing,
    parse_count,
    random_delay,
    run_with_endpoint_envelope,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.twitter")
router = APIRouter(prefix="/scrape/twitter", tags=["twitter"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class TwitterProfileRequest(BaseModel):
    """Request model for POST /scrape/twitter/profile."""
    username: str = Field(..., description="Twitter/X username (without @)")
    timeout: int = Field(default=30000, ge=10000, le=60000)


class TwitterTweetsRequest(BaseModel):
    """Request model for POST /scrape/twitter/tweets."""
    username: str = Field(..., description="Twitter/X username (without @)")
    limit: int = Field(default=20, ge=1, le=50)
    timeout: int = Field(default=30000, ge=10000, le=60000)


class TwitterSearchRequest(BaseModel):
    """Request model for POST /scrape/twitter/search."""
    query: str = Field(..., description="Search query")
    limit: int = Field(default=20, ge=1, le=50)
    filter_type: str = Field(default="latest", description="latest|top")
    timeout: int = Field(default=30000, ge=10000, le=60000)


class TwitterTweet(BaseModel):
    """Single tweet model."""
    text: str = Field(default="")
    likes: str = Field(default="")
    retweets: str = Field(default="")
    replies: str = Field(default="")
    views: str = Field(default="")
    posted_at: str = Field(default="")
    tweet_url: str = Field(default="")
    is_retweet: bool = Field(default=False)
    media_urls: List[str] = Field(default_factory=list)


class TwitterProfile(BaseModel):
    """Twitter profile data model."""
    username: str = Field(...)
    display_name: str = Field(default="")
    bio: str = Field(default="")
    followers: str = Field(default="")
    following: str = Field(default="")
    tweets_count: str = Field(default="")
    verified: bool = Field(default=False)
    joined_date: str = Field(default="")
    location: str = Field(default="")
    website: str = Field(default="")
    pinned_tweet: Optional[TwitterTweet] = Field(default=None)


class TwitterProfileResponse(BaseModel):
    """Response model for profile scrape."""
    success: bool = Field(...)
    profile: Optional[TwitterProfile] = Field(default=None)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = Field(default=None)


class TwitterTweetsResponse(BaseModel):
    """Response model for tweets scrape."""
    success: bool = Field(...)
    tweets: List[TwitterTweet] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = Field(default=None)


class TwitterSearchResponse(BaseModel):
    """Response model for search scrape."""
    success: bool = Field(...)
    tweets: List[TwitterTweet] = Field(default_factory=list)
    count: int = Field(default=0)
    elapsed_ms: int = Field(default=0)
    error: Optional[str] = Field(default=None)


# ─────────────────────────────────────────────
# Tweet builder + profile JS payload
# ─────────────────────────────────────────────

def _make_tweet(data: dict) -> TwitterTweet:
    """Convert the raw tweet dict from the page-side JS into a model."""
    return TwitterTweet(**data)


_PROFILE_EXTRACT_JS = """
() => {
    const data = {
        display_name: '',
        bio: '',
        followers: '',
        following: '',
        tweets_count: '',
        verified: false,
        joined_date: '',
        location: '',
        website: ''
    };

    const nameEl = document.querySelector('[data-testid="UserName"]');
    if (nameEl) {
        const spans = nameEl.querySelectorAll('span');
        for (let span of spans) {
            if (span.textContent && !span.textContent.includes('@')) {
                data.display_name = span.textContent.trim();
                break;
            }
        }
    }

    const bioEl = document.querySelector('[data-testid="UserDescription"]');
    if (bioEl) data.bio = bioEl.textContent.trim();

    const statLinks = document.querySelectorAll('a[href*="/following"], a[href*="/followers"], a[href*="/verified_followers"]');
    statLinks.forEach(link => {
        const href = link.getAttribute('href') || '';
        const text = link.textContent || '';
        const match = text.match(/([\\d,.KM]+)/);
        const count = match ? match[1] : '';
        if (href.includes('/following') && !href.includes('ers')) {
            data.following = count;
        } else if (href.includes('/verified_followers')) {
            // ignore
        } else if (href.includes('/followers')) {
            data.followers = count;
        }
    });

    const tweetsNav = document.querySelector('nav[role="navigation"] a[href*="/"]');
    if (tweetsNav) {
        const text = tweetsNav.textContent || '';
        const match = text.match(/([\\d,.KM]+)/);
        if (match) data.tweets_count = match[1];
    }

    const verifiedSvg = document.querySelector('[data-testid="UserName"] svg[data-testid="icon-verified"]');
    if (verifiedSvg) data.verified = true;

    const joinedEl = document.querySelector('[data-testid="UserProfileHeader_Items"] span');
    if (joinedEl && joinedEl.textContent.includes('Joined')) {
        data.joined_date = joinedEl.textContent.trim();
    }

    const locationEl = document.querySelector('[data-testid="UserLocation"]');
    if (locationEl) data.location = locationEl.textContent.trim();

    const websiteEl = document.querySelector('[data-testid="UserUrl"]');
    if (websiteEl) {
        const link = websiteEl.querySelector('a');
        if (link) data.website = link.getAttribute('href') || link.textContent.trim();
    }

    return data;
}
"""


async def _extract_pinned_tweet(page: Any) -> Optional[TwitterTweet]:
    """Best-effort scrape of the pinned tweet, returning None on miss."""
    try:
        pinned_el = await page.query_selector(
            '[data-testid="tweet"]:has([data-testid="socialContext"])'
        )
        if not pinned_el:
            return None
        social_text = pinned_el.query_selector('[data-testid="socialContext"]')
        if social_text and "pinned" in social_text.inner_text().lower():
            return _make_tweet(extract_tweet_dict(pinned_el))
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("[Twitter] pinned tweet extract failed (%s): %s",
                     type(exc).__name__, exc)
    return None


def _build_profile(
    username: str,
    profile_data: dict,
    pinned: Optional[TwitterTweet],
) -> TwitterProfile:
    """Map the raw profile JS payload into the typed model."""
    return TwitterProfile(
        username=username,
        display_name=profile_data.get("display_name", ""),
        bio=profile_data.get("bio", ""),
        followers=parse_count(profile_data.get("followers", "")),
        following=parse_count(profile_data.get("following", "")),
        tweets_count=parse_count(profile_data.get("tweets_count", "")),
        verified=profile_data.get("verified", False),
        joined_date=profile_data.get("joined_date", ""),
        location=profile_data.get("location", ""),
        website=profile_data.get("website", ""),
        pinned_tweet=pinned,
    )


def _profile_unavailable_response(
    username: str,
    page_url: str,
    start_time: float,
) -> Optional[TwitterProfileResponse]:
    """Detect 'suspended' or 'login wall' redirect pages, else None."""
    if "account/suspended" in page_url or "page-not-found" in page_url:
        return TwitterProfileResponse(
            success=False,
            error=f"Account @{username} not found or suspended",
            elapsed_ms=elapsed_ms_since(start_time),
        )
    if "/login" in page_url or "/i/flow/login" in page_url:
        return TwitterProfileResponse(
            success=False,
            error="Login required - CDP browser not logged into Twitter",
            elapsed_ms=elapsed_ms_since(start_time),
        )
    return None


async def _do_scrape_profile(
    username: str,
    timeout_ms: int,
) -> TwitterProfileResponse:
    """Scrape a single profile via Playwright CDP."""
    start_time = time.time()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            url = f"https://twitter.com/{quote(username)}"
            await navigate_with_pacing(page, url, timeout_ms, "[Twitter]")
            unavailable = _profile_unavailable_response(
                username, page.url, start_time,
            )
            if unavailable is not None:
                return unavailable
            await random_delay()
            profile_data = await page.evaluate(_PROFILE_EXTRACT_JS)
            await random_delay()
            pinned = await _extract_pinned_tweet(page)
            elapsed = elapsed_ms_since(start_time)
            profile = _build_profile(username, profile_data, pinned)
            logger.info("[Twitter] Scraped profile @%s: %s, verified=%s",
                        username, profile.display_name, profile.verified)
            return TwitterProfileResponse(
                success=True, profile=profile, elapsed_ms=elapsed,
            )
    except SCRAPE_BUBBLE_FAILURES as exc:
        elapsed = elapsed_ms_since(start_time)
        logger.error("[Twitter] Error scraping @%s (%s): %s",
                     username, type(exc).__name__, exc)
        return TwitterProfileResponse(
            success=False, error=str(exc), elapsed_ms=elapsed,
        )
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Tweets / search shared body
# ─────────────────────────────────────────────

async def _scroll_and_collect(
    target_url: str,
    limit: int,
    timeout_ms: int,
    where: str,
) -> List[TwitterTweet]:
    """Open a stealth session, scroll, return collected tweets."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await navigate_with_pacing(page, target_url, timeout_ms, where)
            return await collect_tweets_via_scroll(page, limit, where, _make_tweet)
    finally:
        await close_stealth_context(browser, ctx, page)


def _tweets_response_from_collection(
    tweets: List[TwitterTweet],
    limit: int,
    elapsed: int,
    response_cls: Callable[..., Any],
) -> Any:
    """Slice + box ``tweets`` into a ``*Response`` (tweets or search)."""
    sliced = tweets[:limit]
    return response_cls(
        success=True, tweets=sliced, count=len(sliced), elapsed_ms=elapsed,
    )


async def _scroll_collect_response(
    target_url: str,
    limit: int,
    timeout_ms: int,
    where: str,
    response_cls: Callable[..., Any],
) -> Any:
    """Shared body for tweets/search: scroll, then box into a response."""
    start_time = time.time()
    try:
        tweets = await _scroll_and_collect(target_url, limit, timeout_ms, where)
    except SCRAPE_BUBBLE_FAILURES as exc:
        elapsed = elapsed_ms_since(start_time)
        logger.error("%s failed (%s): %s", where, type(exc).__name__, exc)
        return response_cls(success=False, error=str(exc), elapsed_ms=elapsed)
    elapsed = elapsed_ms_since(start_time)
    logger.info("%s collected %d tweets in %dms", where, len(tweets), elapsed)
    return _tweets_response_from_collection(tweets, limit, elapsed, response_cls)


async def _do_scrape_tweets(
    username: str,
    limit: int,
    timeout_ms: int,
) -> TwitterTweetsResponse:
    """Scrape recent tweets from a profile."""
    return await _scroll_collect_response(
        f"https://twitter.com/{quote(username)}",
        limit, timeout_ms,
        f"[Twitter] tweets @{username}",
        TwitterTweetsResponse,
    )


async def _do_search_tweets(
    query: str,
    limit: int,
    filter_type: str,
    timeout_ms: int,
) -> TwitterSearchResponse:
    """Run a tweet search."""
    return await _scroll_collect_response(
        f"https://twitter.com/search?q={quote(query)}&f={filter_type}",
        limit, timeout_ms,
        f"[Twitter] search '{query}'",
        TwitterSearchResponse,
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

def _profile_failure(error_message: str, request_timeout_ms: int) -> TwitterProfileResponse:
    """Failure-envelope factory for the /profile route."""
    return TwitterProfileResponse(
        success=False, error=error_message, elapsed_ms=request_timeout_ms,
    )


def _tweets_failure(error_message: str, request_timeout_ms: int) -> TwitterTweetsResponse:
    """Failure-envelope factory for the /tweets route."""
    return TwitterTweetsResponse(
        success=False, error=error_message, elapsed_ms=request_timeout_ms,
    )


def _search_failure(error_message: str, request_timeout_ms: int) -> TwitterSearchResponse:
    """Failure-envelope factory for the /search route."""
    return TwitterSearchResponse(
        success=False, error=error_message, elapsed_ms=request_timeout_ms,
    )


@router.post(
    "/profile", response_model=TwitterProfileResponse,
    summary="Scrape Twitter/X profile",
)
async def scrape_profile(request: TwitterProfileRequest) -> TwitterProfileResponse:
    """Scrape a Twitter/X profile page (requires logged-in CDP browser)."""
    return await run_with_endpoint_envelope(
        _do_scrape_profile(request.username, request.timeout),
        EndpointEnvelope(
            timeout_seconds=max(request.timeout // 1000, 10) + 5,
            failure_factory=_profile_failure,
            request_timeout_ms=request.timeout,
            where="profile",
        ),
    )


@router.post(
    "/tweets", response_model=TwitterTweetsResponse,
    summary="Get recent tweets from profile",
)
async def scrape_tweets(request: TwitterTweetsRequest) -> TwitterTweetsResponse:
    """Get recent tweets from a Twitter/X profile."""
    return await run_with_endpoint_envelope(
        _do_scrape_tweets(request.username, request.limit, request.timeout),
        EndpointEnvelope(
            timeout_seconds=max(request.timeout // 1000, 10) + 10,
            failure_factory=_tweets_failure,
            request_timeout_ms=request.timeout,
            where="tweets",
        ),
    )


@router.post(
    "/search", response_model=TwitterSearchResponse,
    summary="Search tweets by keyword",
)
async def search_tweets(request: TwitterSearchRequest) -> TwitterSearchResponse:
    """Search tweets by keyword query."""
    if request.filter_type not in ("latest", "top"):
        raise HTTPException(
            status_code=400, detail="filter_type must be 'latest' or 'top'",
        )
    return await run_with_endpoint_envelope(
        _do_search_tweets(
            request.query, request.limit, request.filter_type, request.timeout,
        ),
        EndpointEnvelope(
            timeout_seconds=max(request.timeout // 1000, 10) + 10,
            failure_factory=_search_failure,
            request_timeout_ms=request.timeout,
            where="search",
        ),
    )
