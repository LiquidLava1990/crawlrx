"""TikTok niche keyword maps + scoring functions.

Pure data + pure functions: no I/O, no Playwright, no TikTokApi import.
Consumed by :mod:`_tiktok_deep` to score a fetched profile against the
15 content niches we track.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


NICHE_KEYWORDS: Dict[str, List[str]] = {
    "personal finance": ["money", "finance", "investing", "stocks", "budget", "wealth", "crypto", "income"],
    "fitness": ["workout", "gym", "fitness", "gains", "muscle", "cardio", "weight", "training"],
    "food": ["recipe", "cooking", "food", "eat", "restaurant", "chef", "meal", "tasty"],
    "comedy": ["funny", "lol", "humor", "joke", "prank", "skit", "laugh", "viral"],
    "gaming": ["gaming", "game", "play", "twitch", "stream", "gamer", "xbox", "ps5", "fortnite"],
    "real estate": ["realestate", "house", "property", "invest", "rental", "airbnb", "flip"],
    "entrepreneurship": ["business", "entrepreneur", "startup", "hustle", "sidehustle", "grind", "ecommerce"],
    "education": ["learn", "tip", "howto", "tutorial", "explain", "facts", "knowledge", "teach"],
    "lifestyle": ["life", "day", "routine", "vlog", "daily", "morning", "aesthetic"],
    "fashion": ["outfit", "style", "fashion", "clothes", "ootd", "fit", "drip", "wear"],
    "travel": ["travel", "trip", "vacation", "explore", "adventure", "destination", "abroad"],
    "beauty": ["makeup", "skincare", "beauty", "glow", "routine", "glam", "hair"],
    "tech": ["tech", "ai", "software", "coding", "developer", "app", "saas", "automation"],
    "sports": ["sports", "nba", "nfl", "soccer", "football", "basketball", "athlete"],
    "motivation": ["motivation", "mindset", "success", "goals", "grind", "discipline", "hardwork"],
}

NICHE_BENCHMARKS: Dict[str, Dict[str, Any]] = {
    "personal finance": {"avg_views": 45000, "growth": "high", "competition": "high"},
    "fitness": {"avg_views": 38000, "growth": "medium", "competition": "high"},
    "food": {"avg_views": 52000, "growth": "medium", "competition": "high"},
    "comedy": {"avg_views": 85000, "growth": "high", "competition": "very_high"},
    "gaming": {"avg_views": 22000, "growth": "low", "competition": "very_high"},
    "real estate": {"avg_views": 31000, "growth": "high", "competition": "medium"},
    "entrepreneurship": {"avg_views": 28000, "growth": "high", "competition": "medium"},
    "education": {"avg_views": 41000, "growth": "high", "competition": "medium"},
    "lifestyle": {"avg_views": 33000, "growth": "medium", "competition": "high"},
    "fashion": {"avg_views": 44000, "growth": "medium", "competition": "very_high"},
    "travel": {"avg_views": 39000, "growth": "medium", "competition": "medium"},
    "beauty": {"avg_views": 47000, "growth": "medium", "competition": "very_high"},
    "tech": {"avg_views": 35000, "growth": "high", "competition": "medium"},
    "sports": {"avg_views": 29000, "growth": "medium", "competition": "high"},
    "motivation": {"avg_views": 25000, "growth": "medium", "competition": "high"},
}

_DEFAULT_BENCH: Dict[str, Any] = {
    "avg_views": 30000, "growth": "medium", "competition": "medium",
}


def _niche_recommendation(niche: str, fit_score: float, bench: Dict[str, Any]) -> str:
    """Return a one-sentence recommendation for ``niche``."""
    growth = bench["growth"]
    competition = bench["competition"]
    if fit_score >= 60:
        return (
            f"Double down on {niche} content -- your metrics align well "
            f"and growth potential is {growth}."
        )
    if fit_score >= 30:
        return (
            f"Lean more into {niche} content; your baseline is competitive "
            f"with {competition} competition."
        )
    return (
        f"Pivot toward {niche} to tap {growth} growth potential, "
        f"but expect {competition} competition."
    )


def _word_corpus(videos: List[Dict[str, Any]]) -> List[str]:
    words: List[str] = []
    for v in videos:
        words.extend(re.findall(r"\w+", (v.get("description") or "").lower()))
        for tag in v.get("hashtags", []):
            words.extend(re.findall(r"\w+", tag.lower()))
    return words


@dataclass(frozen=True)
class _ScoreCtx:
    """Bundle of inputs scored against each niche."""

    all_words: List[str]
    total_words: int
    adjusted_avg_views: float
    avg_engagement_rate: float


def _score_niche(
    niche: str, keywords: List[str], ctx: _ScoreCtx,
) -> Tuple[Dict[str, Any], bool]:
    """Score one niche; return ``(record, keyword_hit)``."""
    hits = sum(ctx.all_words.count(kw) for kw in keywords)
    keyword_match_score = min((hits / ctx.total_words) * 1000, 50)
    bench = NICHE_BENCHMARKS.get(niche, _DEFAULT_BENCH)
    niche_avg = bench["avg_views"]
    view_ratio = (
        min((ctx.adjusted_avg_views / niche_avg) * 30, 30) if niche_avg > 0 else 0.0
    )
    engagement = min((ctx.avg_engagement_rate / 5.0) * 20, 20)
    fit = round(keyword_match_score + view_ratio + engagement, 2)
    record = {
        "niche": niche,
        "fit_score": fit,
        "growth_potential": bench["growth"],
        "competition": bench["competition"],
        "avg_views_benchmark": niche_avg,
        "recommendation": _niche_recommendation(niche, fit, bench),
    }
    return record, keyword_match_score > 0


def build_niche_scores(
    videos: List[Dict[str, Any]],
    aggregates: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Score every niche; return (sorted scores, keyword-hit niches)."""
    all_words = _word_corpus(videos)
    ctx = _ScoreCtx(
        all_words=all_words,
        total_words=len(all_words) or 1,
        adjusted_avg_views=aggregates["adjusted_avg_views"],
        avg_engagement_rate=aggregates["avg_engagement_rate"],
    )
    detected: List[str] = []
    raw: List[Dict[str, Any]] = []
    for niche, keywords in NICHE_KEYWORDS.items():
        record, hit = _score_niche(niche, keywords, ctx)
        if hit:
            detected.append(niche)
        raw.append(record)
    raw.sort(key=lambda x: x["fit_score"], reverse=True)
    return raw, detected


def summarize_niche(
    adjusted_avg_views: float,
    avg_engagement_rate: float,
    top_scores: List[Dict[str, Any]],
) -> str:
    """Build the prose ``summary`` field for the niche-analyze response."""
    top_niche = top_scores[0]["niche"] if top_scores else "general content"
    top_fit = top_scores[0]["fit_score"] if top_scores else 0
    return (
        f"This account averages {adjusted_avg_views:.0f} views per video with a "
        f"{avg_engagement_rate:.2f}% engagement rate. The strongest niche fit is "
        f"{top_niche} (fit score: {top_fit:.0f}), based on content keywords and "
        f"view performance vs benchmarks. To accelerate growth, post consistently "
        f"in the {top_niche} niche and use its top-performing hashtags."
    )
