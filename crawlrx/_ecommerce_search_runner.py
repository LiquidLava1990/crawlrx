"""Orchestration helpers for the ecommerce-search pipeline.

Composes per-source adapters from :mod:`_ecommerce_search_sources` into
a priority ladder, enriches results, and returns the final response.
The HTTP route in :mod:`ecommerce_search` is a thin wrapper around
:func:`_search_ecommerce_impl` defined here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ._ecommerce_search_filters import (
    _apply_discount_pct,
    _ecommerce_sort_key,
    _filter_by_price,
    _get_store_url,
)
from ._ecommerce_search_models import (
    EcommerceSearchRequest,
    EcommerceSearchResponse,
    ProductResult,
)
from ._ecommerce_search_sources import (
    _ddgs_shopping_to_product,
    _detect_platform_for_url,
    _google_shopping_to_product,
    _scrape_store,
    _search_ddgs_shopping,
    _search_ecommerce_ddgs,
    _search_google_shopping_playwright,
    _search_searxng_direct,
    _searxng_to_product,
)
from ._scraper_helpers import (
    SCRAPE_FAILURE_EXCEPTIONS,
    network_exception_types,
)

logger = logging.getLogger('crawlrx.ecommerce_search')

_NETWORK_EXC = network_exception_types()
_FAN_OUT_EXC: tuple = (
    asyncio.TimeoutError,
    ImportError,
    RuntimeError,
    TypeError,
    ValueError,
    *_NETWORK_EXC,
    *SCRAPE_FAILURE_EXCEPTIONS,
)


# ─────────────────────────────────────────────
# Source-fan-out gather helpers
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class _SourceSpec:
    """Per-source configuration for the unified gather helper."""

    label: str
    fetch: Callable[[], Any]
    converter: Callable[[Dict[str, Any]], Optional[ProductResult]]
    timeout: float
    log_zero: bool


AddProductFn = Callable[[Optional[ProductResult]], bool]


async def _await_or_run_fetch(fetch: Callable[[], Any], timeout: float) -> Any:
    """Run ``fetch`` in an executor (or await coroutine) under ``timeout``."""
    loop = asyncio.get_running_loop()
    raw = await asyncio.wait_for(loop.run_in_executor(None, fetch), timeout=timeout)
    if asyncio.iscoroutine(raw):
        return await asyncio.wait_for(raw, timeout=timeout)
    return raw


async def _gather_one_source(
    spec: _SourceSpec,
    add_product: AddProductFn,
    platforms_searched: List[str],
) -> None:
    """Run one source: bounded fetch -> convert -> tag platform if any added."""
    try:
        raw = await _await_or_run_fetch(spec.fetch, spec.timeout)
    except _FAN_OUT_EXC as e:
        logger.warning("%s source failed: %s", spec.label, e)
        return  # explicit recovery: _FAN_OUT_EXC non-fatal; source contributes nothing
    added = sum(1 for item in raw if add_product(spec.converter(item)))
    if added:
        platforms_searched.append(spec.label)
        logger.info("%s returned %d results", spec.label, added)
    elif spec.log_zero:
        logger.info("%s returned 0 results", spec.label)


async def _gather_searxng_source(
    request: EcommerceSearchRequest,
    add_product: AddProductFn,
    platforms_searched: List[str],
) -> None:
    """Priority 1: SearXNG direct HTTP."""
    spec = _SourceSpec(
        label="searxng",
        fetch=lambda: _search_searxng_direct(f"buy {request.query}", 20),
        converter=_searxng_to_product,
        timeout=4,
        log_zero=True,
    )
    await _gather_one_source(spec, add_product, platforms_searched)


async def _gather_google_shopping_source(
    request: EcommerceSearchRequest,
    add_product: AddProductFn,
    platforms_searched: List[str],
) -> None:
    """Priority 2: Google Shopping via Playwright CDP."""
    spec = _SourceSpec(
        label="google_shopping",
        fetch=lambda: _search_google_shopping_playwright(
            request.query, request.num_results,
        ),
        converter=_google_shopping_to_product,
        timeout=13,
        log_zero=True,
    )
    await _gather_one_source(spec, add_product, platforms_searched)


async def _gather_ddgs_shopping_source(
    request: EcommerceSearchRequest,
    add_product: AddProductFn,
    platforms_searched: List[str],
    remaining: int,
) -> None:
    """Priority 3: DDGS site-targeted shopping fallback."""
    spec = _SourceSpec(
        label="ddgs_shopping",
        fetch=lambda: _search_ddgs_shopping(request.query, remaining),
        converter=_ddgs_shopping_to_product,
        timeout=8,
        log_zero=False,
    )
    await _gather_one_source(spec, add_product, platforms_searched)


# ─────────────────────────────────────────────
# Priority 4: SERP -> detect -> scrape fan-out
# ─────────────────────────────────────────────


async def _detect_platforms_for_serp(
    store_urls: List[str],
) -> List[Any]:
    """Probe each SERP store URL in parallel; ``Exception`` instances pass through."""
    loop = asyncio.get_running_loop()
    detect_executor = ThreadPoolExecutor(max_workers=5)
    try:
        detect_futures = [
            asyncio.wait_for(
                loop.run_in_executor(detect_executor, _detect_platform_for_url, u),
                timeout=4,
            )
            for u in store_urls
        ]
        return await asyncio.gather(*detect_futures, return_exceptions=True)
    finally:
        detect_executor.shutdown(wait=False)


def _filter_scrape_targets(
    request: EcommerceSearchRequest,
    store_urls: List[str],
    detect_results: List[Any],
    platforms_searched: List[str],
) -> List[tuple]:
    """Decide which (store, platform) pairs to scrape from detect output."""
    to_scrape: List[tuple] = []
    for store_url, info in zip(store_urls, detect_results):
        if isinstance(info, Exception):
            logger.warning("Platform detect exception for %s: %s", store_url, info)
            continue
        platform = info.get("platform", "unknown")
        has_ecommerce = info.get("has_ecommerce", False)
        if not has_ecommerce or platform not in ("shopify", "woocommerce", "generic"):
            continue
        if request.platforms and platform not in request.platforms:
            continue
        to_scrape.append((store_url, platform))
        if platform not in platforms_searched:
            platforms_searched.append(platform)
    return to_scrape


async def _run_platform_scrapes(
    request: EcommerceSearchRequest,
    to_scrape: List[tuple],
    add_product: AddProductFn,
) -> None:
    """Execute the per-platform scrapers in parallel."""
    loop = asyncio.get_running_loop()
    scrape_executor = ThreadPoolExecutor(max_workers=5)
    try:
        scrape_futures = [
            asyncio.wait_for(
                loop.run_in_executor(
                    scrape_executor, _scrape_store, su, pl, request.query,
                ),
                timeout=8,
            )
            for su, pl in to_scrape
        ]
        scrape_results = await asyncio.gather(
            *scrape_futures, return_exceptions=True,
        )
    finally:
        scrape_executor.shutdown(wait=False)
    for r in scrape_results:
        if isinstance(r, Exception):
            logger.warning("Scrape error: %s", r)
            continue
        if isinstance(r, list):
            for product in r:
                add_product(product)


def _serp_store_urls(serp_items: List[Dict[str, Any]]) -> List[str]:
    """Compose up to 8 unique store URLs from SERP items."""
    seen_stores: set = set()
    store_urls: List[str] = []
    for r in serp_items:
        store = _get_store_url(r.get("url", ""))
        if not store or store in seen_stores:
            continue
        seen_stores.add(store)
        store_urls.append(store)
        if len(store_urls) >= 8:
            break
    return store_urls


async def _gather_platform_scraper_source(
    request: EcommerceSearchRequest,
    add_product: AddProductFn,
    platforms_searched: List[str],
    seen_urls: set,
    remaining: int,
) -> None:
    """Priority 4: SERP -> detect -> scrape platform fan-out."""
    loop = asyncio.get_running_loop()
    try:
        ddgs_results = await asyncio.wait_for(
            loop.run_in_executor(
                None, _search_ecommerce_ddgs, request.query, remaining * 2,
            ),
            timeout=9,
        )
    except _FAN_OUT_EXC as e:
        logger.warning("Platform scraper flow failed: %s", e)
        return  # explicit recovery: _FAN_OUT_EXC non-fatal
    serp_items = [
        item for item in ddgs_results
        if (item.get("url", "") and item["url"] not in seen_urls)
    ]
    if not serp_items:
        return
    if "ddgs_search" not in platforms_searched:
        platforms_searched.append("ddgs_search")
    logger.info("DDGS store search returned %d candidates", len(serp_items))
    store_urls = _serp_store_urls(serp_items)
    if not store_urls:
        return
    detect_results = await _detect_platforms_for_serp(store_urls)
    to_scrape = _filter_scrape_targets(
        request, store_urls, detect_results, platforms_searched,
    )
    if to_scrape:
        await _run_platform_scrapes(request, to_scrape, add_product)


# ─────────────────────────────────────────────
# Top-level priority-ladder runner
# ─────────────────────────────────────────────


def _make_add_product(
    seen_urls: set, all_products: List[ProductResult],
) -> AddProductFn:
    """Build the closure that dedupes by URL and appends to ``all_products``."""

    def _add(product: Optional[ProductResult]) -> bool:
        if not product or not product.url or product.url in seen_urls:
            return False
        seen_urls.add(product.url)
        all_products.append(product)
        return True

    return _add


async def _run_priority_ladder(
    request: EcommerceSearchRequest,
    add_product: AddProductFn,
    all_products: List[ProductResult],
    platforms_searched: List[str],
    seen_urls: set,
) -> None:
    """Sequentially run the four priority sources, gating on result count."""
    await _gather_searxng_source(request, add_product, platforms_searched)
    if len(all_products) < 5:
        await _gather_google_shopping_source(
            request, add_product, platforms_searched,
        )
    if len(all_products) < request.num_results:
        await _gather_ddgs_shopping_source(
            request, add_product, platforms_searched,
            request.num_results - len(all_products),
        )
    remaining = request.num_results - len(all_products)
    if remaining > 0 and len(all_products) < 10:
        await _gather_platform_scraper_source(
            request, add_product, platforms_searched, seen_urls, remaining,
        )


def _finalize(
    products: List[ProductResult], request: EcommerceSearchRequest,
) -> List[ProductResult]:
    """Apply discount, price filter, sort and slice to ``num_results``."""
    _apply_discount_pct(products)
    products = _filter_by_price(products, request.min_price, request.max_price)
    products.sort(key=_ecommerce_sort_key)
    return products[:request.num_results]


async def _search_ecommerce_impl(
    request: EcommerceSearchRequest,
) -> EcommerceSearchResponse:
    """Core implementation -- always completes or raises within 45s."""
    t0 = time.time()
    all_products: List[ProductResult] = []
    platforms_searched: List[str] = []
    seen_urls: set = set()
    add_product = _make_add_product(seen_urls, all_products)

    await _run_priority_ladder(
        request, add_product, all_products, platforms_searched, seen_urls,
    )
    final = _finalize(all_products, request)

    return EcommerceSearchResponse(
        success=True,
        query=request.query,
        results=final,
        total=len(final),
        platforms_searched=platforms_searched,
        elapsed_ms=int((time.time() - t0) * 1000),
    )
