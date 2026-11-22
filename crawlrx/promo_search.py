"""
promo_search.py -- Unified promo/deal search endpoint for CrawlRX.
Aggregates results from multiple promo scrapers in parallel.
"""

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ._scraper_helpers import SCRAPE_FAILURE_EXCEPTIONS
from .honey_scraper import scrape_honey
from .promo_scraper import search_promo_codes
from .rakuten_scraper import scrape_rakuten

logger = logging.getLogger("crawlrx")

router = APIRouter(tags=["promo"])

# Thread / executor failures that should not abort the aggregation request.
_THREAD_EXECUTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    RuntimeError,
    OSError,
)


class PromoSearchRequest(BaseModel):
    query: str
    merchant: str = ""


class PromoResult(BaseModel):
    code: str
    discount: str
    description: str
    source: str
    verified: bool


class PromoSearchResponse(BaseModel):
    success: bool
    results: List[PromoResult]
    total: int
    elapsed_ms: int


def _safe_scrape(
    label: str,
    fn: Callable[[str], List[Dict[str, Any]]],
    target: str,
) -> List[Dict[str, Any]]:
    """Run a scraper, swallow narrow scrape failures, log + return []."""
    try:
        return fn(target)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("%s failed (%s): %s", label, type(exc).__name__, exc)
        return []


def _fetch_promo(query: str, merchant: str) -> List[Dict[str, Any]]:
    return _safe_scrape("promo_scraper", search_promo_codes, merchant or query)


def _fetch_honey(query: str, merchant: str) -> List[Dict[str, Any]]:
    return _safe_scrape("honey_scraper", scrape_honey, merchant or query)


def _fetch_rakuten(query: str, merchant: str) -> List[Dict[str, Any]]:
    return _safe_scrape("rakuten_scraper", scrape_rakuten, merchant or query)


def _deduplicate(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    unique = []
    for r in results:
        code = str(r.get("code", "")).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        unique.append(r)
    return unique


def _normalize(r: Dict[str, Any]) -> Optional[PromoResult]:
    code = str(r.get("code", "")).strip()
    if not code:
        return None
    try:
        return PromoResult(
            code=code,
            discount=str(r.get("discount", r.get("value", ""))).strip(),
            description=str(r.get("description", r.get("title", ""))).strip(),
            source=str(r.get("source", "unknown")).strip(),
            verified=bool(r.get("verified", r.get("is_verified", False))),
        )
    except (TypeError, ValueError, AttributeError) as exc:
        logger.error("Failed to normalize promo result (%s): %s", type(exc).__name__, exc)
        return None


def _drain_future(future: "Future[List[Dict[str, Any]]]") -> List[Dict[str, Any]]:
    """Pull a single scraper future's batch, swallowing narrow thread failures."""
    try:
        batch = future.result(timeout=20)
    except _THREAD_EXECUTION_EXCEPTIONS as exc:
        logger.error("[PROMO_SEARCH] Scraper thread failed (%s): %s", type(exc).__name__, exc)
        return []
    return batch or []


def _run_parallel_fetch(query: str, merchant: str) -> List[Dict[str, Any]]:
    """Fan out to all scrapers in parallel; aggregate into a single flat list."""
    aggregated: List[Dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(_fetch_promo, query, merchant),
                executor.submit(_fetch_honey, query, merchant),
                executor.submit(_fetch_rakuten, query, merchant),
            ]
            for future in futures:
                aggregated.extend(_drain_future(future))
    except _THREAD_EXECUTION_EXCEPTIONS as exc:
        logger.error(
            "[PROMO_SEARCH] ThreadPoolExecutor failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None  # explicit recovery: non-fatal: _THREAD_EXECUTION_EXCEPTIONS is recorded but does not abort the caller
    return aggregated


@router.post("/search/promo", response_model=PromoSearchResponse)
def search_promo(request: PromoSearchRequest) -> PromoSearchResponse:
    """Unified promo search across RetailMeNot, Coupons.com, Honey, and Rakuten.

    Runs all scrapers in parallel via a thread pool. Sync route handler so
    FastAPI runs it off the event loop without parking other coroutines.
    """
    start = time.time()
    query = request.query.strip()
    merchant = request.merchant.strip()

    logger.info("[PROMO_SEARCH] query='%s' merchant='%s'", query, merchant)

    all_results = _run_parallel_fetch(query, merchant)
    deduped = _deduplicate(all_results)
    normalized = [n for r in deduped if (n := _normalize(r))]
    elapsed_ms = int((time.time() - start) * 1000)

    logger.info(
        "[PROMO_SEARCH] raw=%d unique=%d normalized=%d elapsed=%dms",
        len(all_results),
        len(deduped),
        len(normalized),
        elapsed_ms,
    )

    return PromoSearchResponse(
        success=bool(normalized),
        results=normalized,
        total=len(normalized),
        elapsed_ms=elapsed_ms,
    )
