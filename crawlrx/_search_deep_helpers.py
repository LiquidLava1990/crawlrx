"""Query/expansion/DDG helpers for search_deep.py."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._search_helpers import DorkParams, SEARCH_NET_EXCEPTIONS

ScoreFn = Callable[[str, dict, Optional[str]], float]

logger = logging.getLogger("crawlrx.search_deep.helpers")


# Simple synonym map for query expansion.
_SYNONYM_MAP: Dict[str, List[str]] = {
    "buy": ["purchase", "get", "acquire"],
    "cheap": ["affordable", "low-cost", "budget"],
    "best": ["top", "leading", "highest-rated"],
    "how to": ["guide", "tutorial", "steps to"],
    "fix": ["repair", "solve", "troubleshoot"],
    "free": ["open-source", "no-cost", "gratis"],
    "review": ["analysis", "comparison", "evaluation"],
    "vs": ["versus", "compared to", "alternative to"],
}

_PROTOCOL_RE = re.compile(r"^https?://")


# ─────────────────────────────────────────────
# ddgs lazy detection (single source of truth for the deep module)
# Module-level imports: prefer the modern `ddgs` package; fall back to the
# older `duckduckgo_search` package (still distributed for slim images).
# ─────────────────────────────────────────────

try:
    from ddgs import DDGS as _DDGS_CLASS  # type: ignore[no-redef] because optional dep; ddgs ships without PEP 561 stubs
except ImportError:
    try:
        from duckduckgo_search import DDGS as _DDGS_CLASS  # type: ignore[no-redef,import-not-found] because optional fallback package; absent in slim deployments
    except ImportError:
        _DDGS_CLASS = None  # type: ignore[assignment] because module-level None sentinel; downstream guards check DDGS_AVAILABLE before calling

DDGS_AVAILABLE: bool = _DDGS_CLASS is not None


def ddgs_class() -> Optional[type]:
    """Return the resolved ``DDGS`` class (or ``None`` if not installed)."""
    return _DDGS_CLASS


# ─────────────────────────────────────────────
# Dork builder
# ─────────────────────────────────────────────

def _site_token(site_filter: str) -> str:
    site = _PROTOCOL_RE.sub("", site_filter).rstrip("/")
    return "site:%s" % site


def _filetype_token(file_type: str) -> str:
    return "filetype:%s" % file_type.lower().lstrip(".")


def _exclude_tokens(terms: List[str]) -> List[str]:
    return ["-%s" % term for term in terms]


def build_dork_query(query: str, params: DorkParams) -> str:
    """Build a Google dork query string from a ``DorkParams`` bundle."""
    parts: List[str] = []
    if params.exact_phrase:
        parts.append('"%s"' % params.exact_phrase)
    parts.append(query)
    if params.site_filter:
        parts.append(_site_token(params.site_filter))
    if params.file_type:
        parts.append(_filetype_token(params.file_type))
    if params.inurl:
        parts.append("inurl:%s" % params.inurl)
    if params.intitle:
        parts.append('intitle:"%s"' % params.intitle)
    if params.exclude_terms:
        parts.extend(_exclude_tokens(params.exclude_terms))
    return " ".join(parts)


# ─────────────────────────────────────────────
# Query expansion
# ─────────────────────────────────────────────

def _accept_variation(
    variation: str, lower: str, variations: List[str], max_n: int,
) -> bool:
    if variation == lower or variation in variations:
        return False
    variations.append(variation)
    return len(variations) >= max_n


def _variations_for_word(
    word: str, synonyms: List[str], lower: str,
    variations: List[str], max_n: int,
) -> bool:
    """Append substitutions for ``word`` -- returns True once ``max_n`` reached."""
    for syn in synonyms[:2]:
        variation = re.sub(re.escape(word), syn, lower, count=1)
        if _accept_variation(variation, lower, variations, max_n):
            return True
    return False


def _synonym_variations(lower: str, max_n: int = 3) -> List[str]:
    variations: List[str] = []
    for word, synonyms in _SYNONYM_MAP.items():
        if word not in lower:
            continue
        if _variations_for_word(word, synonyms, lower, variations, max_n):
            return variations
    return variations


def expand_query(query: str) -> List[str]:
    """Generate up to 3 synonym/quoted variations of ``query``."""
    variations = _synonym_variations(query.lower())
    if len(variations) >= 3:
        return variations[:3]
    if not query.startswith('"'):
        variations.append('"%s"' % query)
    return variations[:3]


# ─────────────────────────────────────────────
# DDG leg (web + news)
# ─────────────────────────────────────────────

def _ddg_text_record(item: dict) -> dict:
    """Render one DDG-text raw item into the unified text-search shape."""
    return {
        "title": item.get("title", ""),
        "url": item.get("href", item.get("url", "")),
        "snippet": item.get("body", item.get("snippet", "")),
        "date": None,
        "source": "ddg",
    }


def _ddg_news_record(item: dict) -> dict:
    """Render one DDG-news raw item into the unified news shape."""
    return {
        "headline": item.get("title", ""),
        "source": item.get("source", ""),
        "date": item.get("date", ""),
        "url": item.get("url", ""),
        "snippet": item.get("body", ""),
    }


@dataclass(frozen=True)
class _DdgLegSpec:
    """Per-leg config bundle for `_run_ddg_leg`."""
    method: str
    record_fn: Callable[[Dict[str, Any]], Dict[str, Any]]
    leg_label: str


def _run_ddg_leg(query: str, num_results: int, spec: _DdgLegSpec) -> List[dict]:
    """Drive any single DDG leg: bind, fetch, map; ``spec.method`` picks ddgs API."""
    if not DDGS_AVAILABLE or _DDGS_CLASS is None:
        logger.warning("ddgs not available for deep search DDG leg (%s)", spec.leg_label)
        return []
    try:
        with _DDGS_CLASS() as ddgs:  # type: ignore[misc] because ddgs has no public stubs; context-manager protocol verified at runtime
            api = getattr(ddgs, spec.method)
            raw = list(api(query, max_results=num_results))
    except SEARCH_NET_EXCEPTIONS as exc:
        logger.warning("DDG %s leg failed: %s", spec.leg_label, type(exc).__name__)
        return []
    out = [spec.record_fn(item) for item in raw]
    logger.info("DDG %s returned %d results for '%s'", spec.leg_label, len(out), query)
    return out


_DDG_TEXT_SPEC = _DdgLegSpec(
    method="text", record_fn=_ddg_text_record, leg_label="text",
)
_DDG_NEWS_SPEC = _DdgLegSpec(
    method="news", record_fn=_ddg_news_record, leg_label="news",
)


def ddg_text_search(query: str, num_results: int) -> List[dict]:
    """Run DDG text search via the ``ddgs`` library."""
    return _run_ddg_leg(query, num_results, _DDG_TEXT_SPEC)


def ddg_news_search(query: str, num_results: int) -> List[dict]:
    """Run DDG news search; output schema matches Google News records."""
    return _run_ddg_leg(query, num_results, _DDG_NEWS_SPEC)


# ─────────────────────────────────────────────
# Merge / dedupe
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class DeepSearchInputs:
    """Bundled inputs for ``run_deep_search`` (keeps positional count low)."""

    query: str
    dork_params: DorkParams
    num_results: int = 10
    timeout: int = 15000


def merge_and_score(
    google_results: List[dict],
    ddg_results: List[dict],
    query: str,
    score_fn: ScoreFn,
) -> List[dict]:
    """Merge two source lists, dedupe by URL, score each, sort descending."""
    seen: set = set()
    merged: List[dict] = []
    for item in [*google_results, *ddg_results]:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        item["score"] = score_fn(query, item, item.get("date"))
        merged.append(item)
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged
