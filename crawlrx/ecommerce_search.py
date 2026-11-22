"""ecommerce_search.py -- POST /search/ecommerce

Unified ecommerce product search. Priority order:
  1. SearXNG direct HTTP call (fast, reliable, no rate limits)
  2. Google Shopping via Playwright CDP (real prices, real products)
  3. DDGS site-targeted shopping search (Amazon/Walmart/Target/BestBuy)
  4. Store platform scraper flow (Shopify / WooCommerce / generic via SERP)

This module is now a thin orchestrator: it owns the FastAPI route and
the public response model, and delegates the priority ladder to
:mod:`_ecommerce_search_runner`. Per-source adapters live in
:mod:`_ecommerce_search_sources`; pure helpers live in
:mod:`_ecommerce_search_filters`.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from ._ecommerce_search_models import (
    EcommerceSearchRequest,
    EcommerceSearchResponse,
    ProductResult,
)
from ._ecommerce_search_runner import _search_ecommerce_impl
from ._ecommerce_search_sources import (
    _google_shopping_to_product,
    _search_google_shopping_playwright,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)

logger = logging.getLogger('crawlrx.ecommerce_search')
router = APIRouter(prefix='/search', tags=['ecommerce'])

_ECOMMERCE_PLATFORMS = {
    'shopify', 'woocommerce', 'bigcommerce', 'magento', 'generic',
}
_ECOMMERCE_HARD_TIMEOUT_S = 45.0


__all__ = [
    "BROWSER_SCRAPE_EXCEPTIONS",
    "EcommerceSearchRequest",
    "EcommerceSearchResponse",
    "PARSE_EXCEPTIONS",
    "ProductResult",
    "SCRAPE_FAILURE_EXCEPTIONS",
    "router",
    "search_ecommerce",
    # Backward-compat re-exports for callers that reach into helpers.
    "_google_shopping_to_product",
    "_search_google_shopping_playwright",
]


@router.post('/ecommerce', response_model=EcommerceSearchResponse)
async def search_ecommerce(
    request: EcommerceSearchRequest,
) -> EcommerceSearchResponse:
    """Unified ecommerce product search with a bounded hard timeout."""
    try:
        return await asyncio.wait_for(
            _search_ecommerce_impl(request), timeout=_ECOMMERCE_HARD_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            'search_ecommerce hard timeout (%ss) for query: %s',
            int(_ECOMMERCE_HARD_TIMEOUT_S),
            request.query,
        )
        return EcommerceSearchResponse(
            success=False,
            query=request.query,
            results=[],
            total=0,
            platforms_searched=[],
            elapsed_ms=int(_ECOMMERCE_HARD_TIMEOUT_S * 1000),
            error='Request timed out after %d seconds' % int(_ECOMMERCE_HARD_TIMEOUT_S),
        )
    except Exception as exc:
        logger.error(
            'search_ecommerce failed for query %s: %s',
            request.query,
            type(exc).__name__,
            exc_info=True,
        )
        return EcommerceSearchResponse(
            success=False,
            query=request.query,
            results=[],
            total=0,
            platforms_searched=[],
            elapsed_ms=0,
            error='Internal error: %s' % type(exc).__name__,
        )
