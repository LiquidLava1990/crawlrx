"""Robust search orchestration for CrawlRX.

This endpoint sits above the individual search routes. It classifies a query,
runs the best available routes in parallel, normalizes results, deduplicates
URLs, and returns one ranked response even when one upstream is thin or noisy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import config as _cfg
from ._ecommerce_search_models import EcommerceSearchRequest
from .ecommerce_search import search_ecommerce
from .local_search import search_local
from .models import (
    DeepSearchRequest,
    LocalSearchRequest,
    NewsDeepRequest,
    SearchRequest,
)
from .search import search as search_web
from .search_deep import search_deep, search_news_google
from .search_fusion import FusionRequest, search_fusion
from .search_media import (
    ImageSearchRequest,
    NewsSearchRequest,
    VideoSearchRequest,
    search_images,
    search_news,
    search_videos,
)
from .utils import timer_ms
from .paths import env_path

logger = logging.getLogger("crawlrx.search_orchestrator")
router = APIRouter(prefix="/search", tags=["search"])

SearchMode = Literal[
    "auto",
    "web",
    "fusion",
    "images",
    "news",
    "videos",
    "deep",
    "google-news",
    "local",
    "ecommerce",
]

RouteStatus = Literal["ok", "warn", "fail", "skipped"]
DiagnosticStatus = Literal["ok", "warn", "fail"]

_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_DORK_MARKERS = ("site:", "filetype:", "intitle:", "inurl:")
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_ROUTE_BASE_SCORE = {
    "google-news": 0.88,
    "deep": 0.84,
    "fusion": 0.82,
    "ecommerce": 0.82,
    "local": 0.82,
    "news": 0.78,
    "web": 0.74,
    "images": 0.68,
    "videos": 0.68,
}
_ROUTE_TIMEOUT_CAPS = {
    "web": 45.0,
    "fusion": 25.0,
    "images": 65.0,
    "news": 65.0,
    "videos": 65.0,
    "deep": 60.0,
    "google-news": 60.0,
    "local": 45.0,
    "ecommerce": 20.0,
}
_ORCHESTRATOR_CACHE_TTL_S = 600
_ORCHESTRATOR_STALE_TTL_S = 86400
_ORCHESTRATOR_CACHE_MAX = 128
_ORCHESTRATOR_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_ORCHESTRATOR_CACHE_LOADED = False
_ORCHESTRATOR_CACHE_PATH = Path(env_path("CRAWLRX_ORCHESTRATOR_CACHE", "search_orchestrator_cache.json"))
_ROUTE_BACKOFF_ELIGIBLE = {
    "fusion",
    "images",
    "news",
    "videos",
    "deep",
    "google-news",
    "local",
    "ecommerce",
}
_ROUTE_FAILURE_LIMIT = 3
_ROUTE_BACKOFF_S = 300
_ROUTE_HEALTH: Dict[str, Dict[str, Any]] = {}
_DIAGNOSTIC_CACHE_TTL_S = int(os.getenv("CRAWLRX_SEARCH_DIAGNOSTIC_TTL_S", "300"))
_DIAGNOSTIC_CACHE: Optional[tuple[float, "SearchDiagnosticsResponse"]] = None
_DIAGNOSTIC_LOG_PATH = Path(env_path("CRAWLRX_SEARCH_DIAGNOSTIC_LOG", "search_diagnostics.jsonl"))
_SOURCE_HEALTH_PATH = Path(env_path("CRAWLRX_SEARCH_SOURCE_HEALTH", "search_source_health.json"))
_SOURCE_HEALTH_SCORE_START = 1.0
_SOURCE_HEALTH_SUCCESS_RECOVERY = float(
    os.getenv("CRAWLRX_SOURCE_HEALTH_SUCCESS_RECOVERY", "0.08")
)
_SOURCE_HEALTH_WARNING_PENALTY = float(
    os.getenv("CRAWLRX_SOURCE_HEALTH_WARNING_PENALTY", "0.12")
)
_SOURCE_HEALTH_FAILURE_PENALTY = float(
    os.getenv("CRAWLRX_SOURCE_HEALTH_FAILURE_PENALTY", "0.35")
)
_SOURCE_QUARANTINE_FAILURE_LIMIT = int(
    os.getenv("CRAWLRX_SOURCE_QUARANTINE_FAILURE_LIMIT", "3")
)
_SOURCE_QUARANTINE_WARNING_LIMIT = int(
    os.getenv("CRAWLRX_SOURCE_QUARANTINE_WARNING_LIMIT", "5")
)
_SOURCE_QUARANTINE_SCORE = float(os.getenv("CRAWLRX_SOURCE_QUARANTINE_SCORE", "0.2"))
_SOURCE_QUARANTINE_S = int(os.getenv("CRAWLRX_SOURCE_QUARANTINE_S", "900"))


class SearchOrchestratorRequest(BaseModel):
    """Request for POST /search/orchestrate."""

    query: str = Field(..., min_length=1)
    mode: SearchMode = Field(default="auto")
    count: int = Field(default=10, ge=1, le=50)
    timeout: int = Field(default=30000, ge=1000, le=120000)
    engine: str = Field(default="searxng")
    include_content: bool = Field(default=False)
    language: Optional[str] = None
    country: Optional[str] = None
    sources: List[str] = Field(default_factory=lambda: ["ddg", "bing"])
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    dedupe: bool = Field(default=True)
    site_filter: Optional[str] = None
    file_type: Optional[str] = None
    exact_phrase: Optional[str] = None
    exclude_terms: Optional[List[str]] = None
    inurl: Optional[str] = None
    intitle: Optional[str] = None
    zip_code: Optional[str] = None
    platforms: Optional[List[str]] = None
    max_price: Optional[float] = None
    min_price: Optional[float] = None


class OrchestratedResult(BaseModel):
    """One normalized result from any CrawlRX search route."""

    title: str
    url: str
    snippet: Optional[str] = None
    source: str = ""
    kind: str = "web"
    score: float = 0.0
    content: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RouteOutcome(BaseModel):
    """Execution proof for one attempted route."""

    route: str
    status: RouteStatus
    elapsed_ms: int = 0
    result_count: int = 0
    engine: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None


class SearchOrchestratorResponse(BaseModel):
    """Response model for POST /search/orchestrate."""

    success: bool
    query: str
    mode: SearchMode
    intent: str
    confidence: float
    results: List[OrchestratedResult] = Field(default_factory=list)
    total: int = 0
    elapsed_ms: int = 0
    routes: List[RouteOutcome] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    cache_hit: bool = False
    stale: bool = False
    error: Optional[str] = None


class SearchOrchestratorStatus(BaseModel):
    """Runtime status for the search brain."""

    cache_entries: int = 0
    cache_path: str = ""
    fresh_ttl_s: int = _ORCHESTRATOR_CACHE_TTL_S
    stale_ttl_s: int = _ORCHESTRATOR_STALE_TTL_S
    route_backoff_s: int = _ROUTE_BACKOFF_S
    route_failure_limit: int = _ROUTE_FAILURE_LIMIT
    route_health: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    source_health_path: str = ""
    source_health: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class SearchDiagnosticCheck(BaseModel):
    """One live quality probe for a search source group."""

    name: str
    source: str
    group: str = ""
    health_key: str = ""
    query: str
    engines: str
    status: DiagnosticStatus
    elapsed_ms: int = 0
    result_count: int = 0
    warning_count: int = 0
    min_results: int = 1
    detail: str = ""
    sample_title: Optional[str] = None


class SearchDiagnosticsResponse(BaseModel):
    """Live search quality proof for operators and smoke tests."""

    status: DiagnosticStatus
    elapsed_ms: int = 0
    generated_at_s: float = 0.0
    cache_hit: bool = False
    cache_ttl_s: int = _DIAGNOSTIC_CACHE_TTL_S
    log_path: str = ""
    registry_path: str = ""
    source_health_path: str = ""
    checks: List[SearchDiagnosticCheck] = Field(default_factory=list)
    summary: Dict[str, int] = Field(default_factory=dict)
    source_groups: Dict[str, str] = Field(default_factory=dict)
    source_health: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


def _as_dict(value: Any) -> Dict[str, Any]:
    """Convert pydantic models or plain dicts to a regular dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def _items(value: Any) -> List[Dict[str, Any]]:
    """Convert a list of pydantic models/dicts into plain dicts."""
    return [_as_dict(item) for item in (value or [])]


