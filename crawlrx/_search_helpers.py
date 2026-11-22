"""Search-module helpers (A refactor).

Shared helpers for ``search.py`` / ``search_deep.py`` / ``search_media.py`` /
``search_fusion.py`` / ``semrush.py``. Lives separate from
``_scraper_helpers.py`` (which is for product-scraper helpers) to keep both
files under the 500-line ceiling.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS

logger = logging.getLogger("crawlrx._search_helpers")


SEARCH_NET_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ConnectionError,
    TimeoutError,
    json.JSONDecodeError,
    UnicodeDecodeError,
)


SERP_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
    IndexError,
)


def _resolve_serp_browser_tuple() -> tuple[type[BaseException], ...]:
    base: list[type[BaseException]] = list(PLAYWRIGHT_EXCEPTIONS)
    base.extend(
        (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RuntimeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            IndexError,
        )
    )
    return tuple(base)


SERP_BROWSER_EXCEPTIONS: tuple[type[BaseException], ...] = (
    _resolve_serp_browser_tuple()
)


async def safe_close_resources(
    log: logging.Logger,
    *resources: tuple[Any, str],
) -> None:
    """Close (obj, label) pairs swallowing known close-time errors only."""
    for obj, label in resources:
        if obj is None:
            continue
        try:
            await obj.close()
        except SERP_BROWSER_EXCEPTIONS as close_err:
            log.debug("%s close error: %s", label, close_err)


@dataclass(frozen=True)
class DorkParams:
    """Bundled args for Google-dork query building."""

    site_filter: Optional[str] = None
    file_type: Optional[str] = None
    exact_phrase: Optional[str] = None
    exclude_terms: Optional[list[str]] = None
    inurl: Optional[str] = None
    intitle: Optional[str] = None


@dataclass(frozen=True)
class EngineSearchParams:
    """Per-engine search arg bundle."""

    num_results: int = 10
    timeout_ms: int = 15000
    language: Optional[str] = None
    country: Optional[str] = None
    agent: Optional[str] = None


def parse_relative_date_token(token: str) -> Optional[tuple[int, str]]:
    """Parse ``"3 days"`` -> ``(3, "day")``."""
    if not token:
        return None
    match = re.search(r"(\d+)\s+(day|week|month|year)s?", token.lower())
    if not match:
        return None
    try:
        return int(match.group(1)), match.group(2)
    except ValueError:
        return None


def dedupe_dicts_by_url(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Deduplicate dict-results by ``url`` key, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(dict(item))
    return out


def url_starts_with_http(value: Optional[str]) -> bool:
    """True if ``value`` is a string starting with ``http``."""
    return bool(value) and isinstance(value, str) and value.startswith("http")
