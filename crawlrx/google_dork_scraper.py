"""
Google dork scraper for CrawlRX.
FastAPI router: POST /scrape/google-dork
Uses CrawlRX /search/quick endpoint for Google dork queries.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter
from pydantic import BaseModel
from pydantic import ValidationError

from ._scraper_helpers import PARSE_EXCEPTIONS

logger = logging.getLogger(__name__)
router = APIRouter()

CRAWLRX_BASE = "http://localhost:7337"  # service_registry not yet wired (module split)
SEARCH_ENDPOINT = "%s/search/quick" % CRAWLRX_BASE


class GoogleDorkRequest(BaseModel):
    merchant: str
    query_type: str  # promo | price | review


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class GoogleDorkResponse(BaseModel):
    success: bool
    results: List[SearchResult]
    elapsed_ms: int


# Network/parse errors that bubble up from `requests.post(...).json()`.
_SEARCH_REQUEST_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.exceptions.RequestException,
    ValueError,
)


def _get_queries(merchant: str, query_type: str) -> List[str]:
    """Build Google dork queries based on query type."""
    queries = {
        "promo": [
            'site:reddit.com "%s" "promo code" OR "coupon code" 2026' % merchant,
            '"%s" "promo code" -site:%s' % (merchant, merchant),
            '"%s.com" coupon code verified 2026' % merchant,
        ],
        "price": [
            '"%s" price drop OR sale OR deal 2026' % merchant,
        ],
        "review": [
            'site:reddit.com "%s" review 2026' % merchant,
        ],
    }

    return queries.get(query_type, [])


def _search_quick(query: str) -> List[Dict[str, Any]]:
    """Call CrawlRX /search/quick endpoint."""
    payload = {"query": query, "limit": 10}
    headers = {"Content-Type": "application/json"}

    logger.debug("Calling CrawlRX search for query: %s", query[:50])
    try:
        resp = requests.post(SEARCH_ENDPOINT, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("Timeout calling CrawlRX for query: %s", query[:50])
        return []
    except _SEARCH_REQUEST_EXCEPTIONS as exc:
        logger.warning("Request failed for query %s: %s", query[:50], exc)
        return []

    results = data.get("results", []) if isinstance(data, dict) else []
    logger.debug("Got %d results for query", len(results))
    return results


def _empty_response(start: float) -> GoogleDorkResponse:
    """Build the failure response shared by every empty-query exit path."""
    return GoogleDorkResponse(
        success=False,
        results=[],
        elapsed_ms=int((time.time() - start) * 1000),
    )


def _absorb_results(
    raw_results: List[Dict[str, Any]],
    seen_urls: set[str],
    out: List[Dict[str, Any]],
) -> None:
    """Append unseen ``raw_results`` to ``out``, mutating ``seen_urls``."""
    for result in raw_results:
        url = result.get("url", "") if isinstance(result, dict) else ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        out.append(result)


def _safe_future_result(future) -> List[Dict[str, Any]]:
    """Return ``future.result()`` or ``[]`` on a known network/parse failure."""
    try:
        return future.result()
    except _SEARCH_REQUEST_EXCEPTIONS as exc:
        logger.error("Error processing query results: %s", exc)
        return []


def _drain_pool(executor: ThreadPoolExecutor, queries: List[str]) -> List[Dict[str, Any]]:
    """Submit queries, collect & dedupe their results."""
    futures = {executor.submit(_search_quick, q): q for q in queries}
    out: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for future in as_completed(futures):
        _absorb_results(_safe_future_result(future), seen_urls, out)
    return out


def _run_dork_queries(queries: List[str]) -> List[Dict[str, Any]]:
    """Fan out the dork queries through a thread pool, dedupe by URL."""
    workers = min(len(queries), 5)
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return _drain_pool(executor, queries)
    except RuntimeError as exc:
        logger.error("ThreadPoolExecutor error: %s", exc)
        return []


def _safe_coerce_one(raw: Dict[str, Any]) -> Optional[SearchResult]:
    """Coerce one raw dict into ``SearchResult``, returning None on bad shape."""
    try:
        return SearchResult(
            title=raw.get("title") or "",
            url=raw.get("url") or "",
            snippet=raw.get("snippet") or "",
        )
    except (ValidationError, *PARSE_EXCEPTIONS) as exc:
        logger.warning("Failed to parse result: %s", exc)
        # explicit recovery: validation/parse errors non-fatal; caller skips row
        return None


def _coerce_results(raw_results: List[Dict[str, Any]]) -> List[SearchResult]:
    """Convert raw search dicts into ``SearchResult`` rows, skipping bad shapes."""
    coerced: List[SearchResult] = []
    for raw in raw_results:
        result = _safe_coerce_one(raw)
        if result is not None:
            coerced.append(result)
    return coerced


@router.post("/scrape/google-dork", response_model=GoogleDorkResponse)
def scrape_google_dork(request: GoogleDorkRequest) -> GoogleDorkResponse:
    """Scrape using Google dork queries via CrawlRX search."""
    start = time.time()
    queries = _get_queries(request.merchant, request.query_type)
    if not queries:
        logger.warning("No queries defined for query_type: %s", request.query_type)
        return _empty_response(start)

    logger.info(
        "Running %d dork queries for merchant: %s, type: %s",
        len(queries),
        request.merchant,
        request.query_type,
    )
    raw_results = _run_dork_queries(queries)
    search_results = _coerce_results(raw_results)
    elapsed = int((time.time() - start) * 1000)
    logger.info("Returning %d unique results in %d ms", len(search_results), elapsed)
    return GoogleDorkResponse(success=True, results=search_results, elapsed_ms=elapsed)
