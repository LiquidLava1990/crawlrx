"""producthunt.py -- Product Hunt Scraper.

Endpoints:
  POST /scrape/producthunt/search
  POST /scrape/producthunt/today
  POST /scrape/producthunt/product

Uses Playwright CDP for JS-heavy Product Hunt pages (driver lives in
:mod:`_producthunt_pages`; extraction JS in :mod:`_producthunt_js`).
"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._ecommerce_product_helpers import BROWSER_OR_HTTP_EXCEPTIONS, parse_kformat_int
from ._producthunt_pages import (
    fetch_product_detail,
    fetch_search_results,
    fetch_today_results,
)
from .config import USER_AGENTS

logger = logging.getLogger("crawlrx.producthunt")
router = APIRouter(prefix="/scrape/producthunt", tags=["producthunt"])


# -- Pydantic Models -----------------------------------------------------------

class ProductHuntProduct(BaseModel):
    """Product Hunt product data."""

    name: str = Field(default="", description="Product name")
    tagline: str = Field(default="", description="Short tagline/description")
    upvotes: int = Field(default=0, description="Number of upvotes")
    comments_count: int = Field(default=0, description="Number of comments")
    product_url: str = Field(default="", description="Product Hunt URL")
    image_url: Optional[str] = Field(default=None, description="Product thumbnail URL")
    topics: List[str] = Field(default_factory=list, description="Product topics/categories")
    maker_name: Optional[str] = Field(default=None, description="Maker/creator name")
    launch_date: Optional[str] = Field(default=None, description="Launch date")


class ProductHuntProductDetail(ProductHuntProduct):
    """Extended product data with full details."""

    description: str = Field(default="", description="Full description")
    comments: List[Dict[str, Any]] = Field(default_factory=list, description="Top 5 comments")
    makers: List[str] = Field(default_factory=list, description="All maker names")
    gallery_images: List[str] = Field(default_factory=list, description="Gallery image URLs")
    external_url: Optional[str] = Field(default=None, description="External product website")
    pricing: Optional[str] = Field(default=None, description="Pricing information")


class ProductHuntSearchRequest(BaseModel):
    """Request model for POST /scrape/producthunt/search."""

    query: str = Field(..., description="Search query")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")


class ProductHuntTodayRequest(BaseModel):
    """Request model for POST /scrape/producthunt/today."""

    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")


class ProductHuntProductRequest(BaseModel):
    """Request model for POST /scrape/producthunt/product."""

    url: str = Field(..., description="Product Hunt product URL")


class ProductHuntSearchResponse(BaseModel):
    """Response model for POST /scrape/producthunt/search."""

    success: bool = Field(..., description="Whether scrape succeeded")
    products: List[ProductHuntProduct] = Field(default_factory=list, description="Search results")
    count: int = Field(default=0, description="Number of results")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class ProductHuntTodayResponse(BaseModel):
    """Response model for POST /scrape/producthunt/today."""

    success: bool = Field(..., description="Whether scrape succeeded")
    products: List[ProductHuntProduct] = Field(default_factory=list, description="Today's top products")
    count: int = Field(default=0, description="Number of results")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class ProductHuntProductResponse(BaseModel):
    """Response model for POST /scrape/producthunt/product."""

    success: bool = Field(..., description="Whether scrape succeeded")
    product: Optional[ProductHuntProductDetail] = Field(default=None, description="Product details")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# -- Utility functions ---------------------------------------------------------

def _normalize_int_field(value: Any) -> int:
    """Coerce a JS-extracted numeric field to ``int``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return parse_kformat_int(value)
    return 0


def _to_product(raw: Dict[str, Any]) -> ProductHuntProduct:
    """Build a ``ProductHuntProduct`` from a JS-extracted dict."""
    raw["upvotes"] = _normalize_int_field(raw.get("upvotes", 0))
    raw["comments_count"] = _normalize_int_field(raw.get("comments_count", 0))
    return ProductHuntProduct(**raw)


def _to_detail(raw: Dict[str, Any]) -> ProductHuntProductDetail:
    """Build a ``ProductHuntProductDetail`` from a JS-extracted dict."""
    raw["upvotes"] = _normalize_int_field(raw.get("upvotes", 0))
    raw["comments_count"] = _normalize_int_field(raw.get("comments_count", 0))
    return ProductHuntProductDetail(**raw)


def _elapsed_ms(start: float) -> int:
    """Milliseconds since ``start``."""
    return int((time.time() - start) * 1000)


# -- Scrape orchestrators ------------------------------------------------------

