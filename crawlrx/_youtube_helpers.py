"""YouTube scraper helpers (sibling module).

Holds narrow exception tuples, URL parsing, oEmbed fallback, and the
:func:`safe_text_extract` helper used in place of bare ``except Exception``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

import httpx
from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger("crawlrx.youtube.helpers")

# Narrow exception families. Replace every bare `except Exception` in
# :mod:`crawlrx.youtube` with one of these tuples.
PLAYWRIGHT_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    PlaywrightError,
    PlaywrightTimeoutError,
    OSError,
    TimeoutError,
    RuntimeError,
)

PARSE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
)

ELEMENT_PROBE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + PARSE_EXCEPTIONS
)

OEMBED_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.TimeoutException,
    httpx.ConnectError,
    OSError,
    ValueError,
    KeyError,
)


_VIDEO_ID_PATTERNS: Tuple[str, ...] = (
    r"[?&]v=([a-zA-Z0-9_-]{11})",
    r"youtu\.be/([a-zA-Z0-9_-]{11})",
    r"/shorts/([a-zA-Z0-9_-]{11})",
    r"/embed/([a-zA-Z0-9_-]{11})",
)


def detect_url_type(url: str) -> Optional[str]:
    """Return ``"channel"``, ``"video"``, or None for a YouTube URL."""
    url_lower = url.lower()
    if "/channel/" in url_lower or "/@" in url_lower or "/c/" in url_lower:
        return "channel"
    if (
        "/watch?v=" in url_lower
        or "/shorts/" in url_lower
        or "/youtu.be/" in url_lower
    ):
        return "video"
    return None


def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-char video ID from any of the supported URL forms."""
    for pattern in _VIDEO_ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def try_oembed(url: str) -> Optional[dict]:
    """YouTube oEmbed lookup; returns the trimmed dict or None."""
    oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(oembed_url)
            if response.status_code != 200:
                return None
            data = response.json()
            return {
                "title": data.get("title", ""),
                "channel": data.get("author_name", ""),
                "author_url": data.get("author_url", ""),
            }
    except OEMBED_EXCEPTIONS as exc:
        logger.debug("oEmbed failed (%s): %s", type(exc).__name__, exc)
        return None


_T = TypeVar("_T")


async def safe_text_extract(
    fn: Callable[[], Awaitable[Any]],
    *,
    field: str,
    default: str = "",
    log: Optional[logging.Logger] = None,
) -> str:
    """Run a Playwright extraction; return default on the documented errors."""
    log = log or logger
    try:
        result = await fn()
        if result is None:
            return default
        return str(result)
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        log.debug("safe_text_extract %s suppressed %s: %s", field, type(exc).__name__, exc)
        return default


async def safe_query(
    fn: Callable[[], Awaitable[_T]],
    *,
    field: str,
    log: Optional[logging.Logger] = None,
) -> Optional[_T]:
    """Run a single Playwright query; return None on documented errors."""
    log = log or logger
    try:
        return await fn()
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        log.debug("safe_query %s suppressed %s: %s", field, type(exc).__name__, exc)
        return None


def parse_views_text(views_text: str) -> str:
    """Pull the numeric prefix out of a ``"123,456 views"``-shaped string."""
    match = re.search(r"([\d,]+)\s*views?", views_text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def parse_count_with_suffix(text: str, label: str) -> str:
    """Pull the count + optional K/M suffix out of an aria-label-shaped string."""
    pattern = rf"([\d,]+(?:\.\d+)?[KM]?)\s*(?:{label})"
    match = re.search(pattern, text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def is_published_marker(meta_text: str) -> bool:
    """True if the metadata text looks like a published-date snippet."""
    lower = (meta_text or "").lower()
    return any(word in lower for word in ("ago", "hour", "day", "week", "month", "year"))
