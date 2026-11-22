"""squarespace_scraper.py -- POST /scrape/squarespace/products

Squarespace eCommerce product scraper -- platform detection and catalog.

Strategy (products):
  1. JSON endpoint -- GET /shop?format=json-pretty (Squarespace commerce API)
  2. DOM scrape via Playwright -- fallback on API failure (CDP port 9234 / knox)
"""

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

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.squarespace_scraper")
router = APIRouter(prefix="", tags=["squarespace"])

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/json, text/html, */*",
}

_PRODUCT_CARD_SELECTORS: tuple[str, ...] = (
    ".product-grid-item",
    ".product-grid .grid-item",
    ".ProductList-item",
    "[data-hook='product-list-grid-item']",
    ".sqs-product",
    ".product",
    ".product-list .product",
)

_TITLE_SELECTOR = (
    ".product-title, .ProductList-title, .product-name, h1, h2, h3, "
    ".title, [data-hook='product-item-name']"
)
_PRICE_SELECTOR = (
    ".product-price, .product-price-original, .price, "
    "[data-hook='product-item-price'], .money"
)
_SOLD_OUT_SELECTOR = (
    ".sold-out, .out-of-stock, [data-hook='product-item-out-of-stock']"
)


# ─────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────

def detect_squarespace(url: str, timeout: int = 8) -> bool:
    """Return True if the URL is a Squarespace site."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("[Squarespace] detect_squarespace failed for %s: %s", url, exc)
        return False
    if any("x-squarespace" in key.lower() for key in resp.headers):
        return True
    return "static1.squarespace.com" in resp.text


# ─────────────────────────────────────────────
# Products -- JSON API primary
# ─────────────────────────────────────────────

def _extract_price(item: dict) -> Optional[str]:
    """Pull a price string out of a Squarespace item dict."""
    if "price" in item:
        price_data = item.get("price", {})
        if isinstance(price_data, dict):
            return (
                price_data.get("formatted")
                or price_data.get("decimalValue")
                or price_data.get("value")
            )
        return str(price_data)
    if "fromPrice" in item:
        return item.get("fromPrice")
    if "priceCents" in item:
        cents = item.get("priceCents")
        if isinstance(cents, (int, float)):
            return "$%.2f" % (cents / 100)
    return None


def _first_image_keys(image: dict) -> Optional[str]:
    """Try url / secureUrl / assetUrl on an image dict."""
    return image.get("url") or image.get("secureUrl") or image.get("assetUrl")


def _extract_image(item: dict) -> Optional[str]:
    """Pull image URL from a Squarespace item dict."""
    image = item.get("image")
    if isinstance(image, dict):
        return _first_image_keys(image)
    images = item.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return _first_image_keys(first)
    return None


def _extract_item_url(item: dict, store_url: str) -> str:
    """Resolve a Squarespace item URL (absolute or store-relative)."""
    item_url = item.get("url") or item.get("fullUrl") or item.get("itemUrl")
    if not item_url:
        return store_url
    if item_url.startswith("http"):
        return item_url
    return store_url.rstrip("/") + "/" + item_url.lstrip("/")


def _extract_in_stock(item: dict) -> bool:
    """Resolve `in_stock` from inventory / quantity fields."""
    inventory = item.get("inventory")
    if isinstance(inventory, dict):
        return bool(inventory.get("inStock", True))
    if "quantity" in item:
        qty = item.get("quantity")
        if qty is None:
            return True
        return isinstance(qty, (int, float)) and qty > 0
    return True


def _parse_squarespace_item(item: dict, store_url: str) -> Optional[Dict[str, Any]]:
    """Normalize a Squarespace commerce item to standard dict."""
    title = item.get("title") or item.get("name") or ""
    if not title:
        return None
    price = _extract_price(item)
    return {
        "title": title,
        "price": str(price) if price is not None else None,
        "url": _extract_item_url(item, store_url),
        "image": _extract_image(item),
        "in_stock": _extract_in_stock(item),
    }


def _items_from_api_payload(data: Any) -> list[Any]:
    """Pull items list from an unwieldy Squarespace JSON payload shape."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    items = data.get("items") or data.get("products") or data.get("collection") or []
    if items:
        return items
    if "commerceEnabled" in data:
        collection = data.get("collection")
        if isinstance(collection, dict):
            return collection.get("items") or collection.get("products") or []
    return []


def _scrape_api(url: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """GET Squarespace commerce JSON endpoint."""
    endpoint = f"{url.rstrip('/')}/shop?format=json-pretty"
    try:
        resp = requests.get(endpoint, headers=_HEADERS, timeout=8)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("[Squarespace] API fetch failed for %s: %s: %s", endpoint, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("[Squarespace] API returned %d for %s", resp.status_code, endpoint)
        return None
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.warning("[Squarespace] API JSON parse failed for %s: %s", endpoint, exc)
        return None
    items = _items_from_api_payload(data)
    if not items:
        return None
    products: list[Dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        normalized = _parse_squarespace_item(item, url)
        if normalized:
            products.append(normalized)
    return products or None


# ─────────────────────────────────────────────
# Products -- Playwright fallback
# ─────────────────────────────────────────────

async def _wait_for_idle(page: Any, timeout_ms: int = 15000) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return


async def _navigate_to_shop_if_needed(page: Any) -> None:
    """Click the first shop/store/products link if not already on /shop."""
    current_path = await page.evaluate("() => window.location.pathname")
    if "/shop" in current_path:
        return
    try:
        link = await page.query_selector(
            "a[href*='shop'], a[href*='store'], a[href*='products']"
        )
        if not link:
            return
        await link.click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await _wait_for_idle(page, 10000)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("[Squarespace] Navigation to shop failed: %s", exc)


async def _query_first_match(page: Any, selectors: tuple[str, ...]) -> list[Any]:
    """Try each selector in order; return first non-empty match list."""
    for selector in selectors:
        items = await page.query_selector_all(selector)
        if items:
            return items
    return []


async def _card_title(card: Any) -> str:
    """Extract product title text or empty string."""
    el = await card.query_selector(_TITLE_SELECTOR)
    return (await el.inner_text()).strip() if el else ""


async def _card_price(card: Any) -> Optional[str]:
    """Extract product price text or None."""
    el = await card.query_selector(_PRICE_SELECTOR)
    return (await el.inner_text()).strip() if el else None


async def _card_image(card: Any) -> Optional[str]:
    """Extract product image URL or None."""
    img = await card.query_selector("img")
    if not img:
        return None
    return (
        await img.get_attribute("src")
        or await img.get_attribute("data-src")
        or await img.get_attribute("data-image")
    )


async def _card_url(card: Any, base_url: str) -> str:
    """Resolve product link URL or fall back to base."""
    link = await card.query_selector("a")
    if not link:
        return base_url
    href = await link.get_attribute("href") or ""
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


async def _card_in_stock(card: Any) -> bool:
    """Detect explicit sold-out marker."""
    sold_out = await card.query_selector(_SOLD_OUT_SELECTOR)
    return sold_out is None


async def _parse_dom_card(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Parse a single product card; None if no title."""
    title = await _card_title(card)
    if not title:
        return None
    return {
        "title": title,
        "price": await _card_price(card),
        "url": await _card_url(card, base_url),
        "image": await _card_image(card),
        "in_stock": await _card_in_stock(card),
    }


async def _safe_parse_dom_card(card: Any, url: str) -> Optional[Dict[str, Any]]:
    """Parse one DOM card, returning None on recoverable extraction error."""
    try:
        return await _parse_dom_card(card, url)
    except CARD_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("[Squarespace] DOM item parse error: %s", exc)
        # explicit recovery: per-card failure non-fatal; caller skips
        return None


async def _do_dom_scrape(page: Any, url: str, limit: int) -> List[Dict[str, Any]]:
    """Run the actual product walk inside an open page."""
    logger.info("[Squarespace] DOM fallback for %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_for_idle(page)
    await _navigate_to_shop_if_needed(page)
    cards = await _query_first_match(page, _PRODUCT_CARD_SELECTORS)
    products: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        parsed = await _safe_parse_dom_card(card, url)
        if parsed is not None:
            products.append(parsed)
    return products


async def _scrape_dom(url: str, limit: int) -> List[Dict[str, Any]]:
    """Playwright DOM fallback for Squarespace product scraping (port 9234)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await _do_dom_scrape(page, url, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("[Squarespace] DOM fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Public scrape function
# ─────────────────────────────────────────────

def _filter_by_query(products: list[Dict[str, Any]], query: Optional[str]) -> list[Dict[str, Any]]:
    """Case-insensitive title filter, no-op when query is empty."""
    if not query:
        return products
    q_lower = query.lower()
    return [p for p in products if q_lower in (p.get("title") or "").lower()]


def scrape_squarespace_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape Squarespace products. API first, Playwright DOM fallback."""
    try:
        api_products = _scrape_api(url, query, limit)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("[Squarespace] API path failed for %s: %s: %s", url, type(exc).__name__, exc)
        api_products = None
        return None  # explicit recovery: non-fatal: HTTP_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    if api_products:
        filtered = _filter_by_query(api_products, query)
        logger.info("[Squarespace] API: %d products from %s", len(filtered), url)
        return filtered[:limit]
    logger.info("[Squarespace] API failed, falling back to DOM for %s", url)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    dom_products = asyncio.run(_scrape_dom(url, limit))
    filtered = _filter_by_query(dom_products, query)
    logger.info("[Squarespace] DOM: %d products from %s", len(filtered), url)
    return filtered[:limit]


# ─────────────────────────────────────────────
# FastAPI endpoint
# ─────────────────────────────────────────────

class SquarespaceProductsRequest(BaseModel):
    url: str = Field(..., description="Squarespace store URL (e.g., https://example.squarespace.com)")
    query: Optional[str] = Field(default=None, description="Filter by product name")
    limit: int = Field(default=50, ge=1, le=250, description="Max products to return")


class SquarespaceProductsResponse(BaseModel):
    success: bool
    url: str
    products: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


_ENDPOINT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    *HTTP_SCRAPE_EXCEPTIONS,
    *CMS_PLAYWRIGHT_EXCEPTIONS,
)


@router.post(
    "/scrape/squarespace/products",
    response_model=SquarespaceProductsResponse,
    summary="Scrape Squarespace store products",
)
async def scrape_squarespace_endpoint(request: SquarespaceProductsRequest) -> SquarespaceProductsResponse:
    """Scrape products from a Squarespace store (API first, DOM fallback)."""
    start = time.time()
    try:
        products = await asyncio.wait_for(
            asyncio.to_thread(
                scrape_squarespace_products,
                request.url,
                request.query,
                request.limit,
            ),
            timeout=60,
        )
        elapsed = int((time.time() - start) * 1000)
        return SquarespaceProductsResponse(
            success=True,
            url=request.url,
            products=products,
            total=len(products),
            elapsed_ms=elapsed,
        )
    except _ENDPOINT_EXCEPTIONS as exc:
        elapsed = int((time.time() - start) * 1000)
        logger.warning(
            "[Squarespace] Endpoint error for %s: %s: %s",
            request.url, type(exc).__name__, exc,
        )
        return SquarespaceProductsResponse(
            success=False,
            url=request.url,
            error=str(exc),
            elapsed_ms=elapsed,
        )


