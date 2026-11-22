"""bestbuy.py -- Best Buy product scraper.

Endpoints:
  POST /scrape/bestbuy/search  -- search Best Buy products
  POST /scrape/bestbuy/product -- scrape single product by URL or SKU

Parser + Playwright bodies live in sibling modules
(``_bestbuy_parsers.py`` / ``_bestbuy_playwright.py``); this module is a
thin FastAPI router + HTTP-fallback orchestrator. Module split.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._bestbuy_parsers import (
    build_product_url,
    extract_sku_from_url,
    parse_product_html,
    parse_search_results_html,
)
from ._bestbuy_playwright import (
    get_product_playwright,
    search_bestbuy_playwright,
)
from ._scraper_helpers import PARSE_EXCEPTIONS
from .config import USER_AGENTS
from .utils import timer_ms

logger = logging.getLogger("crawlrx.bestbuy")
router = APIRouter(prefix="/scrape/bestbuy", tags=["bestbuy"])

BESTBUY_BASE_URL = "https://www.bestbuy.com"


# Pydantic models -----------------------------------------------------------

class BestBuyProduct(BaseModel):
    """Single Best Buy product data"""
    title: str = Field(default="", description="Product title")
    price: Optional[str] = Field(default=None, description="Current price")
    original_price: Optional[str] = Field(default=None, description="Original/strikethrough price")
    rating: Optional[float] = Field(default=None, description="Star rating (1-5)")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    sku: str = Field(default="", description="Best Buy SKU")
    product_url: str = Field(default="", description="Product page URL")
    image_url: Optional[str] = Field(default=None, description="Primary product image URL")
    availability: Optional[str] = Field(default=None, description="Stock availability")
    condition: str = Field(default="new", description="Product condition: new, refurbished, open-box")
    open_box_available: bool = Field(default=False, description="Whether open-box option is available")


class BestBuyProductDetail(BaseModel):
    """Detailed Best Buy product data"""
    title: str = Field(default="", description="Product title")
    price: Optional[str] = Field(default=None, description="Current price")
    original_price: Optional[str] = Field(default=None, description="Original price")
    description: Optional[str] = Field(default=None, description="Product description")
    specs: Dict[str, str] = Field(default_factory=dict, description="Product specifications")
    images: List[str] = Field(default_factory=list, description="Product image URLs")
    rating: Optional[float] = Field(default=None, description="Star rating")
    rating_breakdown: Dict[str, int] = Field(default_factory=dict, description="Rating distribution")
    top_reviews: List[Dict[str, Any]] = Field(default_factory=list, description="Top 5 reviews")
    availability: Optional[str] = Field(default=None, description="Stock status")
    condition: str = Field(default="new", description="Product condition")
    open_box_options: List[Dict[str, Any]] = Field(default_factory=list, description="Open-box pricing options")
    store_availability: List[Dict[str, Any]] = Field(default_factory=list, description="Store pickup availability")
    sku: str = Field(default="", description="Best Buy SKU")
    brand: Optional[str] = Field(default=None, description="Product brand")
    category: Optional[str] = Field(default=None, description="Product category")
    model_number: Optional[str] = Field(default=None, description="Manufacturer model number")


class BestBuySearchRequest(BaseModel):
    """Request model for POST /scrape/bestbuy/search"""
    query: str = Field(..., description="Search query")
    min_price: Optional[float] = Field(default=None, description="Minimum price filter")
    max_price: Optional[float] = Field(default=None, description="Maximum price filter")
    condition: str = Field(default="any", description="Condition filter: 'new', 'refurbished', 'any'")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")


class BestBuySearchResponse(BaseModel):
    """Response model for POST /scrape/bestbuy/search"""
    success: bool = Field(..., description="Whether scrape succeeded")
    products: List[BestBuyProduct] = Field(default_factory=list, description="List of products")
    count: int = Field(default=0, description="Number of products returned")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class BestBuyProductRequest(BaseModel):
    """Request model for POST /scrape/bestbuy/product"""
    url: Optional[str] = Field(default=None, description="Best Buy product URL")
    sku: Optional[str] = Field(default=None, description="Best Buy SKU")


class BestBuyProductResponse(BaseModel):
    """Response model for POST /scrape/bestbuy/product"""
    success: bool = Field(..., description="Whether scrape succeeded")
    product: Optional[BestBuyProductDetail] = None
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# Direct HTTP scraping -------------------------------------------------------

def _build_search_headers() -> Dict[str, str]:
    """Stealth headers for the Best Buy HTTP search request."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


def _build_search_params(request: BestBuySearchRequest) -> Dict[str, Any]:
    """Encode search filters as Best Buy query params."""
    params: Dict[str, Any] = {"st": request.query, "intl": "nosearch"}
    if request.min_price is not None:
        params["lp"] = request.min_price
    if request.max_price is not None:
        params["hp"] = request.max_price
    if request.condition == "refurbished":
        params["cp"] = "2"
    return params