def _model_dump(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value)


def _cache_key(request: SearchOrchestratorRequest) -> str:
    payload = _model_dump(request)
    payload["query"] = _clean_query(payload.get("query", "")).lower()
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_load_once() -> None:
    global _ORCHESTRATOR_CACHE_LOADED
    if _ORCHESTRATOR_CACHE_LOADED:
        return
    _ORCHESTRATOR_CACHE_LOADED = True
    try:
        raw = _ORCHESTRATOR_CACHE_PATH.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    now = time.time()
    for key, item in payload.items():
        if not isinstance(item, dict):
            continue
        ts = item.get("ts")
        cached_payload = item.get("payload")
        if not isinstance(ts, (int, float)) or not isinstance(cached_payload, dict):
            continue
        if now - float(ts) > _ORCHESTRATOR_STALE_TTL_S:
            continue
        _ORCHESTRATOR_CACHE[str(key)] = (float(ts), cached_payload)


def _cache_flush() -> None:
    try:
        _ORCHESTRATOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            _ORCHESTRATOR_CACHE.items(),
            key=lambda item: item[1][0],
            reverse=True,
        )[:_ORCHESTRATOR_CACHE_MAX]
        payload = {
            key: {"ts": ts, "payload": cached_payload}
            for key, (ts, cached_payload) in ordered
        }
        tmp = _ORCHESTRATOR_CACHE_PATH.with_suffix(
            _ORCHESTRATOR_CACHE_PATH.suffix + ".tmp"
        )
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_ORCHESTRATOR_CACHE_PATH)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("orchestrator cache flush failed: %s", type(exc).__name__)


def _cache_get(key: str, max_age_s: int) -> Optional[Dict[str, Any]]:
    _cache_load_once()
    cached = _ORCHESTRATOR_CACHE.get(key)
    if not cached:
        return None
    ts, payload = cached
    if time.time() - ts > max_age_s:
        return None
    return dict(payload)


def _cache_set(key: str, response: SearchOrchestratorResponse) -> None:
    _cache_load_once()
    if not response.results:
        return
    if len(_ORCHESTRATOR_CACHE) >= _ORCHESTRATOR_CACHE_MAX:
        oldest = min(_ORCHESTRATOR_CACHE, key=lambda k: _ORCHESTRATOR_CACHE[k][0])
        _ORCHESTRATOR_CACHE.pop(oldest, None)
    payload = _model_dump(response)
    payload["cache_hit"] = False
    payload["stale"] = False
    _ORCHESTRATOR_CACHE[key] = (time.time(), payload)
    _cache_flush()


