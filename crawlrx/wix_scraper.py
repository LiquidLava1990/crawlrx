"""wix_scraper.py -- Wix eCommerce platform detect + product catalog scrape."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    network_exception_types,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.wix_scraper")
router = APIRouter(prefix="", tags=["wix"])

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/json, text/html, */*",
}
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    *network_exception_types(),
    requests.RequestException,
    ValueError,
)
_WIX_SOURCE_MARKERS: tuple[str, ...] = ("wixstatic.com", "static.parastorage.com")
_DOM_CARD_SELECTORS: tuple[str, ...] = (
    "[data-hook='product-list-grid-item']",
    ".product-item",
    "[class*='productItem']",
    "[class*='ProductItem']",
)


def detect_wix(url: str, timeout: int = 8) -> bool:
    """Return True if the URL is a Wix site."""
    try:
        resp = requests.get(
            url, headers=_HEADERS, timeout=timeout, allow_redirects=True
        )
    except _NETWORK_ERRORS as exc:
        logger.warning(
            "[Wix] detect_wix failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        return False
    for key in resp.headers:
        if "x-wix-request-id" in key.lower():
            return True
    src = resp.text
    return any(marker in src for marker in _WIX_SOURCE_MARKERS)


# ─────────────────────────────────────────────
# API path
# ─────────────────────────────────────────────


def _parse_wix_item(item: dict, store_url: str) -> Dict[str, Any]:
    """Normalize a Wix catalog item to standard dict."""
    media = item.get("media") or {}
    main_media = media.get("mainMedia") or {}
    image_obj = main_media.get("image") or {}
    price_data = item.get("price") or {}
    raw_price = price_data.get("amount")
    stock = item.get("stock") or {}
    return {
        "title": item.get("name", ""),
        "price": str(raw_price) if raw_price is not None else None,
        "url": store_url,
        "image": image_obj.get("url"),
        "available": stock.get("inStock", True),
    }


def _scrape_api(
    url: str, query: Optional[str], limit: int
) -> Optional[List[Dict[str, Any]]]:
    """POST to the Wix eCommerce Storefront API."""
    endpoint = (
        f"{url.rstrip('/')}"
        "/_api/wix-ecommerce-storefront-web/api/v1/catalog/products/query"
    )
    query_block: Dict[str, Any] = {"paging": {"limit": limit}}
    if query:
        query_block["filter"] = {"name": query}
    body: Dict[str, Any] = {"query": query_block}
    try:
        resp = requests.post(
            endpoint,
            json=body,
            headers={**_HEADERS, "Content-Type": "application/json"},
            timeout=8,
        )
    except _NETWORK_ERRORS as exc:
        logger.warning(
            "[Wix] API fetch failed for %s: %s: %s",
            endpoint, type(exc).__name__, exc,
        )
        return None
    if resp.status_code != 200:
        logger.warning("[Wix] API returned %d for %s", resp.status_code, endpoint)
        return None
    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("[Wix] API JSON decode failed for %s: %s", endpoint, exc)
        return None
    items = data.get("items") or []
    if not items:
        return None
    return [_parse_wix_item(item, url) for item in items]


# ─────────────────────────────────────────────
# DOM fallback
# ─────────────────────────────────────────────


async def _select_first_set(page: Any) -> List[Any]:
    """Try each card selector and return the first non-empty match list."""
    for selector in _DOM_CARD_SELECTORS:
        items = await page.query_selector_all(selector)
        if items:
            return items
    return []


async def _wait_idle(page: Any) -> None:
    """Wait for networkidle (non-fatal on timeout)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug("[Wix] networkidle wait timed out")


async def _resolve_inner_text(el: Any, selector: str) -> Optional[str]:
    """Resolve ``el.query_selector(selector).inner_text().strip()`` or ``None``."""
    target = await el.query_selector(selector)
    if target is None:
        return None
    text = await target.inner_text()
    return text.strip() if text else None


async def _resolve_link(el: Any, base: str) -> str:
    """Resolve the product anchor href to an absolute URL or ``base``."""
    link_el = await el.query_selector("a")
    if link_el is None:
        return base
    href = await link_el.get_attribute("href")
    if not href:
        return base
    if href.startswith("http"):
        return href
    return f"{base.rstrip('/')}/{href.lstrip('/')}"


