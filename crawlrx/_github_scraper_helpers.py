"""Internal helpers for :mod:`github_scraper` (module split).

Shared HTTP helpers + shared error-response factory so the per-resource
scrape functions don't duplicate the same try/except shape.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import httpx

logger = logging.getLogger("crawlrx.github")

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "CrawlRX/1.0"

# Narrow exception families used by every GitHub helper. These cover the
# realistic httpx + json + parsing failures without sliding into bare
# ``except Exception``.
GITHUB_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError, httpx.RequestError, httpx.TimeoutException,
    OSError, ValueError, KeyError, TypeError, AttributeError,
    UnicodeDecodeError,
)

T = TypeVar("T")


def get_headers() -> Dict[str, str]:
    """Build the default headers for GitHub API requests."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github.v3+json",
    }


async def fetch_json(url: str, *, params: Dict[str, Any] | None = None) -> "dict[str, Any] | list[Any] | None":
    """GET ``url`` and return the parsed JSON body, raising on HTTP error."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=get_headers(), params=params)
        response.raise_for_status()
        return response.json()


async def fetch_readme_text(owner: str, repo: str) -> str:
    """Return the first 500 chars of a repository README, or ``""``."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/readme"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=get_headers())
            if response.status_code != 200:
                return ""
            data = response.json()
    except GITHUB_FETCH_EXCEPTIONS as e:
        logger.warning("Failed to fetch README for %s/%s: %s", owner, repo, e)
        return ""

    content = data.get("content", "")
    if not content:
        return ""
    try:
        decoded = base64.b64decode(content.replace("\n", "")).decode(
            "utf-8", errors="ignore",
        )
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("README decode failed for %s/%s: %s", owner, repo, e)
        return ""
    return decoded[:500]


def _parse_last_page(link_header: str) -> int | None:
    """Extract the ``page=N`` from a Link header rel=last entry."""
    if 'rel="last"' not in link_header:
        return None
    match = re.search(r'page=(\d+)[^>]*rel="last"', link_header)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def fetch_contributors_count(owner: str, repo: str) -> int:
    """Return the number of contributors for a repository."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contributors?per_page=1"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=get_headers())
    except GITHUB_FETCH_EXCEPTIONS as e:
        logger.warning(
            "Failed to fetch contributors for %s/%s: %s", owner, repo, e,
        )
        return 0
    if response.status_code != 200:
        return 0
    last_page = _parse_last_page(response.headers.get("link", ""))
    if last_page is not None:
        return last_page
    try:
        return len(response.json())
    except (ValueError, TypeError):
        return 0


@dataclass(frozen=True)
class ErrorResponseSpec:
    """Bundle of metadata needed to render an HTTP failure response."""

    factory: Callable[..., Any]
    log_label: str
    elapsed_start: float
    not_found_msg: Optional[str] = None


async def run_with_error_response(
    op: Callable[[], Awaitable[T]],
    spec: ErrorResponseSpec,
) -> T:
    """Run ``op``; on httpx/parse error build a failure response via ``spec.factory``."""
    try:
        return await op()
    except httpx.HTTPStatusError as e:
        elapsed_ms = int((time.time() - spec.elapsed_start) * 1000)
        error_msg = f"HTTP {e.response.status_code}: {e}"
        if e.response.status_code == 404 and spec.not_found_msg:
            error_msg = spec.not_found_msg
        logger.error("%s failed: %s", spec.log_label, error_msg)
        return spec.factory(success=False, error=error_msg, elapsed_ms=elapsed_ms)
    except GITHUB_FETCH_EXCEPTIONS as e:
        elapsed_ms = int((time.time() - spec.elapsed_start) * 1000)
        logger.error("%s error: %s", spec.log_label, e)
        return spec.factory(success=False, error=str(e), elapsed_ms=elapsed_ms)


__all__ = [
    "ErrorResponseSpec",
    "GITHUB_API_BASE",
    "GITHUB_FETCH_EXCEPTIONS",
    "USER_AGENT",
    "fetch_contributors_count",
    "fetch_json",
    "fetch_readme_text",
    "get_headers",
    "run_with_error_response",
]