def _cached_response(payload: Dict[str, Any], *, stale: bool) -> SearchOrchestratorResponse:
    warnings = list(payload.get("warnings") or [])
    if stale:
        warnings.append("served stale orchestrator cache after live routes failed")
    payload = {
        **payload,
        "elapsed_ms": 0 if not stale else payload.get("elapsed_ms", 0),
        "warnings": warnings,
        "cache_hit": True,
        "stale": stale,
        "error": None if payload.get("results") else payload.get("error"),
    }
    return SearchOrchestratorResponse(**payload)


def _extract_zip(query: str) -> Optional[str]:
    match = _ZIP_RE.search(query)
    return match.group(0) if match else None


def _clean_query(query: str) -> str:
    return " ".join(query.strip().split())


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _classify_request(request: SearchOrchestratorRequest) -> tuple[str, float]:
    """Return intent and classifier confidence."""
    if request.mode != "auto":
        return request.mode, 1.0

    query = _clean_query(request.query)
    lower = query.lower()
    if request.zip_code or _extract_zip(query):
        return "local", 0.78
    if "near me" in lower or lower.startswith("near ") or " nearby" in lower:
        return "local", 0.66
    if (
        request.site_filter
        or request.file_type
        or request.exact_phrase
        or request.exclude_terms
        or request.inurl
        or request.intitle
        or _contains_any(lower, _DORK_MARKERS)
    ):
        return "deep", 0.82
    if _contains_any(
        lower,
        (
            "breaking",
            "latest",
            "today",
            "this week",
            "news",
            "press release",
            "announced",
        ),
    ):
        return "news", 0.74
    if _contains_any(
        lower,
        ("image", "images", "photo", "photos", "picture", "logo", "wallpaper"),
    ):
        return "images", 0.73
    if _contains_any(lower, ("video", "videos", "youtube", "watch", "trailer")):
        return "videos", 0.73
    if _contains_any(
        lower,
        (
            "buy",
            "coupon",
            "deal",
            "discount",
            "for sale",
            "price",
            "pricing",
            "shop",
            "shopping",
        ),
    ) or "$" in lower:
        return "ecommerce", 0.76
    return "fusion", 0.62


def _route_plan(request: SearchOrchestratorRequest, intent: str) -> List[str]:
    """Build a bounded parallel plan with fallback coverage."""
    if request.mode == "web":
        return ["web"]
    if request.mode == "fusion":
        return ["fusion", "web"]
    if request.mode != "auto":
        if request.mode == "local" and not (
            request.zip_code or _extract_zip(request.query)
        ):
            return ["fusion", "web"]
        fallback = "fusion" if request.mode not in {"fusion", "web"} else "web"
        return _unique([request.mode, fallback, "web"])

    if intent == "local":
        if not (request.zip_code or _extract_zip(request.query)):
            return ["fusion", "web"]
        return ["local", "fusion", "web"]
    if intent == "deep":
        return ["deep", "fusion", "web"]
    if intent == "news":
        return ["news", "google-news", "web"]
    if intent in {"images", "videos"}:
        return [intent, "fusion", "web"]
    if intent == "ecommerce":
        return ["ecommerce", "fusion", "web"]
    return ["fusion", "web"]


def _preflight_warnings(request: SearchOrchestratorRequest, intent: str) -> List[str]:
    warnings: List[str] = []
    if intent == "local" and not (request.zip_code or _extract_zip(request.query)):
        warnings.append("local map search needs zip_code; using web/fusion fallback")
    return warnings


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _route_timeout_seconds(request: SearchOrchestratorRequest, route: str) -> float:
    requested = max(request.timeout / 1000.0, 5.0)
    return min(requested, _ROUTE_TIMEOUT_CAPS.get(route, 45.0))


def _route_backoff_outcome(route: str) -> Optional[RouteOutcome]:
    if route not in _ROUTE_BACKOFF_ELIGIBLE:
        return None
    state = _ROUTE_HEALTH.get(route)
    if not state:
        return None
    failures = int(state.get("failures", 0))
    last_failure = float(state.get("last_failure", 0.0))
    if failures < _ROUTE_FAILURE_LIMIT:
        return None
    age = time.time() - last_failure
    if age >= _ROUTE_BACKOFF_S:
        return None
    return RouteOutcome(
        route=route,
        status="skipped",
        error="Route cooling down after %d failures" % failures,
        detail="Retry in %ds" % int(_ROUTE_BACKOFF_S - age),
    )


def _record_route_health(outcome: RouteOutcome) -> None:
    if outcome.route not in _ROUTE_BACKOFF_ELIGIBLE:
        return
    if outcome.status == "ok":
        _ROUTE_HEALTH.pop(outcome.route, None)
        return
    if outcome.status != "fail":
        return
    state = _ROUTE_HEALTH.setdefault(
        outcome.route, {"failures": 0, "last_failure": 0.0}
    )
    state["failures"] = int(state.get("failures", 0)) + 1
    state["last_failure"] = time.time()


def _position_score(route: str, position: int, raw_score: Optional[float] = None) -> float:
    base = _ROUTE_BASE_SCORE.get(route, 0.7)
    penalty = min(max(position - 1, 0) * 0.025, 0.28)
    route_score = max(0.05, base - penalty)
    if raw_score is None:
        return round(route_score, 4)
    return round(min(1.0, max(route_score, (base * 0.55) + (raw_score * 0.45))), 4)


