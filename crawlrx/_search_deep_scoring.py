"""Scoring helpers for search_deep.py (split for size)."""
from __future__ import annotations

import logging
import re
import time as _time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("crawlrx.search_deep.scoring")

# High-authority TLD/domain heuristics
_HIGH_AUTH_TLDS = {".gov", ".edu", ".mil"}
_MEDIUM_AUTH_DOMAINS = {
    "wikipedia.org", "nytimes.com", "wsj.com", "reuters.com", "bbc.com",
    "theguardian.com", "bloomberg.com", "techcrunch.com", "wired.com",
    "nature.com", "science.org", "arxiv.org", "ncbi.nlm.nih.gov",
    "stackoverflow.com", "github.com", "docs.python.org", "developer.mozilla.org",
    "cnn.com", "apnews.com", "npr.org", "pbs.org",
}


def _normalize_host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, TypeError):
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_authority_score(url: str) -> float:
    """Rough domain authority heuristic (0.0-1.0)."""
    host = _normalize_host(url)
    if not host:
        return 0.2
    for tld in _HIGH_AUTH_TLDS:
        if host.endswith(tld):
            return 1.0
    for domain in _MEDIUM_AUTH_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return 0.7
    return 0.3


def _freshness_for_days(days: int) -> float:
    if days <= 7:
        return max(0.6, 1.0 - days * 0.05)
    if days <= 30:
        return max(0.4, 0.8 - days * 0.01)
    return 0.3


def _freshness_from_year_match(low: str) -> Optional[float]:
    match = re.search(r"20(\d{2})", low)
    if not match:
        return None
    year = 2000 + int(match.group(1))
    current_year = int(_time.strftime("%Y"))
    diff = current_year - year
    return max(0.1, 0.8 - diff * 0.15)


_FRESHNESS_INSTANT_TOKENS = ("hour", "minute", "just now", "second")
_FRESHNESS_TODAY_TOKENS = ("today", "1 day")


def _freshness_from_keywords(low: str) -> Optional[float]:
    """Map keyword hits to a freshness score, or None if no keyword matches."""
    if any(token in low for token in _FRESHNESS_INSTANT_TOKENS):
        return 1.0
    if any(token in low for token in _FRESHNESS_TODAY_TOKENS):
        return 0.95
    return None


def _freshness_from_relative(low: str) -> Optional[float]:
    """Map ``N day(s)``/``N month(s)`` substrings to a score, or None."""
    days_match = re.search(r"(\d+)\s*day", low)
    if days_match:
        return _freshness_for_days(int(days_match.group(1)))
    months_match = re.search(r"(\d+)\s*month", low)
    if months_match:
        return max(0.1, 0.5 - int(months_match.group(1)) * 0.05)
    return None


def freshness_score(date_str: Optional[str]) -> float:
    """Score freshness 0.0-1.0. Newer = higher."""
    if not date_str:
        return 0.5
    low = date_str.lower()
    for resolver in (_freshness_from_keywords, _freshness_from_relative,
                     _freshness_from_year_match):
        score = resolver(low)
        if score is not None:
            return score
    return 0.5


def relevance_score(query: str, title: str, snippet: Optional[str]) -> float:
    """Keyword relevance (title 0.6, snippet 0.4)."""
    if not title:
        return 0.0
    words = set(re.findall(r"\w+", query.lower()))
    if not words:
        return 0.0
    title_words = set(re.findall(r"\w+", title.lower()))
    snippet_words = set(re.findall(r"\w+", (snippet or "").lower()))
    title_overlap = len(words & title_words) / len(words)
    snippet_overlap = (
        len(words & snippet_words) / len(words) if snippet_words else 0.0
    )
    return min(1.0, title_overlap * 0.6 + snippet_overlap * 0.4)


def score_result(query: str, item: dict, date_str: Optional[str] = None) -> float:
    """Composite: 0.25 * authority + 0.25 * fresh + 0.5 * relevance."""
    da = domain_authority_score(item.get("url", ""))
    fresh = freshness_score(date_str or item.get("date"))
    rel = relevance_score(query, item.get("title", ""), item.get("snippet"))
    return round(da * 0.25 + fresh * 0.25 + rel * 0.5, 4)