async def _do_search_producthunt(query: str, limit: int) -> ProductHuntSearchResponse:
    """Drive the search flow and wrap results in the response model."""
    start = time.time()
    try:
        raw_results = await fetch_search_results(query, limit)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[ProductHunt] Search error: %s", exc)
        elapsed = _elapsed_ms(start)
        return ProductHuntSearchResponse(success=False, error=str(exc), elapsed_ms=elapsed)
    products = [_to_product(item) for item in raw_results]
    elapsed = _elapsed_ms(start)
    return ProductHuntSearchResponse(
        success=True, products=products, count=len(products), elapsed_ms=elapsed
    )


async def _do_get_today(limit: int) -> ProductHuntTodayResponse:
    """Drive the today's-products flow."""
    start = time.time()
    try:
        raw_results = await fetch_today_results(limit)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[ProductHunt] Today's products error: %s", exc)
        return ProductHuntTodayResponse(success=False, error=str(exc), elapsed_ms=_elapsed_ms(start))
    products = [_to_product(item) for item in raw_results]
    return ProductHuntTodayResponse(
        success=True, products=products, count=len(products), elapsed_ms=_elapsed_ms(start)
    )


async def _do_scrape_product(url: str) -> ProductHuntProductResponse:
    """Drive the product-detail flow with URL validation."""
    start = time.time()
    if not url.startswith("https://www.producthunt.com/"):
        return ProductHuntProductResponse(
            success=False,
            error="Invalid Product Hunt URL. Must start with https://www.producthunt.com/",
            elapsed_ms=_elapsed_ms(start),
        )
    try:
        raw = await fetch_product_detail(url)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[ProductHunt] Product scrape error: %s", exc)
        return ProductHuntProductResponse(success=False, error=str(exc), elapsed_ms=_elapsed_ms(start))
    if not raw:
        return ProductHuntProductResponse(
            success=False,
            error="Failed to extract product data",
            elapsed_ms=_elapsed_ms(start),
        )
    return ProductHuntProductResponse(
        success=True, product=_to_detail(raw), elapsed_ms=_elapsed_ms(start)
    )


# -- API Endpoints -------------------------------------------------------------

@router.post(
    "/search", response_model=ProductHuntSearchResponse, summary="Search Product Hunt"
)
async def search_producthunt(
    request: ProductHuntSearchRequest,
) -> ProductHuntSearchResponse:
    """Search for products on Product Hunt."""
    try:
        return await asyncio.wait_for(
            _do_search_producthunt(request.query, request.limit),
            timeout=45,
        )
    except (asyncio.TimeoutError, BROWSER_OR_HTTP_EXCEPTIONS) as exc:
        logger.error("[ProductHunt] Search request failed: %s", exc)
        return ProductHuntSearchResponse(
            success=False, error=f"Request failed: {exc}", elapsed_ms=0
        )


@router.post(
    "/today",
    response_model=ProductHuntTodayResponse,
    summary="Get today's top products",
)
async def get_today_producthunt(
    request: ProductHuntTodayRequest,
) -> ProductHuntTodayResponse:
    """Get today's top products from Product Hunt."""
    try:
        return await asyncio.wait_for(_do_get_today(request.limit), timeout=45)
    except (asyncio.TimeoutError, BROWSER_OR_HTTP_EXCEPTIONS) as exc:
        logger.error("[ProductHunt] Today's products request failed: %s", exc)
        return ProductHuntTodayResponse(
            success=False, error=f"Request failed: {exc}", elapsed_ms=0
        )


@router.post(
    "/product",
    response_model=ProductHuntProductResponse,
    summary="Scrape Product Hunt product",
)
async def scrape_producthunt_product(
    request: ProductHuntProductRequest,
) -> ProductHuntProductResponse:
    """Get detailed information about a Product Hunt product."""
    try:
        return await asyncio.wait_for(_do_scrape_product(request.url), timeout=45)
    except (asyncio.TimeoutError, BROWSER_OR_HTTP_EXCEPTIONS) as exc:
        logger.error("[ProductHunt] Product request failed: %s", exc)
        return ProductHuntProductResponse(
            success=False, error=f"Request failed: {exc}", elapsed_ms=0
        )


@router.post("/", summary="Product Hunt scraper info")
def producthunt_base() -> dict:
    """Base endpoint - returns available Product Hunt scraper routes (no I/O)."""
    return {
        "success": True,
        "message": "Product Hunt scraper endpoints",
        "endpoints": [
            {
                "path": "/scrape/producthunt/search",
                "method": "POST",
                "params": {"query": "str", "limit": "int (opt)"},
            },
            {
                "path": "/scrape/producthunt/today",
                "method": "POST",
                "params": {"limit": "int (opt)"},
            },
            {
                "path": "/scrape/producthunt/product",
                "method": "POST",
                "params": {"url": "str"},
            },
        ],
    }