def _normalize_url_key(url: str) -> str:
    if not url:
        return ""
    if url.startswith("local://"):
        return url.lower()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower().strip()
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    cleaned = parts._replace(
        scheme=parts.scheme.lower(),
        netloc=netloc,
        path=path,
        query=urlencode(sorted(query)),
        fragment="",
    )
    return urlunsplit(cleaned)


def _metadata(**values: Any) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [])}


def _normalize_web(response: Any, route: str = "web") -> List[Dict[str, Any]]:
    data = _as_dict(response)
    engine = data.get("engine") or route
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(data.get("results")), 1):
        url = item.get("url") or ""
        if not url:
            continue
        out.append({
            "title": item.get("title") or url,
            "url": url,
            "snippet": item.get("snippet"),
            "source": engine,
            "kind": "web",
            "score": _position_score(route, item.get("position") or index),
            "content": item.get("content"),
            "metadata": _metadata(route=route, position=item.get("position") or index),
        })
    return out


def _normalize_fusion(response: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(_as_dict(response).get("results")), 1):
        url = item.get("url") or ""
        if not url:
            continue
        raw_score = item.get("score")
        out.append({
            "title": item.get("title") or url,
            "url": url,
            "snippet": item.get("snippet"),
            "source": item.get("source") or "fusion",
            "kind": "web",
            "score": _position_score("fusion", item.get("position") or index, raw_score),
            "metadata": _metadata(
                route="fusion",
                position=item.get("position") or index,
                date=item.get("date"),
                raw_score=raw_score,
            ),
        })
    return out


def _normalize_media(response: Any, route: str) -> List[Dict[str, Any]]:
    data = _as_dict(response)
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(data.get("results")), 1):
        url = item.get("url") or ""
        if not url:
            continue
        out.append({
            "title": item.get("title") or url,
            "url": url,
            "snippet": item.get("snippet") or item.get("published") or item.get("duration"),
            "source": data.get("engine") or "bing",
            "kind": route,
            "score": _position_score(route, index),
            "metadata": _metadata(
                route=route,
                thumbnail=item.get("thumbnail"),
                duration=item.get("duration"),
                published=item.get("published"),
            ),
        })
    return out


def _normalize_deep(response: Any, route: str) -> List[Dict[str, Any]]:
    data = _as_dict(response)
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(data.get("results")), 1):
        url = item.get("url") or ""
        if not url:
            continue
        raw_score = item.get("score")
        title = item.get("title") or item.get("headline") or url
        out.append({
            "title": title,
            "url": url,
            "snippet": item.get("snippet"),
            "source": item.get("source") or route,
            "kind": "news" if route == "google-news" else "web",
            "score": _position_score(route, index, raw_score),
            "metadata": _metadata(
                route=route,
                date=item.get("date"),
                dork_query=data.get("dork_query"),
                raw_score=raw_score,
            ),
        })
    return out


def _normalize_local(response: Any) -> List[Dict[str, Any]]:
    data = _as_dict(response)
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(data.get("results")), 1):
        name = item.get("name") or "Local result"
        website = item.get("website")
        fallback_url = "local://%s" % quote("%s %s" % (name, item.get("address") or ""))
        snippet = " | ".join(
            part
            for part in (item.get("category"), item.get("address"), item.get("phone"))
            if part
        )
        out.append({
            "title": name,
            "url": website or fallback_url,
            "snippet": snippet or None,
            "source": "google-maps",
            "kind": "local",
            "score": _position_score("local", index),
            "metadata": _metadata(
                route="local",
                address=item.get("address"),
                rating=item.get("rating"),
                review_count=item.get("review_count"),
                phone=item.get("phone"),
                website=website,
                category=item.get("category"),
                hours=item.get("hours"),
                zip_code=data.get("zip_code"),
            ),
        })
    return out


def _normalize_ecommerce(response: Any) -> List[Dict[str, Any]]:
    data = _as_dict(response)
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(_items(data.get("results")), 1):
        url = item.get("url") or item.get("store_url") or ""
        if not url:
            continue
        price = item.get("price")
        snippet = " ".join(
            part
            for part in (
                "$%.2f" % price if isinstance(price, (int, float)) else None,
                item.get("platform"),
                "in stock" if item.get("in_stock") else None,
            )
            if part
        )
        out.append({
            "title": item.get("title") or url,
            "url": url,
            "snippet": snippet or None,
            "source": item.get("platform") or "ecommerce",
            "kind": "ecommerce",
            "score": _position_score("ecommerce", index),
            "metadata": _metadata(
                route="ecommerce",
                price=price,
                price_original=item.get("price_original"),
                currency=item.get("currency"),
                image=item.get("image"),
                in_stock=item.get("in_stock"),
                discount_pct=item.get("discount_pct"),
                store_url=item.get("store_url"),
                platforms_searched=data.get("platforms_searched"),
            ),
        })
    return out


def _normalize_response(route: str, response: Any) -> List[Dict[str, Any]]:
    if route == "web":
        return _normalize_web(response)
    if route == "fusion":
        return _normalize_fusion(response)
    if route in {"images", "news", "videos"}:
        return _normalize_media(response, route)
    if route in {"deep", "google-news"}:
        return _normalize_deep(response, route)
    if route == "local":
        return _normalize_local(response)
    if route == "ecommerce":
        return _normalize_ecommerce(response)
    return []


