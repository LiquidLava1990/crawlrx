"""reddit.py -- Reddit posts + comments scraper.

Endpoints: POST /scrape/reddit
           POST /scrape/reddit/post

Uses Reddit's public JSON API -- no auth required.
Handles subreddit fetch, search, and per-post comment extraction.
Works with both old.reddit.com and new reddit (uses .json suffix).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ._scraper_helpers import PARSE_EXCEPTIONS
from .utils import timer_ms

logger = logging.getLogger("crawlrx.reddit")
reddit_router = APIRouter(prefix="/scrape", tags=["scrape"])

REDDIT_BASE = "https://www.reddit.com"
USER_AGENT = "CrawlRX/1.0 (scraper)"

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError, httpx.RequestError, httpx.TimeoutException, OSError,
)
_TOTAL_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *_NETWORK_EXCEPTIONS, *PARSE_EXCEPTIONS,
)


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class RedditComment(BaseModel):
    author: str = Field(default="", description="Comment author username")
    body: str = Field(default="", description="Comment text")
    score: int = Field(default=0, description="Upvote score")
    depth: int = Field(default=0, description="Nesting depth (0 = top-level)")


class RedditPost(BaseModel):
    title: str
    url: str
    author: str
    score: int
    comments: int
    subreddit: str
    published: str
    selftext: str
    permalink: str = Field(default="", description="Reddit permalink path")


class RedditRequest(BaseModel):
    subreddit: Optional[str] = Field(default=None, description="Subreddit (without r/) to fetch top posts")
    query: Optional[str] = Field(default=None, description="Search query")
    limit: int = Field(default=25, ge=1, le=100, description="Max posts to return")
    sort: str = Field(default="top", description="Sort: top|new|hot|rising (subreddit only)")
    timeframe: str = Field(default="week", description="Time range for top: hour|day|week|month|year|all")


class RedditResponse(BaseModel):
    success: bool
    source: str
    posts: List[RedditPost]
    count: int
    error: Optional[str] = None


class RedditPostRequest(BaseModel):
    url: str = Field(..., description="Reddit post URL (old or new reddit)")
    max_comments: int = Field(default=10, ge=1, le=50, description="Max top-level comments to return")


class RedditPostResponse(BaseModel):
    success: bool
    post: Optional[RedditPost] = None
    comments: List[RedditComment] = Field(default_factory=list)
    comment_count: int = Field(default=0)
    error: Optional[str] = None
    elapsed_ms: int = Field(default=0)


# ─────────────────────────────────────────────
# URL Normalization
# ─────────────────────────────────────────────

def _normalize_reddit_url(url: str) -> str:
    """Convert any Reddit URL to the JSON API equivalent."""
    url = url.strip()
    url = url.replace("old.reddit.com", "www.reddit.com")
    if url.startswith("https://reddit.com"):
        url = "https://www.reddit.com" + url[len("https://reddit.com"):]
    url = url.split("?")[0]
    url = url.rstrip("/")
    if not url.endswith(".json"):
        url = url + "/.json"
    return url


# ─────────────────────────────────────────────
# Post Parser
# ─────────────────────────────────────────────

def _parse_post(child: dict) -> RedditPost:
    """Build RedditPost from an API children item."""
    d = child.get("data", {})
    url = d.get("url", "")
    permalink = d.get("permalink", "")
    if permalink and not url.startswith("http"):
        url = REDDIT_BASE + permalink
    created_utc = d.get("created_utc", 0)
    published = (
        datetime.utcfromtimestamp(created_utc).isoformat() if created_utc else ""
    )
    return RedditPost(
        title=d.get("title", ""),
        url=url,
        author=d.get("author", ""),
        score=d.get("score", 0),
        comments=d.get("num_comments", 0),
        subreddit=d.get("subreddit", ""),
        published=published,
        selftext=d.get("selftext") or "",
        permalink=REDDIT_BASE + permalink if permalink else "",
    )


def _safe_parse_post(child: dict) -> Optional[RedditPost]:
    """Parse one Reddit child node into a post, returning None on bad shape."""
    try:
        return _parse_post(child)
    except PARSE_EXCEPTIONS as e:
        logger.warning("[Reddit] Failed to parse post: %s", e)
        # explicit recovery: PARSE_EXCEPTIONS non-fatal; caller skips child
        return None


def _extract_posts(data: dict) -> List[RedditPost]:
    """Extract posts from Reddit API listing response."""
    posts: List[RedditPost] = []
    for child in data.get("data", {}).get("children", []):
        post = _safe_parse_post(child)
        if post is not None:
            posts.append(post)
    return posts


# ─────────────────────────────────────────────
# Comment Parser
# ─────────────────────────────────────────────

def _is_comment_droppable(child: dict) -> bool:
    """Comment kinds we ignore (more-stubs + deleted/removed bodies)."""
    if child.get("kind") == "more":
        return True
    body = child.get("data", {}).get("body", "")
    return not body or body in ("[deleted]", "[removed]")


def _parse_comments(
    children: list,
    depth: int = 0,
    max_top: int = 10,
) -> List[RedditComment]:
    """Recursively extract comments. Collects up to max_top top-level comments."""
    comments: List[RedditComment] = []
    top_level_count = 0
    for child in children:
        if depth == 0 and top_level_count >= max_top:
            break
        if _is_comment_droppable(child):
            continue
        d = child.get("data", {})
        comments.append(RedditComment(
            author=d.get("author") or "",
            body=d.get("body", ""),
            score=d.get("score") or 0,
            depth=depth,
        ))
        if depth == 0:
            top_level_count += 1
        replies = d.get("replies", {})
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            if reply_children and depth < 2:
                comments.extend(
                    _parse_comments(reply_children, depth + 1, max_top),
                )
    return comments


# ─────────────────────────────────────────────
# API Calls (shared httpx helpers)
# ─────────────────────────────────────────────

def _get_reddit_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    follow_redirects: bool = False,
) -> Any:
    """GET ``url`` returning parsed JSON; raises on transport / parse error."""
    with httpx.Client(timeout=30.0, follow_redirects=follow_redirects) as client:
        resp = client.get(url, params=params, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()


def _fetch_subreddit(subreddit: str, limit: int, sort: str, timeframe: str) -> dict:
    """Fetch posts from a subreddit."""
    valid_sorts = ("top", "new", "hot", "rising")
    sort = sort if sort in valid_sorts else "top"
    url = f"{REDDIT_BASE}/r/{subreddit}/{sort}.json"
    params: Dict[str, Any] = {"limit": limit}
    if sort == "top":
        params["t"] = timeframe
    logger.info("[Reddit] Fetching r/%s/%s (limit=%d)", subreddit, sort, limit)
    return _get_reddit_json(url, params=params)


def _search_reddit(query: str, limit: int) -> dict:
    """Search Reddit for posts."""
    url = f"{REDDIT_BASE}/search.json"
    params = {"q": query, "limit": limit, "sort": "relevance"}
    logger.info("[Reddit] Searching for '%s' (limit=%d)", query, limit)
    return _get_reddit_json(url, params=params)


def _fetch_post_with_comments(url: str, max_comments: int) -> tuple:
    """Fetch a single post with its comments. Returns ``(post, comments)``."""
    json_url = _normalize_reddit_url(url)
    logger.info("[Reddit] Fetching post: %s", json_url)
    data = _get_reddit_json(json_url, follow_redirects=True)

    if not isinstance(data, list) or len(data) < 2:
        return None, []
    post_children = data[0].get("data", {}).get("children", [])
    post = _parse_post(post_children[0]) if post_children else None
    comment_children = data[1].get("data", {}).get("children", [])
    comments = _parse_comments(comment_children, depth=0, max_top=max_comments)
    return post, comments


def _scrape_error_response(
    source: str,
    err: BaseException,
    log_label: str,
) -> RedditResponse:
    """Map a fetch exception to a RedditResponse failure shape."""
    if isinstance(err, httpx.HTTPStatusError):
        logger.error("[Reddit] HTTP error %s: %s", log_label, err)
        return RedditResponse(
            success=False, source=source, posts=[], count=0,
            error="HTTP %d: %s" % (err.response.status_code, err),
        )
    if isinstance(err, httpx.RequestError):
        logger.error("[Reddit] Network error %s: %s", log_label, err)
        return RedditResponse(
            success=False, source=source, posts=[], count=0,
            error="Network error: %s" % err,
        )
    logger.error("[Reddit] Error %s: %s", log_label, err)
    return RedditResponse(
        success=False, source=source, posts=[], count=0, error=str(err),
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

def _resolve_listing_data(request: RedditRequest, source: str):
    """Fetch listing data for ``request`` -- returns either dict or RedditResponse."""
    try:
        if request.subreddit:
            return _fetch_subreddit(
                request.subreddit, request.limit, request.sort, request.timeframe,
            )
        return _search_reddit(request.query, request.limit)
    except _TOTAL_NETWORK_EXCEPTIONS as e:
        log_label = (
            f"for r/{request.subreddit}" if request.subreddit
            else f"searching '{request.query}'"
        )
        return _scrape_error_response(source, e, log_label)


@reddit_router.post(
    "/reddit",
    response_model=RedditResponse,
    summary="Scrape Reddit posts",
)
def scrape_reddit(request: RedditRequest) -> RedditResponse:
    """Fetch Reddit posts from a subreddit or search Reddit (FastAPI threadpool)."""
    start = timer_ms()
    if not request.subreddit and not request.query:
        raise HTTPException(
            status_code=400, detail="Either 'subreddit' or 'query' is required",
        )
    source = (
        "r/%s" % request.subreddit if request.subreddit
        else "search: %s" % request.query
    )
    fetch_result = _resolve_listing_data(request, source)
    if isinstance(fetch_result, RedditResponse):
        return fetch_result
    try:
        posts = _extract_posts(fetch_result)
    except PARSE_EXCEPTIONS as e:
        logger.error("[Reddit] Parse error: %s", e)
        return RedditResponse(
            success=False, source=source, posts=[], count=0,
            error="Parse error: %s" % e,
        )
    elapsed = int(timer_ms() - start)
    logger.info(
        "[Reddit] Fetched %d posts from %s in %dms", len(posts), source, elapsed,
    )
    return RedditResponse(
        success=True, source=source, posts=posts, count=len(posts),
    )


@reddit_router.post(
    "/reddit/post",
    response_model=RedditPostResponse,
    summary="Scrape Reddit post with top comments",
)
def scrape_reddit_post(request: RedditPostRequest) -> RedditPostResponse:
    """Fetch a single Reddit post with its top comments (FastAPI threadpool)."""
    start = timer_ms()
    try:
        post, comments = _fetch_post_with_comments(request.url, request.max_comments)
    except httpx.HTTPStatusError as e:
        elapsed = int(timer_ms() - start)
        logger.error("[Reddit] HTTP error fetching post %s: %s", request.url, e)
        return RedditPostResponse(
            success=False,
            error="HTTP %d: %s" % (e.response.status_code, e),
            elapsed_ms=elapsed,
        )
    except _TOTAL_NETWORK_EXCEPTIONS as e:
        elapsed = int(timer_ms() - start)
        logger.error("[Reddit] Error fetching post %s: %s", request.url, e)
        return RedditPostResponse(success=False, error=str(e), elapsed_ms=elapsed)

    elapsed = int(timer_ms() - start)
    if not post:
        return RedditPostResponse(
            success=False, error="Could not parse post from URL", elapsed_ms=elapsed,
        )
    logger.info(
        "[Reddit] Post fetched: '%s' -- %d comments extracted in %dms",
        post.title[:60], len(comments), elapsed,
    )
    return RedditPostResponse(
        success=True, post=post, comments=comments,
        comment_count=len(comments), elapsed_ms=elapsed,
    )
