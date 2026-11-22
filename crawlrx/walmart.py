"""walmart.py -- Walmart product scraper.

Endpoints:
  POST /scrape/walmart/search -- search Walmart products
  POST /scrape/walmart/product -- scrape single product by URL or item ID

Uses direct HTTP first (Walmart has decent server-side rendering),
Playwright fallback if blocked.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    parse_rating_value,
)
from ._walmart_parsers import (
    absolute_walmart_url,
    build_product_url,
    extract_item_id_from_url,
    parse_product_html,
    parse_search_results_html,
    probe_rating,
    probe_text,
    safe_attr,
    safe_inner_text,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.walmart")
router = APIRouter(prefix="/scrape/walmart", tags=["walmart"])

WALMART_BASE_URL = "https://www.walmart.com"

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SORT_MAP = {
    "best_match": "relevance",
    "price_asc": "price_low",
    "price_desc": "price_high",
    "rating": "rating",
}


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class WalmartProduct(BaseModel):
    """Single Walmart product in a search result."""
    title: str
    price: Optional[str] = None
    original_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    availability: Optional[str] = None
    product_url: str
    image_url: Optional[str] = None
    item_id: str
    seller: Optional[str] = None


class WalmartProductDetail(BaseModel):
    """Detailed Walmart product page data."""
    title: str
    price: Optional[str] = None
    original_price: Optional[str] = None
    description: Optional[str] = None
    specs: Dict[str, str] = Field(default_factory=dict)
    images: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    rating_breakdown: Dict[str, int] = Field(default_factory=dict)
    top_reviews: List[Dict[str, Any]] = Field(default_factory=list)
    availability: Optional[str] = None
    fulfillment_options: List[str] = Field(default_factory=list)
    item_id: str
    seller: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None


class WalmartSearchRequest(BaseModel):
    """Request model for POST /scrape/walmart/search."""
    query: str = Field(...)
    min_price: Optional[float] = Field(default=None)
    max_price: Optional[float] = Field(default=None)
    sort: str = Field(default="best_match")
    limit: int = Field(default=20, ge=1, le=50)


class WalmartSearchResponse(BaseModel):
    """Response model for POST /scrape/walmart/search."""
    success: bool
    products: List[WalmartProduct] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class WalmartProductRequest(BaseModel):
    """Request model for POST /scrape/walmart/product."""
    url: Optional[str] = Field(default=None)
    item_id: Optional[str] = Field(default=None)


class WalmartProductResponse(BaseModel):
    """Response model for POST /scrape/walmart/product."""
    success: bool
    product: Optional[WalmartProductDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Search-request URL building
# ─────────────────────────────────────────────

def _build_search_params(request: WalmartSearchRequest) -> Dict[str, Any]:
    """Build the query-string params dict for the HTTP search endpoint."""
    params: Dict[str, Any] = {"q": request.query, "page": 1}
    if request.min_price is not None:
        params["min_price"] = request.min_price
    if request.max_price is not None:
        params["max_price"] = request.max_price
    if request.sort in _SORT_MAP:
        params["sort"] = _SORT_MAP[request.sort]
    return params


def _search_failure_response(
    request: WalmartSearchRequest,
    error_message: str,
    start: float,
) -> WalmartSearchResponse:
    """Build a failure-shaped ``WalmartSearchResponse``."""
    elapsed = int(timer_ms() - start)
    logger.error("Walmart search '%s' failed: %s", request.query, error_message)
    return WalmartSearchResponse(
        success=False, products=[], count=0, error=error_message, elapsed_ms=elapsed,
    )


# ─────────────────────────────────────────────
# Direct HTTP path
# ─────────────────────────────────────────────

async def _http_get_text(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Fetch a URL and return its body; raises ``httpx.HTTPError`` on failure."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url, params=params, headers=_HTTP_HEADERS, timeout=30.0)
        resp.raise_for_status()
        return resp.text


async def _search_walmart_http(
    request: WalmartSearchRequest,
) -> WalmartSearchResponse:
    """Search Walmart via direct HTTP + ``parse_search_results_html``."""
    start = timer_ms()
    try:
        html = await _http_get_text(
            f"{WALMART_BASE_URL}/search", _build_search_params(request),
        )
    except httpx.HTTPError as exc:
        return _search_failure_response(request, f"HTTP error: {exc}", start)
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        return _search_failure_response(request, str(exc), start)
    products = parse_search_results_html(WalmartProduct, html, request.limit)
    elapsed = int(timer_ms() - start)
    logger.info(
        "Walmart HTTP search returned %d products for '%s' in %dms",
        len(products), request.query, elapsed,
    )
    return WalmartSearchResponse(
        success=True, products=products, count=len(products), elapsed_ms=elapsed,
    )


async def _get_product_http(item_id: str) -> Optional[WalmartProductDetail]:
    """Fetch + parse a Walmart product page via direct HTTP."""
    url = build_product_url(item_id)
    try:
        html = await _http_get_text(url)
    except httpx.HTTPError as exc:
        logger.warning("Walmart HTTP product fetch failed for %s: %s", item_id, exc)
        return None
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.warning(
            "Walmart HTTP product parse failed for %s (%s): %s",
            item_id, type(exc).__name__, exc,
        )
        return None
    return parse_product_html(WalmartProductDetail, html, item_id)


# ─────────────────────────────────────────────
# Playwright fallback path
# ─────────────────────────────────────────────

async def _wait_for_search_grid(page: Any) -> None:
    """Best-effort wait for search-result tiles; only Playwright timeouts swallowed."""
    try:
        await page.wait_for_selector(
            "[data-automation-id='product-title'], .search-result", timeout=10000,
        )
    except PlaywrightTimeoutError:
        return


async def _scroll_grid(page: Any, passes: int = 3, dy: int = 800) -> None:
    """Trigger lazy-loaded tiles by scrolling ``passes`` times."""
    for _ in range(passes):
        await page.evaluate(f"window.scrollBy(0, {dy})")
        await asyncio.sleep(0.5)


async def _extract_search_card(card: Any) -> Optional[WalmartProduct]:
    """Build one ``WalmartProduct`` from a Playwright card handle."""
    item_id = await safe_attr(card, "data-item-id")
    if not item_id:
        return None
    title_el = await card.query_selector(
        "[data-automation-id='product-title']",
    ) or card
    title = (await safe_inner_text(title_el)) or ""
    price_el = await card.query_selector("[data-automation-id='product-price']")
    price = await safe_inner_text(price_el)
    rating_el = await card.query_selector("[data-automation-id='product-rating']")
    rating_text = await safe_inner_text(rating_el)
    rating = parse_rating_value(rating_text)
    img_el = await card.query_selector("img")
    image_url = await safe_attr(img_el, "src")
    link_el = await card.query_selector("a[href^='/ip/']")
    href = await safe_attr(link_el, "href")
    product_url = absolute_walmart_url(href) if href else build_product_url(item_id)
    return WalmartProduct(
        title=title,
        price=price,
        rating=rating,
        product_url=product_url,
        image_url=image_url,
        item_id=item_id,
    )


async def _collect_search_cards(
    page: Any, limit: int,
) -> List[WalmartProduct]:
    """Iterate Playwright product cards into a list of ``WalmartProduct``."""
    cards = await page.query_selector_all(
        "[data-item-id], [data-automation-id='product-title']",
    )
    out: List[WalmartProduct] = []
    for card in cards[:limit]:
        try:
            product = await _extract_search_card(card)
        except SCRAPE_FAILURE_EXCEPTIONS as exc:
            logger.debug(
                "Walmart card extract failed (%s): %s",
                type(exc).__name__, exc,
            )
            continue
        if product is not None:
            out.append(product)
    return out


async def _search_walmart_playwright(
    request: WalmartSearchRequest,
) -> WalmartSearchResponse:
    """Search Walmart via Playwright CDP; called when HTTP path is empty/blocked."""
    start = timer_ms()
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = (
                f"{WALMART_BASE_URL}/search?"
                f"q={urllib.parse.quote_plus(request.query)}"
            )
            logger.info("[Walmart] Navigating to search: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_search_grid(page)
            await _scroll_grid(page)
            products = await _collect_search_cards(page, request.limit)
            elapsed = int(timer_ms() - start)
            logger.info(
                "Walmart Playwright search returned %d products in %dms",
                len(products), elapsed,
            )
            return WalmartSearchResponse(
                success=True, products=products, count=len(products), elapsed_ms=elapsed,
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        return _search_failure_response(request, str(exc), start)
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Product-detail Playwright path
# ─────────────────────────────────────────────

_DETAIL_TEXT_PROBES = {
    "title": "h1[data-automation-id='product-title']",
    "price": "[data-automation-id='product-price']",
    "description": "[data-automation-id='product-description']",
    "availability": "[data-automation-id='availability-status']",
}


async def _populate_product_text_fields(
    product: WalmartProductDetail, page: Any,
) -> None:
    """Fill ``title``/``price``/``description``/``availability`` from selectors."""
    for field, selector in _DETAIL_TEXT_PROBES.items():
        text = await probe_text(page, selector)
        if text is not None:
            setattr(product, field, text)


async def _populate_product_rating(
    product: WalmartProductDetail, page: Any,
) -> None:
    """Fill ``product.rating`` from the reviews-avg-rating widget."""
    rating = await probe_rating(page, "[data-automation-id='reviews-avg-rating']")
    if rating is not None:
        product.rating = rating


async def _populate_product_images(
    product: WalmartProductDetail, page: Any,
) -> None:
    """Fill ``product.images`` from gallery ``<img>`` ``src`` attrs."""
    try:
        img_els = await page.query_selector_all(
            "[data-automation-id='product-image'] img",
        )
    except (AttributeError, TypeError) as exc:
        logger.debug("walmart image selector failed (%s): %s",
                     type(exc).__name__, exc)
        return
    images: List[str] = []
    for img in img_els:
        src = await safe_attr(img, "src")
        if src:
            images.append(src)
    product.images = images


async def _populate_product_seller(
    product: WalmartProductDetail, page: Any,
) -> None:
    """Fill ``product.seller``, defaulting to ``Walmart`` if not found."""
    text = await probe_text(page, "[data-automation-id='sold-by-section']")
    product.seller = text if text else "Walmart"


async def _get_product_playwright(item_id: str) -> Optional[WalmartProductDetail]:
    """Scrape a Walmart product detail via Playwright CDP."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            url = build_product_url(item_id)
            logger.info("[Walmart] Navigating to product: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)
            product = WalmartProductDetail(title="", item_id=item_id)
            await _populate_product_text_fields(product, page)
            await _populate_product_rating(product, page)
            await _populate_product_images(product, page)
            await _populate_product_seller(product, page)
            return product if product.title else None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "Walmart Playwright product fetch error (%s): %s",
            type(exc).__name__, exc,
        )
        return None
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/search", response_model=WalmartSearchResponse)
async def walmart_search(request: WalmartSearchRequest) -> WalmartSearchResponse:
    """Search Walmart products: HTTP-first, Playwright fallback."""
    result = await _search_walmart_http(request)
    if not result.success or result.count == 0:
        logger.info("Walmart HTTP search failed/empty, trying Playwright fallback")
        result = await _search_walmart_playwright(request)
    return result


def _resolve_item_id(request: WalmartProductRequest) -> Optional[str]:
    """Return the item-id from ``request.item_id`` or via URL extraction."""
    if request.item_id:
        return request.item_id
    if request.url:
        return extract_item_id_from_url(request.url)
    return None


@router.post("/product", response_model=WalmartProductResponse)
async def walmart_product(
    request: WalmartProductRequest,
) -> WalmartProductResponse:
    """Scrape a single Walmart product by URL or item ID."""
    start = timer_ms()
    item_id = _resolve_item_id(request)
    if not item_id:
        return WalmartProductResponse(
            success=False,
            error="Could not extract item_id from URL. Provide item_id directly.",
            elapsed_ms=int(timer_ms() - start),
        )
    product = await _get_product_http(item_id)
    if product is None:
        logger.info("Walmart HTTP product fetch failed, trying Playwright fallback")
        product = await _get_product_playwright(item_id)
    elapsed = int(timer_ms() - start)
    if product is not None:
        return WalmartProductResponse(
            success=True, product=product, elapsed_ms=elapsed,
        )
    return WalmartProductResponse(
        success=False, error="Failed to fetch product details", elapsed_ms=elapsed,
    )