def _route_engine(route: str, response: Any) -> Optional[str]:
    data = _as_dict(response)
    engine = data.get("engine")
    if engine:
        return str(engine)
    sources = data.get("sources_used") or data.get("platforms_searched")
    if sources:
        return ", ".join(str(source) for source in sources)
    return None


async def _call_route(route: str, request: SearchOrchestratorRequest) -> Any:
    count = min(request.count, 50)
    if route == "web":
        return await search_web(
            SearchRequest(
                query=request.query,
                engine=request.engine,
                num_results=count,
                timeout=request.timeout,
                language=request.language,
                country=request.country,
                include_content=request.include_content,
            )
        )
    if route == "fusion":
        return await asyncio.to_thread(
            search_fusion,
            FusionRequest(
                query=request.query,
                count=count,
                sources=request.sources,
                min_score=request.min_score,
                dedupe=request.dedupe,
            ),
        )
    if route == "images":
        return await search_images(ImageSearchRequest(query=request.query, count=count))
    if route == "news":
        return await search_news(NewsSearchRequest(query=request.query, count=count))
    if route == "videos":
        return await search_videos(VideoSearchRequest(query=request.query, count=count))
    if route == "deep":
        return await search_deep(
            DeepSearchRequest(
                query=request.query,
                site_filter=request.site_filter,
                file_type=request.file_type,
                exact_phrase=request.exact_phrase,
                exclude_terms=request.exclude_terms or None,
                inurl=request.inurl,
                intitle=request.intitle,
                num_results=count,
                timeout=request.timeout,
            )
        )
    if route == "google-news":
        return await search_news_google(
            NewsDeepRequest(
                query=request.query,
                num_results=count,
                timeout=request.timeout,
            )
        )
    if route == "local":
        zip_code = request.zip_code or _extract_zip(request.query)
        if not zip_code:
            raise ValueError("ZIP required for local search")
        return await search_local(
            LocalSearchRequest(
                query=request.query,
                zip_code=zip_code,
                limit=min(count, 20),
            )
        )
    if route == "ecommerce":
        return await search_ecommerce(
            EcommerceSearchRequest(
                query=request.query,
                num_results=count,
                platforms=request.platforms,
                max_price=request.max_price,
                min_price=request.min_price,
            )
        )
    raise ValueError("Unknown search route: %s" % route)


async def _execute_route(
    route: str,
    request: SearchOrchestratorRequest,
) -> tuple[List[Dict[str, Any]], RouteOutcome]:
    backoff = _route_backoff_outcome(route)
    if backoff is not None:
        return [], backoff

    start = timer_ms()
    try:
        response = await asyncio.wait_for(
            _call_route(route, request),
            timeout=_route_timeout_seconds(request, route),
        )
    except ValueError as exc:
        elapsed = int(timer_ms() - start)
        outcome = RouteOutcome(
            route=route,
            status="skipped" if "ZIP required" in str(exc) else "fail",
            elapsed_ms=elapsed,
            error=str(exc),
        )
        _record_route_health(outcome)
        return [], outcome
    except asyncio.TimeoutError:
        elapsed = int(timer_ms() - start)
        outcome = RouteOutcome(
            route=route,
            status="fail",
            elapsed_ms=elapsed,
            error="Route timed out after %.1fs" % _route_timeout_seconds(request, route),
        )
        _record_route_health(outcome)
        return [], outcome
    except Exception as exc:  # endpoint-level guard: one route cannot kill orchestration
        logger.warning("Orchestrated route '%s' failed: %s", route, type(exc).__name__)
        elapsed = int(timer_ms() - start)
        outcome = RouteOutcome(
            route=route,
            status="fail",
            elapsed_ms=elapsed,
            error=type(exc).__name__,
        )
        _record_route_health(outcome)
        return [], outcome

    elapsed = int(timer_ms() - start)
    data = _as_dict(response)
    records = _normalize_response(route, response)
    error = data.get("error")
    status: RouteStatus = "ok" if records else "warn"
    detail = None if records else "No normalized results"
    if error and records:
        detail = "Partial: %s" % error
    outcome = RouteOutcome(
        route=route,
        status=status,
        elapsed_ms=elapsed,
        result_count=len(records),
        engine=_route_engine(route, response),
        error=error if not records else None,
        detail=detail,
    )
    _record_route_health(outcome)
    return records, outcome


