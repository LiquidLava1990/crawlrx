"""github_scraper.py -- GitHub scraper.

Endpoints: POST /scrape/github/repo
           POST /scrape/github/user
           POST /scrape/github/search

Uses GitHub's FREE public API -- no auth required for public data.
Pure HTTP requests, no Playwright.
"""
# Browser: optional CDP port 9223 (see CRAWLRX_CDP_PORTS)
import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._github_scraper_helpers import (
    ErrorResponseSpec,
    GITHUB_API_BASE,
    GITHUB_FETCH_EXCEPTIONS,
    fetch_contributors_count,
    fetch_json,
    fetch_readme_text,
    run_with_error_response,
)

logger = logging.getLogger("crawlrx.github")
router = APIRouter(prefix="/scrape/github", tags=["github"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class GitHubRepoRequest(BaseModel):
    """Request model for POST /scrape/github/repo"""
    owner: str = Field(..., description="Repository owner username or org")
    repo: str = Field(..., description="Repository name")


class GitHubUserRequest(BaseModel):
    """Request model for POST /scrape/github/user"""
    username: str = Field(..., description="GitHub username or org name")


class GitHubSearchRequest(BaseModel):
    """Request model for POST /scrape/github/search"""
    query: str = Field(..., description="Search query")
    language: Optional[str] = Field(default=None, description="Filter by programming language")
    sort: str = Field(default="stars", description="Sort by: stars, forks, updated")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results to return")


class GitHubRepoData(BaseModel):
    """GitHub repository data"""
    name: str = Field(default="", description="Repository name")
    owner: str = Field(default="", description="Repository owner")
    description: Optional[str] = Field(default=None, description="Repository description")
    stars: int = Field(default=0, description="Star count")
    forks: int = Field(default=0, description="Fork count")
    watchers: int = Field(default=0, description="Watcher count")
    open_issues: int = Field(default=0, description="Open issues count")
    language: Optional[str] = Field(default=None, description="Primary language")
    license: Optional[str] = Field(default=None, description="License name")
    topics: List[str] = Field(default_factory=list, description="Repository topics")
    last_commit: Optional[str] = Field(default=None, description="Last commit date (ISO)")
    created_at: Optional[str] = Field(default=None, description="Creation date (ISO)")
    readme_summary: Optional[str] = Field(default=None, description="First 500 chars of README")
    contributors_count: int = Field(default=0, description="Number of contributors")
    url: str = Field(default="", description="Repository URL")


class GitHubUserData(BaseModel):
    """GitHub user/org profile data"""
    username: str = Field(default="", description="GitHub username")
    name: Optional[str] = Field(default=None, description="Display name")
    bio: Optional[str] = Field(default=None, description="Profile bio")
    followers: int = Field(default=0, description="Follower count")
    following: int = Field(default=0, description="Following count")
    public_repos: int = Field(default=0, description="Public repository count")
    location: Optional[str] = Field(default=None, description="Location")
    website: Optional[str] = Field(default=None, description="Website/blog URL")
    company: Optional[str] = Field(default=None, description="Company/organization")
    avatar_url: Optional[str] = Field(default=None, description="Avatar image URL")
    top_repos: List[dict] = Field(default_factory=list, description="Top 5 repos by stars")


class GitHubSearchResult(BaseModel):
    """GitHub search result item"""
    name: str = Field(..., description="Repository name")
    owner: str = Field(..., description="Repository owner")
    stars: int = Field(default=0, description="Star count")
    forks: int = Field(default=0, description="Fork count")
    language: Optional[str] = Field(default=None, description="Primary language")
    description: Optional[str] = Field(default=None, description="Repository description")
    url: str = Field(..., description="Repository URL")
    last_updated: Optional[str] = Field(default=None, description="Last updated date (ISO)")


class GitHubRepoResponse(BaseModel):
    """Response model for POST /scrape/github/repo"""
    success: bool = Field(..., description="Whether scrape succeeded")
    repo: Optional[GitHubRepoData] = Field(default=None, description="Repository data")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class GitHubUserResponse(BaseModel):
    """Response model for POST /scrape/github/user"""
    success: bool = Field(..., description="Whether scrape succeeded")
    user: Optional[GitHubUserData] = Field(default=None, description="User data")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class GitHubSearchResponse(BaseModel):
    """Response model for POST /scrape/github/search"""
    success: bool = Field(..., description="Whether search succeeded")
    repos: List[GitHubSearchResult] = Field(default_factory=list, description="Search results")
    count: int = Field(default=0, description="Number of results")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# GitHub API Client
# ─────────────────────────────────────────────

async def _fetch_repo_data(owner: str, repo: str) -> dict:
    """Fetch repository data from GitHub API."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    logger.info("Fetching repo data: %s/%s", owner, repo)
    return await fetch_json(url)


async def _fetch_user_data(username: str) -> dict:
    """Fetch user profile data from GitHub API."""
    url = f"{GITHUB_API_BASE}/users/{username}"
    logger.info("Fetching user data: %s", username)
    return await fetch_json(url)


async def _fetch_user_repos(username: str, limit: int = 5) -> List[dict]:
    """Fetch user's top repositories by stars."""
    url = (
        f"{GITHUB_API_BASE}/users/{username}/repos"
        f"?sort=stars&direction=desc&per_page={limit}"
    )
    logger.info("Fetching top repos for user: %s", username)
    try:
        repos = await fetch_json(url)
    except GITHUB_FETCH_EXCEPTIONS as e:
        logger.warning("Failed to fetch repos for %s: %s", username, e)
        return []
    return [
        {
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "language": r.get("language"),
            "url": r.get("html_url", ""),
        }
        for r in repos
    ]


async def _search_repos(
    query: str,
    language: Optional[str],
    sort: str,
    limit: int,
) -> List[dict]:
    """Search repositories on GitHub."""
    search_query = f"{query} language:{language}" if language else query
    url = f"{GITHUB_API_BASE}/search/repositories"
    params = {
        "q": search_query,
        "sort": sort,
        "order": "desc",
        "per_page": min(limit, 50),
    }
    logger.info(
        "Searching repos: q='%s', sort=%s, limit=%d", search_query, sort, limit,
    )
    data = await fetch_json(url, params=params)
    return data.get("items", [])


# ─────────────────────────────────────────────
# Builders + scrape orchestration
# ─────────────────────────────────────────────

def _build_repo_data(repo_data: dict, readme_summary: str, contributors: int) -> GitHubRepoData:
    """Translate GitHub API repo payload into a typed model."""
    license_block = repo_data.get("license") or {}
    return GitHubRepoData(
        name=repo_data.get("name", ""),
        owner=repo_data.get("owner", {}).get("login", ""),
        description=repo_data.get("description"),
        stars=repo_data.get("stargazers_count", 0),
        forks=repo_data.get("forks_count", 0),
        watchers=repo_data.get("watchers_count", 0),
        open_issues=repo_data.get("open_issues_count", 0),
        language=repo_data.get("language"),
        license=license_block.get("name"),
        topics=repo_data.get("topics", []),
        last_commit=repo_data.get("pushed_at"),
        created_at=repo_data.get("created_at"),
        readme_summary=readme_summary or None,
        contributors_count=contributors,
        url=repo_data.get("html_url", ""),
    )


def _build_user_data(
    username: str,
    user_data: dict,
    top_repos: List[dict],
) -> GitHubUserData:
    """Translate GitHub API user payload into a typed model."""
    return GitHubUserData(
        username=user_data.get("login", username),
        name=user_data.get("name"),
        bio=user_data.get("bio"),
        followers=user_data.get("followers", 0),
        following=user_data.get("following", 0),
        public_repos=user_data.get("public_repos", 0),
        location=user_data.get("location"),
        website=user_data.get("blog") or None,
        company=user_data.get("company"),
        avatar_url=user_data.get("avatar_url"),
        top_repos=top_repos,
    )


async def _scrape_repo_op(owner: str, repo: str, start_time: float) -> GitHubRepoResponse:
    """Inner repo-scrape sequence; raises on failure."""
    repo_data = await _fetch_repo_data(owner, repo)
    readme_summary = await fetch_readme_text(owner, repo)
    contributors = await fetch_contributors_count(owner, repo)
    elapsed_ms = int((time.time() - start_time) * 1000)
    repo_model = _build_repo_data(repo_data, readme_summary, contributors)
    logger.info(
        "Scraped repo %s/%s: %d stars, %d forks",
        owner, repo_model.name, repo_model.stars, repo_model.forks,
    )
    return GitHubRepoResponse(success=True, repo=repo_model, elapsed_ms=elapsed_ms)


async def _do_scrape_repo(owner: str, repo: str) -> GitHubRepoResponse:
    """Repository scrape (single coroutine)."""
    start_time = time.time()
    spec = ErrorResponseSpec(
        factory=GitHubRepoResponse,
        log_label=f"repo {owner}/{repo}",
        elapsed_start=start_time,
        not_found_msg=f"Repository '{owner}/{repo}' not found",
    )
    return await run_with_error_response(
        lambda: _scrape_repo_op(owner, repo, start_time), spec,
    )


async def _scrape_user_op(username: str, start_time: float) -> GitHubUserResponse:
    """Inner user-scrape sequence; raises on failure."""
    user_data = await _fetch_user_data(username)
    top_repos = await _fetch_user_repos(username, limit=5)
    elapsed_ms = int((time.time() - start_time) * 1000)
    user_model = _build_user_data(username, user_data, top_repos)
    logger.info(
        "Scraped user %s: %d followers, %d repos",
        username, user_model.followers, user_model.public_repos,
    )
    return GitHubUserResponse(success=True, user=user_model, elapsed_ms=elapsed_ms)


async def _do_scrape_user(username: str) -> GitHubUserResponse:
    """User profile scrape (single coroutine)."""
    start_time = time.time()
    spec = ErrorResponseSpec(
        factory=GitHubUserResponse,
        log_label=f"user {username}",
        elapsed_start=start_time,
        not_found_msg=f"User '{username}' not found",
    )
    return await run_with_error_response(
        lambda: _scrape_user_op(username, start_time), spec,
    )


def _build_search_results(items: List[dict], limit: int) -> List[GitHubSearchResult]:
    """Translate raw search items into typed results."""
    return [
        GitHubSearchResult(
            name=item.get("name", ""),
            owner=item.get("owner", {}).get("login", ""),
            stars=item.get("stargazers_count", 0),
            forks=item.get("forks_count", 0),
            language=item.get("language"),
            description=item.get("description"),
            url=item.get("html_url", ""),
            last_updated=item.get("updated_at"),
        )
        for item in items[:limit]
    ]


async def _search_repos_op(
    query: str,
    language: Optional[str],
    sort: str,
    limit: int,
    start_time: float,
) -> GitHubSearchResponse:
    """Inner search sequence; raises on failure."""
    items = await _search_repos(query, language, sort, limit)
    repos = _build_search_results(items, limit)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info("Search found %d repos for query '%s'", len(repos), query)
    return GitHubSearchResponse(
        success=True, repos=repos, count=len(repos), elapsed_ms=elapsed_ms,
    )


async def _do_search_repos(
    query: str,
    language: Optional[str],
    sort: str,
    limit: int,
) -> GitHubSearchResponse:
    """Repository search (single coroutine)."""
    start_time = time.time()
    if sort not in ("stars", "forks", "updated", ""):
        elapsed_ms = int((time.time() - start_time) * 1000)
        return GitHubSearchResponse(
            success=False,
            error="Invalid sort. Must be one of: stars, forks, updated",
            elapsed_ms=elapsed_ms,
        )
    spec = ErrorResponseSpec(
        factory=GitHubSearchResponse,
        log_label=f"search '{query}'",
        elapsed_start=start_time,
    )
    return await run_with_error_response(
        lambda: _search_repos_op(query, language, sort, limit, start_time), spec,
    )


# ─────────────────────────────────────────────
# Endpoint helpers
# ─────────────────────────────────────────────

async def _wrap_request(
    coro,
    failure_factory,
    label: str,
):
    """Wrap an inner do-fn in an asyncio.wait_for + narrow exception block."""
    try:
        return await asyncio.wait_for(coro, timeout=30)
    except asyncio.TimeoutError:
        logger.error("%s request timed out", label)
        return failure_factory(
            success=False, error=f"{label} request timed out", elapsed_ms=30000,
        )
    except GITHUB_FETCH_EXCEPTIONS as e:
        logger.error("%s request failed: %s", label, e)
        return failure_factory(
            success=False, error=f"Request failed: {e}", elapsed_ms=0,
        )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/repo", response_model=GitHubRepoResponse, summary="Scrape GitHub repository")
async def scrape_github_repo(request: GitHubRepoRequest) -> GitHubRepoResponse:
    """Scrape a GitHub repository for metadata."""
    return await _wrap_request(
        _do_scrape_repo(request.owner, request.repo),
        GitHubRepoResponse,
        "Repo",
    )


@router.post("/user", response_model=GitHubUserResponse, summary="Scrape GitHub user/org")
async def scrape_github_user(request: GitHubUserRequest) -> GitHubUserResponse:
    """Scrape a GitHub user or organization profile."""
    return await _wrap_request(
        _do_scrape_user(request.username),
        GitHubUserResponse,
        "User",
    )


@router.post("/search", response_model=GitHubSearchResponse, summary="Search GitHub repositories")
async def search_github_repos(request: GitHubSearchRequest) -> GitHubSearchResponse:
    """Search for repositories on GitHub."""
    return await _wrap_request(
        _do_search_repos(request.query, request.language, request.sort, request.limit),
        GitHubSearchResponse,
        "Search",
    )


@router.post("/", summary="GitHub scraper info")
async def github_base() -> dict:
    """Base endpoint -- returns available GitHub scraper routes."""
    return {
        "success": True,
        "message": "GitHub scraper endpoints",
        "endpoints": [
            {"path": "/scrape/github/repo", "method": "POST", "params": {"owner": "str", "repo": "str"}},
            {"path": "/scrape/github/user", "method": "POST", "params": {"username": "str"}},
            {
                "path": "/scrape/github/search",
                "method": "POST",
                "params": {
                    "query": "str",
                    "language": "str (opt)",
                    "sort": "str (opt)",
                    "limit": "int (opt)",
                },
            },
        ],
    }
