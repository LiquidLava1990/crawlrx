"""godaddy_scraper.py -- POST /scrape/godaddy/products

GoDaddy Stores product scraper -- platform detection and catalog.

GoDaddy Stores runs on their own platform (formerly GoCentral) or WooCommerce.

Strategy (products):
  1. JSON probe -- GET /api/product then GET /products (REST API)
  2. DOM scrape via Playwright -- fallback (CDP port 9234 / knox)
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

logger = logging.getLogger("crawlrx.godaddy_scraper")
router = APIRouter(prefix="", tags=["godaddy"])

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/json, text/html, */*",
}

_DETECTION_TEXT_MARKERS: tuple[str, ...] = ("img1.wsimg.com", "secureserver.net")
_DETECTION_HEADER_MARKERS: tuple[str, ...] = ("godaddy", "secureserver")

_API_PATHS: tuple[str, ...] = ("/api/product", "/products")

_CARD_SELECTORS: tuple[str, ...] = (
    ".product-card",
    "[class*='product-card']",
    ".product-item",
    "[class*='ProductCard']",
    "li.product",
    "article.product",
)

_TITLE_SELECTOR = (
    ".product-title, [class*='product-title'], [class*='ProductTitle'], h2, h3"
)
_PRICE_SELECTOR = ".price, [class*='price'], [class*='Price']"


# ─────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────

def detect_godaddy(url: str, timeout: int = 8) -> bool:
    """Return True if the URL is a GoDaddy / GoDaddy Stores site."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("[GoDaddy] detect_godaddy failed for %s: %s: %s", url, type(exc).__name__, exc)
        return False
    if any(marker in resp.text for marker in _DETECTION_TEXT_MARKERS):
        return True
    return any(
        any(needle in val.lower() for needle in _DETECTION_HEADER_MARKERS)
        for val in resp.headers.values()
    )


# ─────────────────────────────────────────────
# Products -- JSON API primary
# ─────────────────────────────────────────────

def _normalize_item(item: dict, base_url: str) -> Optional[Dict[str, Any]]:
    """Normalize a raw API item to standard dict."""
    title = item.get("name") or item.get("title") or item.get("productName") or ""
    if not title:
        return None
    price = item.get("price") or item.get("salePrice") or item.get("regularPrice")
    if isinstance(price, (int, float)):
        price = str(price)
    image = item.get("image") or item.get("imageUrl") or item.get("thumbnail") or None
    link = item.get("url") or item.get("link") or item.get("productUrl") or base_url
    return {"title": title, "price": price, "url": link, "image": image}


def _items_from_api_payload(data: Any) -> list[Any]:
    """Extract items list from list / dict API payload shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("products") or data.get("items") or data.get("data") or []
    return []


def _try_json_endpoint(base: str, path: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    """GET a JSON product endpoint and normalize results."""
    endpoint = f"{base.rstrip('/')}{path}"
    try:
        resp = requests.get(endpoint, headers=_HEADERS, timeout=8)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("[GoDaddy] JSON endpoint %s failed: %s: %s", endpoint, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("[GoDaddy] %s returned %d", endpoint, resp.status_code)
        return None
    try:
        raw_items = _items_from_api_payload(resp.json())
    except (ValueError, TypeError) as exc:
        logger.warning("[GoDaddy] JSON decode failed for %s: %s", endpoint, exc)
        return None
    products: list[Dict[str, Any]] = []
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_item(item, base)
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


async def _query_first_match(page: Any, selectors: tuple[str, ...]) -> list[Any]:
    """Try selectors in order; return first non-empty match list."""
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
    return await img.get_attribute("src") or await img.get_attribute("data-src")


async def _card_url(card: Any, base_url: str) -> str:
    """Resolve product link or fall back to base."""
    link = await card.query_selector("a")
    if not link:
        return base_url
    href = await link.get_attribute("href") or ""
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


async def _parse_dom_card(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Parse one product card; None if no title."""
    title = await _card_title(card)
    if not title:
        return None
    return {
        "title": title,
        "price": await _card_price(card),
        "url": await _card_url(card, base_url),
        "image": await _card_image(card),
    }