async def _resolve_image(el: Any) -> Optional[str]:
    """Resolve a card's image src (or data-src fallback)."""
    img_el = await el.query_selector("img")
    if img_el is None:
        return None
    src = await img_el.get_attribute("src")
    if src:
        return src
    return await img_el.get_attribute("data-src")


async def _build_dom_card(el: Any, base: str) -> Optional[Dict[str, Any]]:
    """Build one product dict from a DOM card element."""
    try:
        title = await _resolve_inner_text(
            el,
            ".product-name, [data-hook='product-item-name'], [class*='productName']",
        )
        price = await _resolve_inner_text(
            el,
            ".product-price, [data-hook='product-item-price-to-pay'], [class*='productPrice']",
        )
        image = await _resolve_image(el)
        prod_url = await _resolve_link(el, base)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("[Wix] DOM item parse error: %s: %s", type(exc).__name__, exc)
        return None
    if not title:
        return None
    return {
        "title": title,
        "price": price,
        "url": prod_url,
        "image": image,
        "available": True,
    }


async def _scrape_dom(url: str, limit: int) -> List[Dict[str, Any]]:
    """Playwright DOM fallback for Wix product scraping."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Wix] DOM fallback for %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _wait_idle(page)
            items = await _select_first_set(page)
            return await _walk_dom_cards(items[:limit], url)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "[Wix] DOM fallback failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


async def _walk_dom_cards(items: list, url: str) -> List[Dict[str, Any]]:
    """Walk DOM cards into product dicts (skipping malformed)."""
    products: List[Dict[str, Any]] = []
    for el in items:
        built = await _build_dom_card(el, url)
        if built is not None:
            products.append(built)
    return products


# ─────────────────────────────────────────────
# Public scrape entry
# ─────────────────────────────────────────────


def _filter_by_query(
    products: List[Dict[str, Any]], query: Optional[str]
) -> List[Dict[str, Any]]:
    """Return only the products whose title contains ``query`` (case-insensitive)."""
    if not query:
        return products
    q = query.lower()
    return [p for p in products if q in (p.get("title") or "").lower()]


async def scrape_wix_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Scrape products from a Wix store. Returns [] on total failure."""
    api_products = _scrape_api(url, query, limit)
    if api_products:
        logger.info("[Wix] API: %d products from %s", len(api_products), url)
        return api_products[:limit]
    logger.info("[Wix] API failed, falling back to DOM for %s", url)
    dom_products = await _scrape_dom(url, limit)
    dom_products = _filter_by_query(dom_products, query)
    logger.info("[Wix] DOM: %d products from %s", len(dom_products), url)
    return dom_products[:limit]


# ─────────────────────────────────────────────
# FastAPI endpoint
# ─────────────────────────────────────────────


class WixProductsRequest(BaseModel):
    """Request body for POST /scrape/wix/products."""

    url: str = Field(..., description="Wix store URL")
    query: Optional[str] = Field(default=None, description="Filter by product name")
    limit: int = Field(default=50, ge=1, le=250, description="Max products to return")


class WixProductsResponse(BaseModel):
    """Response body for POST /scrape/wix/products."""

    success: bool
    url: str
    products: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


_ENDPOINT_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    *_NETWORK_ERRORS,
    *PLAYWRIGHT_EXCEPTIONS,
)


@router.post(
    "/scrape/wix/products",
    response_model=WixProductsResponse,
    summary="Scrape Wix store products",
)
async def scrape_wix_endpoint(request: WixProductsRequest) -> WixProductsResponse:
    """Scrape products from a Wix eCommerce store (API first, DOM fallback)."""
    start = time.time()
    try:
        products = await asyncio.wait_for(
            scrape_wix_products(request.url, request.query, request.limit),
            timeout=60,
        )
    except _ENDPOINT_ERRORS as exc:
        elapsed = int((time.time() - start) * 1000)
        logger.warning(
            "[Wix] Endpoint error for %s: %s: %s",
            request.url, type(exc).__name__, exc,
        )
        return WixProductsResponse(
            success=False, url=request.url, error=str(exc), elapsed_ms=elapsed,
        )
    elapsed = int((time.time() - start) * 1000)
    return WixProductsResponse(
        success=True,
        url=request.url,
        products=products,
        total=len(products),
        elapsed_ms=elapsed,
    )