def _merge_duplicate(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    routes = set(existing.get("metadata", {}).get("routes", []))
    routes.add(existing.get("metadata", {}).get("route") or existing.get("source") or "")
    routes.add(incoming.get("metadata", {}).get("route") or incoming.get("source") or "")
    routes.discard("")
    if incoming.get("score", 0.0) > existing.get("score", 0.0):
        winner, loser = incoming, existing
    else:
        winner, loser = existing, incoming
    metadata = {**loser.get("metadata", {}), **winner.get("metadata", {})}
    metadata["routes"] = sorted(routes)
    if loser.get("snippet") and not winner.get("snippet"):
        winner["snippet"] = loser["snippet"]
    winner["metadata"] = metadata
    return winner


def _dedupe_and_rank(records: List[Dict[str, Any]], limit: int) -> List[OrchestratedResult]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in records:
        key = _normalize_url_key(item.get("url", ""))
        if not key:
            key = "title:%s" % (item.get("title") or "").lower().strip()
        if not key:
            continue
        item.setdefault("metadata", {})
        item["metadata"].setdefault("route", item.get("metadata", {}).get("route"))
        if key in by_key:
            by_key[key] = _merge_duplicate(by_key[key], item)
        else:
            by_key[key] = item

    ranked = sorted(
        by_key.values(),
        key=lambda item: (
            item.get("score", 0.0),
            len(item.get("snippet") or ""),
            item.get("title") or "",
        ),
        reverse=True,
    )
    return [OrchestratedResult(**item) for item in ranked[:limit]]


def _confidence(
    results: List[OrchestratedResult],
    outcomes: List[RouteOutcome],
    classifier_confidence: float,
    requested_count: int,
) -> float:
    if not results:
        return 0.0
    ok_routes = sum(1 for outcome in outcomes if outcome.status == "ok")
    attempted = sum(1 for outcome in outcomes if outcome.status != "skipped")
    coverage = min(len(results) / max(requested_count, 1), 1.0)
    route_health = ok_routes / max(attempted, 1)
    value = (classifier_confidence * 0.35) + (coverage * 0.35) + (route_health * 0.30)
    return round(min(max(value, 0.05), 0.98), 3)


def _warnings(outcomes: List[RouteOutcome]) -> List[str]:
    warnings: List[str] = []
    for outcome in outcomes:
        if outcome.status == "ok":
            continue
        detail = outcome.error or outcome.detail or outcome.status
        warnings.append("%s: %s" % (outcome.route, detail))
    return warnings


def _route_health_snapshot() -> Dict[str, Dict[str, Any]]:
    now = time.time()
    snapshot: Dict[str, Dict[str, Any]] = {}
    for route, state in sorted(_ROUTE_HEALTH.items()):
        failures = int(state.get("failures", 0))
        last_failure = float(state.get("last_failure", 0.0))
        remaining = 0
        if failures >= _ROUTE_FAILURE_LIMIT:
            remaining = max(0, int(_ROUTE_BACKOFF_S - (now - last_failure)))
        snapshot[route] = {
            "failures": failures,
            "last_failure_age_s": int(now - last_failure) if last_failure else None,
            "backoff_remaining_s": remaining,
        }
    return snapshot


def _read_source_health() -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(_SOURCE_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            out[str(key)] = dict(value)
    return out


def _write_source_health(payload: Dict[str, Dict[str, Any]]) -> None:
    try:
        _SOURCE_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SOURCE_HEALTH_PATH.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write search source health: %s", exc)


def _source_health_key(check: SearchDiagnosticCheck | Dict[str, Any]) -> str:
    if isinstance(check, SearchDiagnosticCheck):
        source = check.source
        group = check.group
        engines = check.engines
    else:
        source = str(check.get("source", "searxng"))
        group = str(check.get("group", ""))
        engines = str(check.get("engines", ""))
    suffix = group or engines or "default"
    return "%s:%s" % (source or "source", suffix)


def _source_health_status(state: Dict[str, Any], now: float) -> str:
    quarantined_until = float(state.get("quarantined_until_s", 0.0) or 0.0)
    if quarantined_until > now:
        return "quarantined"
    score = float(state.get("score", _SOURCE_HEALTH_SCORE_START))
    if score < 0.65 or int(state.get("consecutive_warnings", 0)) > 0:
        return "degraded"
    return "healthy"


def _source_health_snapshot() -> Dict[str, Dict[str, Any]]:
    now = time.time()
    payload = _read_source_health()
    for state in payload.values():
        state["health_status"] = _source_health_status(state, now)
        quarantined_until = float(state.get("quarantined_until_s", 0.0) or 0.0)
        state["quarantine_remaining_s"] = max(0, int(quarantined_until - now))
    return payload


def _update_source_health(
    checks: List[SearchDiagnosticCheck],
    now: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    now = time.time() if now is None else now
    payload = _read_source_health()
    for check in checks:
        key = check.health_key or _source_health_key(check)
        score = _SOURCE_HEALTH_SCORE_START
        state = payload.setdefault(key, {"score": score})
        score = float(state.get("score", _SOURCE_HEALTH_SCORE_START))
        consecutive_failures = int(state.get("consecutive_failures", 0))
        consecutive_warnings = int(state.get("consecutive_warnings", 0))
        consecutive_successes = int(state.get("consecutive_successes", 0))

        if check.status == "ok":
            score = min(1.0, score + _SOURCE_HEALTH_SUCCESS_RECOVERY)
            consecutive_successes += 1
            consecutive_failures = 0
            consecutive_warnings = 0
            if consecutive_successes >= 1:
                state["quarantined_until_s"] = 0.0
        elif check.status == "warn":
            score = max(0.0, score - _SOURCE_HEALTH_WARNING_PENALTY)
            consecutive_warnings += 1
            consecutive_failures = 0
            consecutive_successes = 0
        else:
            score = max(0.0, score - _SOURCE_HEALTH_FAILURE_PENALTY)
            consecutive_failures += 1
            consecutive_successes = 0

        should_quarantine = (
            consecutive_failures >= _SOURCE_QUARANTINE_FAILURE_LIMIT
            or consecutive_warnings >= _SOURCE_QUARANTINE_WARNING_LIMIT
            or score <= _SOURCE_QUARANTINE_SCORE
        )
        if should_quarantine:
            state["quarantined_until_s"] = max(
                float(state.get("quarantined_until_s", 0.0) or 0.0),
                now + _SOURCE_QUARANTINE_S,
            )

        state.update({
            "source": check.source,
            "group": check.group,
            "engines": check.engines,
            "score": round(score, 3),
            "last_status": check.status,
            "last_detail": check.detail,
            "last_result_count": check.result_count,
            "last_warning_count": check.warning_count,
            "last_elapsed_ms": check.elapsed_ms,
            "last_checked_s": now,
            "consecutive_failures": consecutive_failures,
            "consecutive_warnings": consecutive_warnings,
            "consecutive_successes": consecutive_successes,
        })
        state["health_status"] = _source_health_status(state, now)
        quarantined_until = float(state.get("quarantined_until_s", 0.0) or 0.0)
        state["quarantine_remaining_s"] = max(0, int(quarantined_until - now))
    _write_source_health(payload)
    return payload


def orchestrator_runtime_status() -> SearchOrchestratorStatus:
    """Return runtime cache/backoff health without running a search."""
    _cache_load_once()
    return SearchOrchestratorStatus(
        cache_entries=len(_ORCHESTRATOR_CACHE),
        cache_path=str(_ORCHESTRATOR_CACHE_PATH),
        route_health=_route_health_snapshot(),
        source_health_path=str(_SOURCE_HEALTH_PATH),
        source_health=_source_health_snapshot(),
    )


def _source_groups() -> Dict[str, str]:
    """Return the configured source groups used by direct SearXNG probes."""
    return {
        "web": _cfg.SEARXNG_WEB_ENGINES,
        "news": _cfg.SEARXNG_NEWS_ENGINES,
        "images": _cfg.SEARXNG_IMAGE_ENGINES,
        "videos": _cfg.SEARXNG_VIDEO_ENGINES,
    }


def _searxng_golden_checks() -> List[Dict[str, Any]]:
    """Small high-signal query suite for upstream quality proof."""
    groups = _source_groups()
    checks: List[Dict[str, Any]] = []
    for item in _cfg.source_diagnostic_checks():
        spec = dict(item)
        group = str(spec.get("group", "") or "")
        if group:
            spec["engines"] = groups.get(group, "")
        spec.setdefault("source", "searxng")
        spec.setdefault("engines", groups["web"])
        spec.setdefault("min_results", 1)
        spec["health_key"] = _source_health_key(spec)
        checks.append(spec)
    return checks


def _searxng_check_sync(spec: Dict[str, Any]) -> SearchDiagnosticCheck:
    """Run one SearXNG JSON probe and classify result quality."""
    import urllib.error
    import urllib.request

    started = timer_ms()
    query = str(spec.get("query", "openai"))
    engines = str(spec.get("engines", _cfg.SEARXNG_WEB_ENGINES))
    group = str(spec.get("group", "") or "")
    health_key = str(spec.get("health_key") or _source_health_key(spec))
    min_results = int(spec.get("min_results", 1))
    url = _cfg.SEARXNG_URL.rstrip("/") + "/search?" + urlencode({
        "q": query,
        "format": "json",
        "engines": engines,
    })
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-Real-IP": "127.0.0.1"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read(524288)
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        results = payload.get("results", []) if isinstance(payload, dict) else []
        warnings = payload.get("unresponsive_engines", []) if isinstance(payload, dict) else []
        result_count = len(results) if isinstance(results, list) else 0
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        sample_title = None
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                sample_title = str(first.get("title") or "") or None
        status: DiagnosticStatus = "ok"
        detail = "quality threshold met"
        if result_count < min_results:
            status = "fail"
            detail = "only %d result(s), expected at least %d" % (
                result_count, min_results,
            )
        elif warning_count:
            status = "warn"
            detail = "%d upstream warning(s)" % warning_count
        return SearchDiagnosticCheck(
            name=str(spec.get("name", "searxng-check")),
            source=str(spec.get("source", "searxng")),
            group=group,
            health_key=health_key,
            query=query,
            engines=engines,
            status=status,
            elapsed_ms=int(timer_ms() - started),
            result_count=result_count,
            warning_count=warning_count,
            min_results=min_results,
            detail=detail,
            sample_title=sample_title,
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        urllib.error.URLError,
    ) as exc:
        return SearchDiagnosticCheck(
            name=str(spec.get("name", "searxng-check")),
            source=str(spec.get("source", "searxng")),
            group=group,
            health_key=health_key,
            query=query,
            engines=engines,
            status="fail",
            elapsed_ms=int(timer_ms() - started),
            result_count=0,
            warning_count=0,
            min_results=min_results,
            detail="%s: %s" % (type(exc).__name__, exc),
        )


def _diagnostic_summary(checks: List[SearchDiagnosticCheck]) -> Dict[str, int]:
    summary = {"total": len(checks), "ok": 0, "warn": 0, "fail": 0}
    for check in checks:
        summary[check.status] = summary.get(check.status, 0) + 1
    return summary


def _diagnostic_status(summary: Dict[str, int]) -> DiagnosticStatus:
    if summary.get("fail", 0):
        return "fail"
    if summary.get("warn", 0):
        return "warn"
    return "ok"


def _append_diagnostic_log(response: SearchDiagnosticsResponse) -> None:
    """Persist one compact diagnostics sample for trend/debug proof."""
    try:
        _DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        checks = [
            {
                "name": check.name,
                "group": check.group,
                "health_key": check.health_key,
                "status": check.status,
                "result_count": check.result_count,
                "warning_count": check.warning_count,
                "elapsed_ms": check.elapsed_ms,
            }
            for check in response.checks
        ]
        row = {
            "ts": response.generated_at_s,
            "status": response.status,
            "elapsed_ms": response.elapsed_ms,
            "summary": response.summary,
            "checks": checks,
        }
        with _DIAGNOSTIC_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("Could not write search diagnostics log: %s", exc)


async def run_search_diagnostics(force: bool = False) -> SearchDiagnosticsResponse:
    """Run or return cached high-signal search source diagnostics."""
    global _DIAGNOSTIC_CACHE
    now = time.time()
    if not force and _DIAGNOSTIC_CACHE is not None:
        cached_at, cached = _DIAGNOSTIC_CACHE
        if now - cached_at <= _DIAGNOSTIC_CACHE_TTL_S:
            payload = _model_dump(cached)
            payload["cache_hit"] = True
            return SearchDiagnosticsResponse(**payload)

    start = timer_ms()
    checks = await asyncio.gather(
        *(
            asyncio.to_thread(_searxng_check_sync, spec)
            for spec in _searxng_golden_checks()
        )
    )
    health = _update_source_health(list(checks), now=now)
    summary = _diagnostic_summary(list(checks))
    response = SearchDiagnosticsResponse(
        status=_diagnostic_status(summary),
        elapsed_ms=int(timer_ms() - start),
        generated_at_s=now,
        cache_hit=False,
        log_path=str(_DIAGNOSTIC_LOG_PATH),
        registry_path=str(_cfg.SOURCE_REGISTRY_PATH),
        source_health_path=str(_SOURCE_HEALTH_PATH),
        checks=list(checks),
        summary=summary,
        source_groups=_source_groups(),
        source_health=health,
    )
    _DIAGNOSTIC_CACHE = (time.time(), response)
    _append_diagnostic_log(response)
    return response


@router.post(
    "/orchestrate",
    response_model=SearchOrchestratorResponse,
    summary="Auto-orchestrated robust search across CrawlRX routes",
)
async def search_orchestrate(
    request: SearchOrchestratorRequest,
) -> SearchOrchestratorResponse:
    """Classify, parallelize, dedupe, rank, and return the best CrawlRX search."""
    start = timer_ms()
    key = _cache_key(request)
    fresh = _cache_get(key, _ORCHESTRATOR_CACHE_TTL_S)
    if fresh:
        return _cached_response(fresh, stale=False)

    intent, classifier_confidence = _classify_request(request)
    plan = _route_plan(request, intent)
    preflight_warnings = _preflight_warnings(request, intent)
    results_and_outcomes = await asyncio.gather(
        *(_execute_route(route, request) for route in plan),
        return_exceptions=False,
    )
    records: List[Dict[str, Any]] = []
    outcomes: List[RouteOutcome] = []
    for route_records, outcome in results_and_outcomes:
        records.extend(route_records)
        outcomes.append(outcome)

    results = _dedupe_and_rank(records, request.count)
    warnings = preflight_warnings + _warnings(outcomes)
    elapsed = int(timer_ms() - start)
    error = None
    if not results:
        stale = _cache_get(key, _ORCHESTRATOR_STALE_TTL_S)
        if stale:
            return _cached_response(stale, stale=True)
        error = "No results returned by orchestrated routes"
    response = SearchOrchestratorResponse(
        success=bool(results),
        query=request.query,
        mode=request.mode,
        intent=intent,
        confidence=_confidence(
            results, outcomes, classifier_confidence, request.count,
        ),
        results=results,
        total=len(results),
        elapsed_ms=elapsed,
        routes=outcomes,
        warnings=warnings,
        error=error,
    )
    _cache_set(key, response)
    return response


@router.get(
    "/orchestrate/status",
    response_model=SearchOrchestratorStatus,
    summary="Search orchestrator runtime status",
)
def search_orchestrate_status() -> SearchOrchestratorStatus:
    """Expose cache and route backoff status for operators."""
    return orchestrator_runtime_status()


@router.get(
    "/diagnostics",
    response_model=SearchDiagnosticsResponse,
    summary="Run high-signal search source diagnostics",
)
async def search_diagnostics(force: bool = False) -> SearchDiagnosticsResponse:
    """Expose live golden-query proof for search quality monitoring."""
    return await run_search_diagnostics(force=force)


@router.get(
    "/source-registry",
    summary="Search source registry and active engine groups",
)
def search_source_registry() -> Dict[str, Any]:
    """Expose the source registry loaded by CrawlRX."""
    return {
        "path": str(_cfg.SOURCE_REGISTRY_PATH),
        "registry": _cfg.SOURCE_REGISTRY,
        "active_groups": _source_groups(),
    }


@router.get(
    "/source-health",
    summary="Persistent search source health and quarantine state",
)
def search_source_health() -> Dict[str, Any]:
    """Expose the persistent source score/quarantine ledger."""
    return {
        "path": str(_SOURCE_HEALTH_PATH),
        "quarantine_s": _SOURCE_QUARANTINE_S,
        "failure_limit": _SOURCE_QUARANTINE_FAILURE_LIMIT,
        "warning_limit": _SOURCE_QUARANTINE_WARNING_LIMIT,
        "quarantine_score": _SOURCE_QUARANTINE_SCORE,
        "sources": _source_health_snapshot(),
    }
