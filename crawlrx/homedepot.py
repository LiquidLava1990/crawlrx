"""homedepot.py -- Home Depot product scraper

Endpoints:
- POST /scrape/homedepot/search -- search Home Depot products
- POST /scrape/homedepot/product -- scrape single product by URL

Uses direct HTTP first (Home Depot has decent server-side rendering).
Playwright fallback if blocked.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._homedepot_parsers import HOMEDEPOT_BASE_URL, parse_product_html, parse_search_results_html
from ._homedepot_playwright import get_product_via_playwright, search_via_playwright
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .utils import timer_ms

logger = logging.getLogger("crawlrx.homedepot")
router = APIRouter(prefix="/scrape/homedepot", tags=["homedepot"])


_DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------
# Pydantic Models
# --------------------------------------------------------------------------

class HomeDepotProduct(BaseModel):
    """Single Home Depot product data."""

    title: str
    price: Optional[str] = None
    original_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    model_number: Optional[str] = None
    brand: Optional[str] = None
    product_url: str
    image_url: Optional[str] = None
    availability: Optional[str] = None
    store_stock: Optional[str] = None


class HomeDepotProductDetail(BaseModel):
    """Detailed Home Depot product data."""

    title: str
    price: Optional[str] = None
    specs: Dict[str, str] = Field(default_factory=dict)
    description: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    rating_breakdown: Dict[str, Any] = Field(default_factory=dict)
    top_reviews: List[Dict[str, Any]] = Field(default_factory=list)
    related_items: List[Dict[str, str]] = Field(default_factory=list)
    availability: Optional[str] = None
    model_number: Optional[str] = None
    brand: Optional[str] = None
    sku: Optional[str] = None


class HomeDepotSearchRequest(BaseModel):
    """Request model for POST /scrape/homedepot/search."""

    query: str = Field(..., description="Search query")
    min_price: Optional[float] = Field(default=None, description="Minimum price filter")
    max_price: Optional[float] = Field(default=None, description="Maximum price filter")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")
    in_store: bool = Field(default=False, description="Filter for in-store availability")
    store_zip: Optional[str] = Field(
        default=None, description="ZIP code for store stock lookup"
    )


class HomeDepotSearchResponse(BaseModel):
    """Response model for POST /scrape/homedepot/search."""

    success: bool
    products: List[HomeDepotProduct] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class HomeDepotProductRequest(BaseModel):
    """Request model for POST /scrape/homedepot/product."""

    url: str = Field(..., description="Home Depot product URL")


class HomeDepotProductResponse(BaseModel):
    """Response model for POST /scrape/homedepot/product."""

    success: bool
    product: Optional[HomeDepotProductDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# --------------------------------------------------------------------------
# Small parsing helpers (kept here for backward compatibility imports)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Direct HTTP scraping
# --------------------------------------------------------------------------

def _build_search_url(query: str) -> str:
    """Build a Home Depot search URL for ``query``."""
    return f"{HOMEDEPOT_BASE_URL}/s/{urllib.parse.quote_plus(query)}"


def _search_params(request: HomeDepotSearchRequest) -> Dict[str, Any]:
    """Build the price-filter query params for a search."""
    params: Dict[str, Any] = {}
    if request.min_price is not None:
        params["min_price"] = request.min_price
    if request.max_price is not None:
        params["max_price"] = request.max_price
    return params


async def _fetch_html(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    """GET ``url`` with our default headers and return the body."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(
            url, params=params or {}, headers=_DEFAULT_HEADERS, timeout=30.0
        )
        resp.raise_for_status()
        return resp.text


def _empty_search_response(error_message: str, elapsed: int) -> HomeDepotSearchResponse:
    """Construct a failed empty search response."""
    return HomeDepotSearchResponse(
        success=False, products=[], count=0, error=error_message, elapsed_ms=elapsed,
    )


async def _search_homedepot_http(
    request: HomeDepotSearchRequest,
) -> HomeDepotSearchResponse:
    """Search Home Depot using direct HTTP requests."""
    start = timer_ms()
    try:
        html = await _fetch_html(_build_search_url(request.query), _search_params(request))
    except httpx.HTTPError as exc:
        elapsed = int(timer_ms() - start)
        logger.error("Home Depot HTTP search error for '%s': %s", request.query, exc)
        return _empty_search_response(f"HTTP error: {exc}", elapsed)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int(timer_ms() - start)
        logger.error(
            "Home Depot HTTP search unexpected error for '%s': %s",
            request.query,
            exc,
        )
        return _empty_search_response(str(exc), elapsed)

    products = parse_search_results_html(html, request.limit, HomeDepotProduct)
    elapsed = int(timer_ms() - start)
    logger.info(
        "Home Depot HTTP search returned %d products for '%s' in %dms",
        len(products),
        request.query,
        elapsed,
    )
    return HomeDepotSearchResponse(
        success=True,
        products=products,
        count=len(products),
        elapsed_ms=elapsed,
    )


async def _get_product_http(url: str) -> Optional[HomeDepotProductDetail]:
    """Get product details using direct HTTP request."""
    try:
        html = await _fetch_html(url)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Home Depot HTTP product fetch failed for %s: %s", url, exc)
        return None
    return parse_product_html(html, url, HomeDepotProductDetail)


# --------------------------------------------------------------------------
# Playwright fallback (delegates to _homedepot_playwright)
# --------------------------------------------------------------------------

async def _search_homedepot_playwright(
    request: HomeDepotSearchRequest,
) -> HomeDepotSearchResponse:
    """Search Home Depot using Playwright CDP (delegates)."""
    return await search_via_playwright(
        request, HomeDepotSearchResponse, HomeDepotProduct
    )


async def _get_product_playwright(url: str) -> Optional[HomeDepotProductDetail]:
    """Get product details using Playwright CDP (delegates)."""
    return await get_product_via_playwright(url, HomeDepotProductDetail)


# --------------------------------------------------------------------------
# API Endpoints
# --------------------------------------------------------------------------

@router.post("/search", response_model=HomeDepotSearchResponse)
async def homedepot_search(
    request: HomeDepotSearchRequest,
) -> HomeDepotSearchResponse:
    """Search Home Depot products. HTTP first, Playwright fallback."""
    result = await _search_homedepot_http(request)
    if not result.success or result.count == 0:
        logger.info(
            "Home Depot HTTP search failed/empty, trying Playwright fallback"
        )
        result = await _search_homedepot_playwright(request)
    return result


@router.post("/product", response_model=HomeDepotProductResponse)
async def homedepot_product(
    request: HomeDepotProductRequest,
) -> HomeDepotProductResponse:
    """Scrape a single Home Depot product by URL."""
    start = timer_ms()
    if not request.url:
        return HomeDepotProductResponse(
            success=False,
            error="URL is required",
            elapsed_ms=int(timer_ms() - start),
        )

    product = await _get_product_http(request.url)
    if not product:
        logger.info(
            "Home Depot HTTP product fetch failed, trying Playwright fallback"
        )
        product = await _get_product_playwright(request.url)
    elapsed = int(timer_ms() - start)

    if product:
        return HomeDepotProductResponse(
            success=True, product=product, elapsed_ms=elapsed,
        )
    return HomeDepotProductResponse(
        success=False,
        error="Failed to fetch product details",
        elapsed_ms=elapsed,
    )
