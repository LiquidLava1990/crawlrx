"""costco.py -- Costco product scraper.

Endpoints:
  POST /scrape/costco/search  -- search Costco products
  POST /scrape/costco/product -- scrape single product by URL

Parser + Playwright bodies live in sibling modules
(``_costco_parsers.py`` / ``_costco_playwright.py``); this module is a
thin FastAPI router + HTTP-fallback orchestrator. Module split.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._costco_parsers import (
    extract_item_number_from_url,
    parse_product_html,
    parse_search_results_html,
)
from ._costco_playwright import (
    get_product_playwright,
    search_costco_playwright,
)
from ._scraper_helpers import PARSE_EXCEPTIONS
from .config import USER_AGENTS
from .utils import timer_ms

logger = logging.getLogger("crawlrx.costco")
router = APIRouter(prefix="/scrape/costco", tags=["costco"])

COSTCO_BASE_URL = "https://www.costco.com"


# Pydantic models -----------------------------------------------------------

class CostcoProduct(BaseModel):
    """Single Costco product data"""
    title: str = Field(default="", description="Product title")
    price: Optional[str] = Field(default=None, description="Current price")
    item_number: str = Field(default="", description="Costco item number")
    rating: Optional[float] = Field(default=None, description="Star rating (1-5)")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    product_url: str = Field(default="", description="Product page URL")
    image_url: Optional[str] = Field(default=None, description="Primary product image URL")
    availability: Optional[str] = Field(default=None, description="Stock availability")
    unit_price: Optional[str] = Field(default=None, description="Price per unit (e.g., '$0.50/oz')")
    warehouse_only: bool = Field(default=False, description="Whether item is warehouse only")


class CostcoProductDetail(BaseModel):
    """Detailed Costco product data"""
    title: str = Field(default="", description="Product title")
    price: Optional[str] = Field(default=None, description="Current price")
    description: Optional[str] = Field(default=None, description="Product description")
    specs: Dict[str, str] = Field(default_factory=dict, description="Product specifications")
    images: List[str] = Field(default_factory=list, description="Product image URLs")
    rating: Optional[float] = Field(default=None, description="Star rating")
    rating_breakdown: Dict[str, int] = Field(default_factory=dict, description="Rating distribution by star")
    top_reviews: List[Dict[str, Any]] = Field(default_factory=list, description="Top 5 reviews")
    item_number: str = Field(default="", description="Costco item number")
    warehouse_availability: List[Dict[str, Any]] = Field(default_factory=list, description="Warehouse stock status")
    online_availability: Optional[str] = Field(default=None, description="Online delivery/pickup availability")
    brand: Optional[str] = Field(default=None, description="Product brand")
    category: Optional[str] = Field(default=None, description="Product category")
    unit_price: Optional[str] = Field(default=None, description="Price per unit")
    features: List[str] = Field(default_factory=list, description="Product features/bullets")


class CostcoSearchRequest(BaseModel):
    """Request model for POST /scrape/costco/search"""
    query: str = Field(..., description="Search query")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")
    category: Optional[str] = Field(default=None, description="Category filter (optional)")


class CostcoSearchResponse(BaseModel):
    """Response model for POST /scrape/costco/search"""
    success: bool = Field(..., description="Whether scrape succeeded")
    products: List[CostcoProduct] = Field(default_factory=list, description="List of products")
    count: int = Field(default=0, description="Number of products returned")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class CostcoProductRequest(BaseModel):
    """Request model for POST /scrape/costco/product"""
    url: str = Field(..., description="Costco product URL")


class CostcoProductResponse(BaseModel):
    """Response model for POST /scrape/costco/product"""
    success: bool = Field(..., description="Whether scrape succeeded")
    product: Optional[CostcoProductDetail] = None
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# Direct HTTP scraping -------------------------------------------------------

def _build_search_headers() -> Dict[str, str]:
    """Stealth headers for the Costco HTTP search request."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