async def _safe_parse_dom_card(card: Any, url: str) -> Optional[Dict[str, Any]]:
    """Parse a single DOM card, returning None on recoverable extraction error."""
    try:
        return await _parse_dom_card(card, url)
    except CARD_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("[GoDaddy] DOM item parse error: %s", exc)
        # explicit recovery: CARD_EXTRACTION_EXCEPTIONS is non-fatal; caller skips card
        return None


async def _do_dom_scrape(page: Any, url: str, limit: int) -> List[Dict[str, Any]]:
    """Run the actual product walk inside an open page."""
    logger.info("[GoDaddy] DOM fallback for %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_for_idle(page)
    cards = await _query_first_match(page, _CARD_SELECTORS)
    products: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        parsed = await _safe_parse_dom_card(card, url)
        if parsed is not None:
            products.append(parsed)
    return products


async def _scrape_dom(url: str, limit: int) -> List[Dict[str, Any]]:
    """Playwright DOM fallback for GoDaddy product scraping (port 9234)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await _do_dom_scrape(page, url, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("[GoDaddy] DOM fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Public scrape function
# ─────────────────────────────────────────────

def _filter_by_query(
    products: list[Dict[str, Any]], query: Optional[str],
) -> list[Dict[str, Any]]:
    """Case-insensitive title filter, no-op when query is empty."""
    if not query:
        return products
    q_lower = query.lower()
    return [p for p in products if q_lower in (p.get("title") or "").lower()]


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously, returning [] when a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(coro)


def scrape_godaddy_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape products from a GoDaddy Stores site (API first, DOM fallback)."""
    base = url.rstrip("/")
    for path in _API_PATHS:
        products = _try_json_endpoint(base, path, limit)
        if products:
            filtered = _filter_by_query(products, query)
            logger.info("[GoDaddy] API (%s): %d products from %s", path, len(filtered), url)
            return filtered[:limit]
    logger.info("[GoDaddy] API failed, falling back to DOM for %s", url)
    dom_products = _run_async(_scrape_dom(url, limit))
    filtered = _filter_by_query(dom_products, query)
    logger.info("[GoDaddy] DOM: %d products from %s", len(filtered), url)
    return filtered[:limit]


# ─────────────────────────────────────────────
# FastAPI endpoint
# ─────────────────────────────────────────────

class GoDaddyProductsRequest(BaseModel):
    url: str = Field(..., description="GoDaddy store URL (e.g., https://mystore.godaddysites.com)")
    query: Optional[str] = Field(default=None, description="Filter by product name")
    limit: int = Field(default=50, ge=1, le=250, description="Max products to return")


class GoDaddyProductsResponse(BaseModel):
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
    "/scrape/godaddy/products",
    response_model=GoDaddyProductsResponse,
    summary="Scrape GoDaddy store products",
)
async def scrape_godaddy_endpoint(request: GoDaddyProductsRequest) -> GoDaddyProductsResponse:
    """Scrape products from a GoDaddy Stores site."""
    start = time.time()
    try:
        products = await asyncio.wait_for(
            asyncio.to_thread(
                scrape_godaddy_products,
                request.url,
                request.query,
                request.limit,
            ),
            timeout=60,
        )
        elapsed = int((time.time() - start) * 1000)
        return GoDaddyProductsResponse(
            success=True,
            url=request.url,
            products=products,
            total=len(products),
            elapsed_ms=elapsed,
        )
    except _ENDPOINT_EXCEPTIONS as exc:
        elapsed = int((time.time() - start) * 1000)
        logger.warning(
            "[GoDaddy] Endpoint error for %s: %s: %s",
            request.url, type(exc).__name__, exc,
        )
        return GoDaddyProductsResponse(
            success=False,
            url=request.url,
            error=str(exc),
            elapsed_ms=elapsed,
        )
