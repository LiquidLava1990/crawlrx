"""stockx.py -- StockX product scraper.

Endpoints:
- POST /scrape/stockx/search -- Search StockX products
- POST /scrape/stockx/product -- Scrape single product detail + price history

Uses Playwright CDP 9229 (JS-heavy). Stealth delays 500-1500 ms.
"""

import asyncio
import functools
import logging
import random
import time
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from ._stockx_helpers import (
    PRODUCT_DETAIL_JS,
    SEARCH_TILES_JS,
    StockXRequestSpec,
    build_product_dict,
    extract_price,
    run_stockx_request,
    wait_for_search_tiles,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.stockx")
router = APIRouter(prefix="/scrape/stockx", tags=["stockx"])

STOCKX_BASE_URL = "https://stockx.com"


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class StockXProduct(BaseModel):
    """Single StockX product listing"""
    name: str
    ticker: Optional[str] = None
    last_sale: Optional[str] = None
    retail_price: Optional[str] = None
    premium_pct: Optional[float] = None
    asks_count: Optional[int] = None
    bids_count: Optional[int] = None
    product_url: str
    image_url: Optional[str] = None
    category: Optional[str] = None


class PriceHistoryPoint(BaseModel):
    """Single price history data point"""
    date: str
    price: Optional[str] = None
    volume: Optional[int] = None


class SizeBreakdownItem(BaseModel):
    """Size breakdown asks/bids"""
    size: str
    asks: Optional[int] = None
    bids: Optional[int] = None
    last_sale: Optional[str] = None


class StockXProductDetail(BaseModel):
    """Detailed StockX product information"""
    name: str
    description: Optional[str] = None
    colorway: Optional[str] = None
    retail_price: Optional[str] = None
    last_sale: Optional[str] = None
    week_52_high: Optional[str] = None
    week_52_low: Optional[str] = None
    all_time_high: Optional[str] = None
    all_time_low: Optional[str] = None
    total_trades: Optional[int] = None
    price_history: List[PriceHistoryPoint] = Field(default_factory=list)
    size_breakdown: List[SizeBreakdownItem] = Field(default_factory=list)


class StockXSearchRequest(BaseModel):
    """Request model for POST /scrape/stockx/search"""
    query: str = Field(..., description="Search query (product name, brand, etc.)")
    category: str = Field(default="any", description="Category: sneakers, apparel, electronics, collectibles, or any")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum products to return")


class StockXSearchResponse(BaseModel):
    """Response model for POST /scrape/stockx/search"""
    success: bool
    products: List[StockXProduct] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class StockXProductRequest(BaseModel):
    """Request model for POST /scrape/stockx/product"""
    url: Optional[str] = Field(default=None, description="Full StockX product URL")
    ticker: Optional[str] = Field(default=None, description="StockX product ticker (alternative to URL)")


class StockXProductResponse(BaseModel):
    """Response model for POST /scrape/stockx/product"""
    success: bool
    product: Optional[StockXProductDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


async def _random_delay(min_ms: int = 500, max_ms: int = 1500) -> None:
    """Sleep for random duration between min_ms and max_ms."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


def _build_search_url(query: str, category: str) -> str:
    """Build StockX search URL."""
    query_encoded = quote(query)
    category_map = {
        "sneakers": "sneakers",
        "apparel": "apparel",
        "electronics": "electronics",
        "collectibles": "collectibles",
    }
    cat_slug = category_map.get(category.lower(), "")
    if cat_slug:
        return f"{STOCKX_BASE_URL}/{cat_slug}?query={query_encoded}"
    return f"{STOCKX_BASE_URL}/search?s={query_encoded}"


# ─────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────

async def _parse_search_products(page, limit: int) -> List[StockXProduct]:
    """Parse search results from StockX page."""
    if not await wait_for_search_tiles(page):
        return []
    await _random_delay(500, 1000)
    products_data = await page.evaluate(SEARCH_TILES_JS)
    return [
        StockXProduct(**build_product_dict(data))
        for data in products_data[:limit]
    ]


def _build_price_history(raw: List[dict]) -> List[PriceHistoryPoint]:
    """Build typed price history points (max 30 days)."""
    return [
        PriceHistoryPoint(date=ph.get("date", ""), price=ph.get("price") or None)
        for ph in raw[:30]
    ]


def _build_size_breakdown(raw: List[dict]) -> List[SizeBreakdownItem]:
    """Build typed size breakdown rows."""
    return [
        SizeBreakdownItem(
            size=sb.get("size", ""),
            asks=sb.get("asks"),
            bids=sb.get("bids"),
            last_sale=sb.get("last_sale") or None,
        )
        for sb in raw
    ]


@functools.cache
def _maybe_price(value: Optional[str]) -> Optional[str]:
    """Run ``extract_price`` only when ``value`` is truthy (cached, pure regex)."""
    return extract_price(value) if value else None


def _build_product_detail(data: dict) -> StockXProductDetail:
    """Build typed StockXProductDetail from JS-emitted dict."""
    return StockXProductDetail(
        name=data.get("name", ""),
        description=data.get("description") or None,
        colorway=data.get("colorway") or None,
        retail_price=_maybe_price(data.get("retail_price")),
        last_sale=_maybe_price(data.get("last_sale")),
        week_52_high=_maybe_price(data.get("week_52_high")),
        week_52_low=_maybe_price(data.get("week_52_low")),
        all_time_high=_maybe_price(data.get("all_time_high")),
        all_time_low=_maybe_price(data.get("all_time_low")),
        total_trades=data.get("total_trades"),
        price_history=_build_price_history(data.get("price_history", [])),
        size_breakdown=_build_size_breakdown(data.get("size_breakdown", [])),
    )


async def _parse_product_detail(page) -> StockXProductDetail:
    """Parse single product detail page."""
    await page.wait_for_selector(
        "h1, [data-testid='product-name'], .product-name", timeout=20000,
    )
    await _random_delay(800, 1200)
    data = await page.evaluate(PRODUCT_DETAIL_JS)
    return _build_product_detail(data)


# ─────────────────────────────────────────────
# Scrape orchestration
# ─────────────────────────────────────────────

class _SearchScope:
    """Mutable scope for the search-scrape pipeline (lets cleanup see browser refs)."""

    __slots__ = ("browser", "ctx", "page")

    def __init__(self) -> None:
        self.browser = None
        self.ctx = None
        self.page = None


async def _open_stealth_page(
    scope: _SearchScope,
    target_url: str,
    delay_min: int,
    delay_max: int,
):
    """Open a stealth context, navigate to ``target_url``, and warm-up delay."""
    async with async_playwright() as p:
        scope.browser, scope.ctx, scope.page = await make_stealth_context(p)
        await scope.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await _random_delay(delay_min, delay_max)
        return scope.page


async def _search_stockx_op(
    scope: _SearchScope,
    query: str,
    category: str,
    limit: int,
    start_time: float,
) -> StockXSearchResponse:
    """Run the search browser pipeline and build a typed response."""
    search_url = _build_search_url(query, category)
    logger.info("Searching StockX: %s", search_url[:100])
    page = await _open_stealth_page(scope, search_url, 1000, 2000)
    products = await _parse_search_products(page, limit)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info("Found %d StockX products in %dms", len(products), elapsed_ms)
    return StockXSearchResponse(
        success=True,
        products=products,
        count=len(products),
        elapsed_ms=elapsed_ms,
    )


async def _do_search_stockx(
    query: str,
    category: str,
    limit: int,
) -> StockXSearchResponse:
    """StockX search using a stealth context."""
    start_time = time.time()
    scope = _SearchScope()
    spec = StockXRequestSpec(
        factory=StockXSearchResponse,
        log_label="StockX search",
        start_time=start_time,
        cleanup=lambda: close_stealth_context(scope.browser, scope.ctx, scope.page),
    )
    return await run_stockx_request(
        lambda: _search_stockx_op(scope, query, category, limit, start_time),
        spec,
    )


def _resolve_target_url(url: Optional[str], ticker: Optional[str]) -> Optional[str]:
    """Resolve a full URL from ``url`` or ``ticker`` -- returns None if missing."""
    if url:
        return url
    if ticker:
        return f"{STOCKX_BASE_URL}/{ticker}"
    return None


async def _scrape_product_op(
    scope: _SearchScope,
    target_url: str,
    start_time: float,
) -> StockXProductResponse:
    """Run the product-detail browser pipeline and build a typed response."""
    logger.info("Scraping StockX product: %s", target_url[:100])
    page = await _open_stealth_page(scope, target_url, 1500, 2500)
    product_detail = await _parse_product_detail(page)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "Scraped product '%s' in %dms", product_detail.name[:40], elapsed_ms,
    )
    return StockXProductResponse(
        success=True, product=product_detail, elapsed_ms=elapsed_ms,
    )


async def _do_scrape_product(
    url: Optional[str],
    ticker: Optional[str],
) -> StockXProductResponse:
    """StockX product scrape using a stealth context."""
    start_time = time.time()
    target_url = _resolve_target_url(url, ticker)
    if not target_url:
        return StockXProductResponse(
            success=False, error="Either 'url' or 'ticker' is required",
        )
    scope = _SearchScope()
    spec = StockXRequestSpec(
        factory=StockXProductResponse,
        log_label="StockX product scrape",
        start_time=start_time,
        cleanup=lambda: close_stealth_context(scope.browser, scope.ctx, scope.page),
    )
    return await run_stockx_request(
        lambda: _scrape_product_op(scope, target_url, start_time),
        spec,
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post(
    "/search",
    response_model=StockXSearchResponse,
    summary="Search StockX products",
)
async def search_stockx(request: StockXSearchRequest) -> StockXSearchResponse:
    """Search StockX products by query."""
    try:
        return await asyncio.wait_for(
            _do_search_stockx(request.query, request.category, request.limit),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("StockX search wall-clock timeout")
        return StockXSearchResponse(
            success=False,
            error="Request failed: wall-clock timeout (60s)",
            elapsed_ms=60000,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("StockX search request failed: %s", e)
        return StockXSearchResponse(
            success=False, error="Request failed: %s" % e, elapsed_ms=60000,
        )


@router.post(
    "/product",
    response_model=StockXProductResponse,
    summary="Scrape StockX product detail",
)
async def scrape_stockx_product(request: StockXProductRequest) -> StockXProductResponse:
    """Scrape detailed information from a single StockX product."""
    if not request.url and not request.ticker:
        return StockXProductResponse(
            success=False, error="Either 'url' or 'ticker' is required",
        )
    try:
        return await asyncio.wait_for(
            _do_scrape_product(request.url, request.ticker),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("StockX product scrape wall-clock timeout")
        return StockXProductResponse(
            success=False,
            error="Request failed: wall-clock timeout (60s)",
            elapsed_ms=60000,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("StockX product scrape request failed: %s", e)
        return StockXProductResponse(
            success=False, error="Request failed: %s" % e, elapsed_ms=60000,
        )