async def _fetch_search_html(request: CostcoSearchRequest) -> str:
    """Fetch raw search-results HTML via httpx."""
    search_url = f"{COSTCO_BASE_URL}/CatalogSearch"
    params = {"keyword": request.query, "dept": "all"}
    headers = _build_search_headers()
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(search_url, params=params, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.text


def _http_search_failure(query: str, elapsed: int, error_kind: str, exc: BaseException) -> CostcoSearchResponse:
    """Build a failure response and emit the standard error log line."""
    logger.error("Costco HTTP search %s for '%s': %s", error_kind, query, exc)
    error_msg = "HTTP error: %s" % exc if error_kind == "error" else str(exc)
    return CostcoSearchResponse(
        success=False, products=[], count=0,
        error=error_msg, elapsed_ms=elapsed,
    )


async def _search_costco_http(request: CostcoSearchRequest) -> CostcoSearchResponse:
    """Search Costco using direct HTTP requests."""
    start = timer_ms()
    try:
        html = await _fetch_search_html(request)
    except httpx.HTTPError as exc:
        return _http_search_failure(request.query, int(timer_ms() - start), "error", exc)
    except PARSE_EXCEPTIONS as exc:
        return _http_search_failure(request.query, int(timer_ms() - start), "unexpected error", exc)

    products = parse_search_results_html(html, request.limit, CostcoProduct)
    elapsed = int(timer_ms() - start)
    logger.info("Costco HTTP search returned %d products for '%s' in %dms",
                len(products), request.query, elapsed)
    return CostcoSearchResponse(
        success=True, products=products, count=len(products), elapsed_ms=elapsed,
    )


async def _get_product_http(url: str) -> Optional[CostcoProductDetail]:
    """Get product details using direct HTTP request."""
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
        logger.warning("Costco HTTP product fetch failed for %s: %s", url, exc)
        return None
    return parse_product_html(html, url, CostcoProductDetail)


# Playwright thin wrappers ---------------------------------------------------

async def _search_costco_playwright(request: CostcoSearchRequest) -> CostcoSearchResponse:
    """Delegate to the sibling Playwright search implementation."""
    return await search_costco_playwright(request, CostcoProduct, CostcoSearchResponse)


async def _get_product_playwright(url: str) -> Optional[CostcoProductDetail]:
    """Delegate to the sibling Playwright product implementation."""
    return await get_product_playwright(url, CostcoProductDetail)


# API endpoints --------------------------------------------------------------

def _invalid_costco_url(url: str) -> bool:
    return not url.startswith("https://www.costco.com/")


@router.post("/search", response_model=CostcoSearchResponse)
async def costco_search(request: CostcoSearchRequest) -> CostcoSearchResponse:
    """Search Costco products: HTTP-first with Playwright fallback."""
    result = await _search_costco_http(request)
    if result.success and result.count > 0:
        return result
    logger.info("Costco HTTP search failed/empty, trying Playwright fallback")
    return await _search_costco_playwright(request)


@router.post("/product", response_model=CostcoProductResponse)
async def costco_product(request: CostcoProductRequest) -> CostcoProductResponse:
    """Scrape a single Costco product by URL: HTTP-first with Playwright fallback."""
    start = timer_ms()
    if _invalid_costco_url(request.url):
        return CostcoProductResponse(
            success=False,
            error="Invalid Costco URL. Must start with https://www.costco.com/",
            elapsed_ms=int(timer_ms() - start),
        )
    product = await _get_product_http(request.url)
    if product is None:
        logger.info("Costco HTTP product fetch failed, trying Playwright fallback")
        product = await _get_product_playwright(request.url)
    elapsed = int(timer_ms() - start)
    if product is None:
        return CostcoProductResponse(
            success=False, error="Failed to fetch product details", elapsed_ms=elapsed,
        )
    return CostcoProductResponse(success=True, product=product, elapsed_ms=elapsed)


# Snapshot-stable aliases ----------------------------------------------------

# Aliases that keep the cross-file signature snapshot stable while the
# real implementations live in the sibling parser/playwright modules.
_extract_item_number_from_url = extract_item_number_from_url
_parse_search_results_html = parse_search_results_html
_parse_product_html = parse_product_html


async def scrape_costco_products(query: str, limit: int = 20) -> CostcoSearchResponse:
    """Module-level convenience wrapper used by the package public API."""
    return await costco_search(CostcoSearchRequest(query=query, limit=limit))
