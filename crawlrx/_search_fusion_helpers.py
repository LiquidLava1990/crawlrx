"""Helpers for search_fusion.py (Module split).

Split from ``search_fusion.py`` to keep the router file lean and to give
the scoring + source-runner logic its own narrow exception families.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ._search_engines import EngineSearchParams
from ._search_helpers import SEARCH_NET_EXCEPTIONS, SERP_BROWSER_EXCEPTIONS

logger = logging.getLogger("crawlrx._search_fusion_helpers")


# Date formats accepted by ``parse_date`` (broadest first).
_DATE_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y",
)

_RELATIVE_DATE_RE = re.compile(r"(\d+)\s+(day|week|month|year)s?\s+ago")

_TRUSTED_DOMAINS: Tuple[str, ...] = (
    "wikipedia.org", "github.com", "stackoverflow.com", "reddit.com",
    "nytimes.com", "bbc.com", "bbc.co.uk", "reuters.com", "bloomberg.com",
    "wsj.com", "washingtonpost.com", "theguardian.com", "ap.org",
    "nature.com", "science.org", "arxiv.org", "ieee.org",
    "medium.com", "substack.com", "techcrunch.com", "theverge.com",
)

# Buckets for ``score_freshness`` -- (max-age-days, score).
_FRESHNESS_BUCKETS: Tuple[Tuple[int, float], ...] = (
    (1, 1.0), (7, 0.9), (30, 0.8), (90, 0.7),
    (180, 0.6), (365, 0.5), (730, 0.4), (1825, 0.3),
)
_FRESHNESS_FALLBACK: float = 0.2


# ─────────────────────────────────────────────
# URL / date utilities
# ─────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Strip trailing slash, lowercase scheme + host."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError) as exc:
        logger.debug("normalize_url failed: %s", type(exc).__name__)
        return url.lower().rstrip("/")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path.rstrip("/")
    if parsed.query:
        return f"{scheme}://{host}{path}?{parsed.query}"
    return f"{scheme}://{host}{path}"


def _parse_relative_date(cleaned: str) -> Optional[datetime]:
    match = _RELATIVE_DATE_RE.search(cleaned.lower())
    if not match:
        return None
    num = int(match.group(1))
    unit = match.group(2)
    now = datetime.now(tz=timezone.utc)
    if unit == "day":
        return now - timedelta(days=num)
    if unit == "week":
        return now - timedelta(weeks=num)
    if unit == "month":
        return now - timedelta(days=num * 30)
    return now - timedelta(days=num * 365)


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a date string into ``datetime`` (or ``None``)."""
    if not date_str:
        return None
    cleaned = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _parse_relative_date(cleaned)


# ─────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────

def score_freshness(date: Optional[str]) -> float:
    """Return a 0-1 score, higher = more recent."""
    parsed = parse_date(date)
    if not parsed:
        return 0.5
    days_old = (datetime.now(tz=timezone.utc) - parsed).days
    for max_age, score in _FRESHNESS_BUCKETS:
        if days_old <= max_age:
            return score
    return _FRESHNESS_FALLBACK


def _term_overlap(query_terms: set, source_terms: set) -> float:
    if not query_terms:
        return 0.0
    return len(query_terms & source_terms) / len(query_terms)


def _trust_boost(url: str) -> float:
    url_lower = url.lower()
    return 0.15 if any(domain in url_lower for domain in _TRUSTED_DOMAINS) else 0.0


@dataclass(frozen=True)
class ScoreInputs:
    """Bundle of inputs for :func:`score_result` (the parameter-count limit)."""

    title: str
    url: str
    snippet: Optional[str]
    query: str
    date: Optional[str] = None


def score_result(inputs: ScoreInputs) -> float:
    """Composite relevance score (0.0-1.0) for one result."""
    query_terms = set(inputs.query.lower().split())
    title_terms = set(inputs.title.lower().split())
    snippet_terms = set((inputs.snippet or "").lower().split())
    relevance = (
        _term_overlap(query_terms, title_terms) * 0.6
        + _term_overlap(query_terms, snippet_terms) * 0.4
    )
    freshness = score_freshness(inputs.date)
    return min(1.0, relevance * 0.6 + _trust_boost(inputs.url) + freshness * 0.2)


