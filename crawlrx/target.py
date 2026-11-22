"""target.py -- Target product scraper public surface (HTTP + Playwright)."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter

from ._scraper_helpers import price_in_range as _hp_price_in_range
from ._target_http import (
    TARGET_BASE_URL,
    build_product_url as _build_product_url,
    extract_tcin_from_url as _extract_tcin_from_url,
    get_product_http as _get_product_http,
    parse_product_html as _parse_product_html,
    parse_search_results_html as _parse_search_results_html,
    search_target_http as _search_target_http_impl,
)
from ._target_models import (
    TargetFulfillmentOptions,
    TargetProduct,
    TargetProductDetail,
    TargetProductRequest,
    TargetProductResponse,
    TargetReview,
    TargetSearchRequest,
    TargetSearchResponse,
)
from ._target_playwright import (
    get_product_playwright as _get_product_playwright,
    search_target_playwright as _search_target_playwright_impl,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.target")
router = APIRouter(prefix="/scrape/target", tags=["target"])


def _extract_price_value(price_text: Optional[str]) -> Optional[float]:
    """Backward-compat wrapper for the shared numeric-price parser."""
    from ._scraper_helpers import parse_price_value
    return parse_price_value(price_text)


def _sort_products(products: List[TargetProduct], sort: str) -> None:
    """Sort the list in place per the supplied ``sort`` token."""
    if sort == "price_asc":
        products.sort(key=lambda p: _extract_price_value(p.price) or float("inf"))
    elif sort == "price_desc":
        products.sort(key=lambda p: _extract_price_value(p.price) or 0.0, reverse=True)
    elif sort == "rating":
        products.sort(key=lambda p: p.rating or 0.0, reverse=True)


def _filter_and_limit_products(
    products: List[TargetProduct], request: TargetSearchRequest
) -> List[TargetProduct]:
    """Filter products by price range, sort, and apply ``limit``."""
    filtered = [
        p for p in products
        if _hp_price_in_range(p.price, request.min_price, request.max_price)
    ]
    _sort_products(filtered, request.sort)
    return filtered[: request.limit]


async def _search_target_http(request: TargetSearchRequest) -> TargetSearchResponse:
    """Search Target via direct HTTP."""
    return await _search_target_http_impl(request, _filter_and_limit_products)


async def _search_target_playwright(
    request: TargetSearchRequest,
) -> TargetSearchResponse:
    """Search Target via Playwright CDP."""
    return await _search_target_playwright_impl(request, _filter_and_limit_products)


def _resolve_product_tcin(request: TargetProductRequest) -> Optional[str]:
    """Resolve the TCIN from a product request, preferring the explicit field."""
    if request.tcin:
        return request.tcin
    if request.url:
        return _extract_tcin_from_url(request.url)
    return None


@router.post("/search", response_model=TargetSearchResponse)
async def target_search(request: TargetSearchRequest) -> TargetSearchResponse:
    """Search Target products: HTTP first, Playwright fallback if blocked."""
    result = await _search_target_http(request)
    if not result.success or result.count == 0:
        logger.info("Target HTTP search failed/empty, trying Playwright fallback")
        result = await _search_target_playwright(request)
    return result


@router.post("/product", response_model=TargetProductResponse)
async def target_product(request: TargetProductRequest) -> TargetProductResponse:
    """Scrape a single Target product by URL or TCIN."""
    start = timer_ms()
    tcin = _resolve_product_tcin(request)
    if not tcin:
        return TargetProductResponse(
            success=False,
            error="Could not extract TCIN from URL. Provide tcin directly.",
            elapsed_ms=int(timer_ms() - start),
        )
    product = await _get_product_http(tcin)
    if not product:
        logger.info("Target HTTP product fetch failed, trying Playwright fallback")
        product = await _get_product_playwright(tcin)
    elapsed = int(timer_ms() - start)
    if product:
        return TargetProductResponse(success=True, product=product, elapsed_ms=elapsed)
    return TargetProductResponse(
        success=False,
        error="Failed to fetch product details",
        elapsed_ms=elapsed,
    )


__all__ = (
    "TARGET_BASE_URL",
    "TargetFulfillmentOptions",
    "TargetProduct",
    "TargetProductDetail",
    "TargetProductRequest",
    "TargetProductResponse",
    "TargetReview",
    "TargetSearchRequest",
    "TargetSearchResponse",
    "router",
    "target_product",
    "target_search",
)