async def _fetch_search_html(request: BestBuySearchRequest) -> str:
    """Fetch raw search-results HTML via httpx."""
    search_url = f"{BESTBUY_BASE_URL}/site/searchpage.jsp"
    params = _build_search_params(request)
    headers = _build_search_headers()
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(search_url, params=params, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.text


def _http_search_failure(query: str, elapsed: int, error_kind: str, exc: BaseException) -> BestBuySearchResponse:
    """Build a failure response and emit the standard error log line."""
    logger.error("Best Buy HTTP search %s for '%s': %s", error_kind, query, exc)
    error_msg = "HTTP error: %s" % exc if error_kind == "error" else str(exc)
    return BestBuySearchResponse(
        success=False, products=[], count=0,
        error=error_msg, elapsed_ms=elapsed,
    )


async def _search_bestbuy_http(request: BestBuySearchRequest) -> BestBuySearchResponse:
    """Search Best Buy using direct HTTP requests."""
    start = timer_ms()
    try:
        html = await _fetch_search_html(request)
    except httpx.HTTPError as exc:
        return _http_search_failure(request.query, int(timer_ms() - start), "error", exc)
    except PARSE_EXCEPTIONS as exc:
        return _http_search_failure(request.query, int(timer_ms() - start), "unexpected error", exc)

    products = parse_search_results_html(html, request.limit, request.condition, BestBuyProduct)
    elapsed = int(timer_ms() - start)
    logger.info("Best Buy HTTP search returned %d products for '%s' in %dms",
                len(products), request.query, elapsed)
    return BestBuySearchResponse(
        success=True, products=products, count=len(products), elapsed_ms=elapsed,
    )


async def _get_product_http(sku: str) -> Optional[BestBuyProductDetail]:
    """Get product details using direct HTTP request."""
    url = build_product_url(sku)
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as exc:
        logger.warning("Best Buy HTTP product fetch failed for %s: %s", sku, exc)
        return None
    return parse_product_html(html, sku, BestBuyProductDetail)


# Playwright thin wrappers ---------------------------------------------------

async def _search_bestbuy_playwright(request: BestBuySearchRequest) -> BestBuySearchResponse:
    """Delegate to the sibling Playwright search implementation."""
    return await search_bestbuy_playwright(request, BestBuyProduct, BestBuySearchResponse)


async def _get_product_playwright(sku: str) -> Optional[BestBuyProductDetail]:
    """Delegate to the sibling Playwright product implementation."""
    return await get_product_playwright(sku, BestBuyProductDetail)


# API endpoints --------------------------------------------------------------

def _resolve_request_sku(request: BestBuyProductRequest) -> Optional[str]:
    """Pull the SKU from a product request body (direct or via URL)."""
    if request.sku:
        return request.sku
    if request.url:
        return extract_sku_from_url(request.url)
    return None


@router.post("/search", response_model=BestBuySearchResponse)
async def bestbuy_search(request: BestBuySearchRequest) -> BestBuySearchResponse:
    """Search Best Buy products: HTTP-first with Playwright fallback."""
    result = await _search_bestbuy_http(request)
    if result.success and result.count > 0:
        return result
    logger.info("Best Buy HTTP search failed/empty, trying Playwright fallback")
    return await _search_bestbuy_playwright(request)


@router.post("/product", response_model=BestBuyProductResponse)
async def bestbuy_product(request: BestBuyProductRequest) -> BestBuyProductResponse:
    """Scrape a single Best Buy product by URL or SKU."""
    start = timer_ms()
    sku = _resolve_request_sku(request)
    if not sku:
        return BestBuyProductResponse(
            success=False,
            error="Could not extract SKU from URL. Provide SKU directly.",
            elapsed_ms=int(timer_ms() - start),
        )
    product = await _get_product_http(sku)
    if product is None:
        logger.info("Best Buy HTTP product fetch failed, trying Playwright fallback")
        product = await _get_product_playwright(sku)
    elapsed = int(timer_ms() - start)
    if product is None:
        return BestBuyProductResponse(
            success=False, error="Failed to fetch product details", elapsed_ms=elapsed,
        )
    return BestBuyProductResponse(success=True, product=product, elapsed_ms=elapsed)


# Snapshot-stable aliases ----------------------------------------------------

# Aliases that keep the cross-file signature snapshot stable while the
# real implementations live in the sibling parser/playwright modules.
_extract_sku_from_url = extract_sku_from_url
_build_product_url = build_product_url
_parse_search_results_html = parse_search_results_html
_parse_product_html = parse_product_html


async def scrape_bestbuy_products(query: str, limit: int = 20) -> BestBuySearchResponse:
    """Module-level convenience wrapper used by the package public API."""
    return await bestbuy_search(BestBuySearchRequest(query=query, limit=limit))