# ─────────────────────────────────────────────
# Source runners
# ─────────────────────────────────────────────

# Tuple is (source_name, results, error_message_or_None).
SourceOutcome = Tuple[str, List[Dict[str, Any]], Optional[str]]


def _tag_source(results: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    for r in results:
        r["source"] = source
    return results


def run_ddg_source(query: str, count: int) -> SourceOutcome:
    """Synchronously fetch DDG results -- used inside a ``ThreadPoolExecutor``."""
    import asyncio

    from ._search_engines import do_search_ddgs

    try:
        result = asyncio.run(do_search_ddgs(query, count))
    except SEARCH_NET_EXCEPTIONS as exc:
        return ("ddg", [], "DDG net error: %s" % type(exc).__name__)
    except RuntimeError as exc:
        # asyncio.run inside an existing loop -- fall back to a fresh loop.
        return ("ddg", [], "DDG runtime: %s" % exc)
    if not result.get("success"):
        return ("ddg", [], result.get("error", "DDG returned failure"))
    return ("ddg", _tag_source(result.get("results", []), "ddg"), None)


def run_bing_source(query: str, count: int) -> SourceOutcome:
    """Synchronously fetch Bing results via Playwright."""
    import asyncio

    from ._search_engines import do_search_playwright

    params = EngineSearchParams(
        num_results=count,
        timeout_ms=15000,
        language=None,
        country=None,
        agent=None,
    )
    try:
        result = asyncio.run(do_search_playwright(query, "bing", params))
    except SERP_BROWSER_EXCEPTIONS as exc:
        return ("bing", [], "Bing browser error: %s" % type(exc).__name__)
    except RuntimeError as exc:
        return ("bing", [], "Bing runtime: %s" % exc)
    if not result.get("success"):
        return ("bing", [], result.get("error", "Bing returned failure"))
    return ("bing", _tag_source(result.get("results", []), "bing"), None)


SOURCE_RUNNERS = {
    "ddg": run_ddg_source,
    "bing": run_bing_source,
}


# ─────────────────────────────────────────────
# Deduplication & merge
# ─────────────────────────────────────────────

def deduplicate_results(
    all_results: List[Dict[str, Any]],
    keep_highest_score: bool = True,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ``(deduped, num_dupes_dropped)`` keyed by ``normalize_url``."""
    seen: Dict[str, Dict[str, Any]] = {}
    dupes = 0
    for result in all_results:
        url = result.get("url", "")
        if not url:
            continue
        normalized = normalize_url(url)
        if normalized not in seen:
            seen[normalized] = result
            continue
        dupes += 1
        if not keep_highest_score:
            continue
        if result.get("score", 0) > seen[normalized].get("score", 0):
            seen[normalized] = result
    return list(seen.values()), dupes


def _drain_one_future(
    future: Any, source_name: str, errors: List[str], stats: Dict[str, int],
) -> Optional[tuple]:
    """Resolve one future to (name, results, error) or None on transport failure."""
    try:
        return future.result(timeout=20)
    except (TimeoutError, *SEARCH_NET_EXCEPTIONS, *SERP_BROWSER_EXCEPTIONS) as exc:
        logger.error("Source %s exception: %s", source_name, type(exc).__name__)
        errors.append("%s: %s" % (source_name, type(exc).__name__))
        stats[source_name] = 0
        # explicit recovery: net/timeout/browser failure non-fatal; caller skips source
        return None


def collect_outcomes(
    sources_to_run: List[str],
    query: str,
    count: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[str], List[str]]:
    """Run sources in parallel and return aggregated outcomes."""
    all_results: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {}
    used: List[str] = []
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(SOURCE_RUNNERS[source], query, count): source
            for source in sources_to_run
            if source in SOURCE_RUNNERS
        }
        for future, source_name in futures.items():
            outcome = _drain_one_future(future, source_name, errors, stats)
            if outcome is None:
                continue
            name, results, error = outcome
            if error:
                logger.warning("Source %s failed: %s", name, error)
                errors.append("%s: %s" % (name, error))
                stats[name] = 0
                continue
            logger.info("Source %s returned %d results", name, len(results))
            all_results.extend(results)
            stats[name] = len(results)
            used.append(name)
    return all_results, stats, used, errors
