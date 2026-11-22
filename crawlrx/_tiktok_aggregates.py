"""Aggregate stats for TikTok deep-profile videos.

Pure functions that take the canonical video-record list (built by
:mod:`_tiktok_deep`) and return the dict consumed by
``TikTokDeepProfileResponse.aggregates``.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


def _empty_aggregates() -> Dict[str, Any]:
    return {
        "total_videos_scraped": 0, "raw_avg_views": 0, "adjusted_avg_views": 0,
        "median_views": 0, "avg_likes": 0, "avg_comments": 0, "avg_shares": 0,
        "avg_engagement_rate": 0, "outliers_removed": 0, "top_5_by_views": [],
        "top_5_by_engagement": [], "hashtags": {}, "top_10_hashtags": [],
        "posting_frequency_per_week": 0, "best_days": {}, "best_hours": {}, "sounds": [],
    }


def _view_stats(views_list: List[int]) -> Dict[str, float]:
    """Compute raw/adjusted/median view stats with IQR outlier removal."""
    if not views_list:
        return {"raw_avg_views": 0, "adjusted_avg_views": 0, "median_views": 0, "outliers_removed": 0}
    raw_avg = sum(views_list) / len(views_list)
    median = statistics.median(views_list)
    outliers_removed = 0
    adjusted_avg = raw_avg
    if len(views_list) >= 4:
        q1, _, q3 = statistics.quantiles(views_list, n=4)
        iqr = q3 - q1
        filtered = [v for v in views_list if (q1 - 1.5 * iqr) <= v <= (q3 + 1.5 * iqr)]
        outliers_removed = len(views_list) - len(filtered)
        adjusted_avg = sum(filtered) / len(filtered) if filtered else raw_avg
    return {
        "raw_avg_views": raw_avg,
        "adjusted_avg_views": adjusted_avg,
        "median_views": median,
        "outliers_removed": outliers_removed,
    }


def _top_videos(videos: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Top-5 by raw views and by engagement_rate."""
    top_views = [
        {
            "id": v["id"], "url": v["url"],
            "description": (v.get("description") or "")[:100],
            "views": v["views"],
        }
        for v in sorted(videos, key=lambda v: v.get("views", 0), reverse=True)[:5]
    ]
    eng_pool = [v for v in videos if v.get("engagement_rate") is not None]
    top_eng = [
        {
            "id": v["id"], "url": v["url"],
            "description": (v.get("description") or "")[:100],
            "engagement_rate": v["engagement_rate"], "views": v["views"],
        }
        for v in sorted(eng_pool, key=lambda v: v["engagement_rate"], reverse=True)[:5]
    ]
    return {"top_5_by_views": top_views, "top_5_by_engagement": top_eng}


def _hashtag_stats(videos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Frequency-sorted hashtag dict + top 10 list."""
    counts: Dict[str, int] = defaultdict(int)
    for v in videos:
        for tag in v.get("hashtags", []):
            counts[tag] += 1
    sorted_tags = dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))
    return {"hashtags": sorted_tags, "top_10_hashtags": list(sorted_tags.keys())[:10]}


_DAY_NAMES: Tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def _posting_cadence(videos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Posting frequency per week + per-day / per-hour histograms."""
    timestamps = [v["create_time_unix"] for v in videos if v.get("create_time_unix")]
    best_days: Dict[str, int] = {d: 0 for d in _DAY_NAMES}
    best_hours: Dict[int, int] = {h: 0 for h in range(24)}
    if not timestamps:
        return {"posting_frequency_per_week": 0.0, "best_days": best_days, "best_hours": best_hours}
    days_span = (max(timestamps) - min(timestamps)) / 86400
    freq = (len(videos) / days_span) * 7 if days_span > 0 else 0
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        best_days[_DAY_NAMES[dt.weekday()]] += 1
        best_hours[dt.hour] += 1
    return {"posting_frequency_per_week": freq, "best_days": best_days, "best_hours": best_hours}


def _sound_stats(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sound_counter: Counter = Counter()
    sound_meta: Dict[str, Dict[str, Any]] = {}
    for v in videos:
        s = v.get("sound")
        if s and s.get("title"):
            key = s["title"]
            sound_counter[key] += 1
            if key not in sound_meta:
                sound_meta[key] = {
                    "author": s.get("author"),
                    "is_original": s.get("is_original"),
                }
    return [
        {
            "title": title,
            "author": sound_meta[title]["author"],
            "is_original": sound_meta[title]["is_original"],
            "count": count,
        }
        for title, count in sound_counter.most_common()
    ]


def _avg(seq: List[float]) -> float:
    return sum(seq) / len(seq) if seq else 0.0


def compute_deep_aggregates(videos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate stats over a video list (IQR-adjusted view averages, sounds)."""
    if not videos:
        return _empty_aggregates()

    views_list = [v["views"] for v in videos if v.get("views", 0) > 0]
    likes_list = [v["likes"] for v in videos]
    comments_list = [v["comments"] for v in videos]
    shares_list = [v["shares"] for v in videos]
    eng_list = [
        v["engagement_rate"] for v in videos if v.get("engagement_rate") is not None
    ]

    view_stats = _view_stats(views_list)
    cadence = _posting_cadence(videos)
    tags = _hashtag_stats(videos)
    tops = _top_videos(videos)
    sounds = _sound_stats(videos)

    return {
        "total_videos_scraped": len(videos),
        "raw_avg_views": round(view_stats["raw_avg_views"], 1),
        "adjusted_avg_views": round(view_stats["adjusted_avg_views"], 1),
        "median_views": round(view_stats["median_views"], 1),
        "avg_likes": round(_avg(likes_list), 1),
        "avg_comments": round(_avg(comments_list), 1),
        "avg_shares": round(_avg(shares_list), 1),
        "avg_engagement_rate": round(_avg(eng_list), 4),
        "outliers_removed": view_stats["outliers_removed"],
        **tops,
        **tags,
        "posting_frequency_per_week": round(cadence["posting_frequency_per_week"], 2),
        "best_days": cadence["best_days"],
        "best_hours": cadence["best_hours"],
        "sounds": sounds,
    }
