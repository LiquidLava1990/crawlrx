"""woocommerce.py -- WooCommerce store detection + product scraping for CrawlRX

Standalone functions:
    detect_woocommerce(url, timeout) -> bool
    scrape_woocommerce_products(store_url, query, limit) -> List[Dict]

FastAPI router preserved for consistency (POST /scrape/woocommerce/products,
POST /scrape/woocommerce/product).

Strategy:
    1. WC Store API  /wp-json/wc/store/v1/products   (primary, 8s timeout)
    2. WC REST API   /wp-json/wc/v3/products          (fallback)
    3. Playwright CDP on port 9232                     (last resort)
"""

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_HEADERS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    first_image_src,
    normalize_base_url,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger('crawlrx.woocommerce')
router = APIRouter(prefix="", tags=["woocommerce"])

_HEADERS = CMS_HEADERS

_WC_PRODUCT_SELECTORS: tuple[str, ...] = (
    ".products .product",
    ".woocommerce-loop-product",
    ".product.type-product",
    '[class*="products"] [class*="product"]',
)

_WC_NAME_SELECTORS: tuple[str, ...] = (
    ".woocommerce-loop-product__title",
    ".product-title",
    "h2",
    "h3",
)


# ─────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────

def _probe_wc_endpoint(endpoint: str, timeout: int) -> bool:
    """Return True if `endpoint` returns a 200 + JSON list response."""
    try:
        resp = requests.get(
            endpoint, headers=_HEADERS, timeout=timeout, allow_redirects=True
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, list)
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("WC probe %s suppressed %s: %s", endpoint, type(exc).__name__, exc)
        return False


def _probe_wc_html(base: str, timeout: int) -> bool:
    """Return True if the base URL HTML contains a WooCommerce marker."""
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
        return resp.status_code == 200 and "woocommerce" in resp.text.lower()
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("WC HTML probe suppressed %s: %s", type(exc).__name__, exc)
        return False


def detect_woocommerce(url: str, timeout: int = 8) -> bool:
    """Return True if the URL is a WooCommerce store."""
    base = normalize_base_url(url)
    endpoints = (
        f"{base}/wp-json/wc/store/v1/products?per_page=1",
        f"{base}/wp-json/wc/v3/products?per_page=1",
    )
    if any(_probe_wc_endpoint(ep, timeout) for ep in endpoints):
        return True
    return _probe_wc_html(base, timeout)


# ─────────────────────────────────────────────
# API parsers
# ─────────────────────────────────────────────

def _parse_price(value: Any) -> Optional[float]:
    """Strip non-numeric chars, return float or None."""
    if value is None:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(value))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _minor_to_float(value: Any) -> Optional[float]:
    """Store API price coercion -- divide by 100 if value looks like minor units."""
    parsed = _parse_price(value)
    if parsed is None:
        return None
    if parsed > 999 and "." not in str(value):
        return round(parsed / 100, 2)
    return parsed


def _store_api_price_triple(raw: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(price, regular, sale) parsed via Store API minor-unit heuristic."""
    prices = raw.get("prices", {})
    coerce: Callable[[Any], Optional[float]] = _minor_to_float if prices else _parse_price
    return (
        coerce(prices.get("price") or raw.get("price") or ""),
        coerce(prices.get("regular_price") or raw.get("regular_price") or ""),
        coerce(prices.get("sale_price") or raw.get("sale_price") or ""),
    )


def _build_product_from_store_api(raw: dict) -> Dict[str, Any]:
    """Map WC Store API v1 product to standard dict."""
    price_f, regular_f, sale_f = _store_api_price_triple(raw)
    return {
        "title": raw.get("name", ""),
        "price": price_f,
        "regular_price": regular_f if regular_f else price_f,
        "sale_price": sale_f if sale_f and sale_f != regular_f else None,
        "url": raw.get("permalink") or raw.get("add_to_cart", {}).get("url") or None,
        "image": first_image_src(raw.get("images", [])),
        "in_stock": raw.get("is_in_stock", True),
        "sku": raw.get("sku") or None,
    }


def _build_product_from_rest_api(raw: dict) -> Dict[str, Any]:
    """Map WC REST API v3 product to standard dict."""
    price_f = _parse_price(raw.get("price"))
    regular_f = _parse_price(raw.get("regular_price"))
    sale_f = _parse_price(raw.get("sale_price"))
    return {
        "title": raw.get("name", ""),
        "price": price_f,
        "regular_price": regular_f if regular_f else price_f,
        "sale_price": sale_f if sale_f else None,
        "url": raw.get("permalink") or None,
        "image": first_image_src(raw.get("images", [])),
        "in_stock": raw.get("stock_status", "instock") == "instock",
        "sku": raw.get("sku") or None,
    }


def _fetch_wc_api(
    endpoint: str,
    params: Dict[str, Any],
    label: str,
    builder: Callable[[dict], Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Generic WC API fetch -- shared by Store-v1 and REST-v3 paths."""
    try:
        resp = requests.get(
            endpoint, headers=_HEADERS, params=params, timeout=8, allow_redirects=True
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                logger.info("WooCommerce %s: %d products from %s", label, len(data), endpoint)
                return [builder(p) for p in data]
        logger.info("WooCommerce %s returned %d for %s", label, resp.status_code, endpoint)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "WooCommerce %s API error for %s: %s: %s",
            label, endpoint, type(exc).__name__, exc,
        )
        return None  # explicit recovery: non-fatal: HTTP_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return None


def _fetch_store_api(base: str, limit: int, query: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Fetch products via WC Store API v1."""
    params: Dict[str, Any] = {"per_page": limit}
    if query:
        params["search"] = query
    return _fetch_wc_api(
        f"{base}/wp-json/wc/store/v1/products",
        params,
        "store/v1",
        _build_product_from_store_api,
    )


def _fetch_rest_api(base: str, limit: int, query: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Fetch products via WC REST API v3."""
    params: Dict[str, Any] = {"per_page": limit, "status": "publish"}
    if query:
        params["search"] = query
    return _fetch_wc_api(
        f"{base}/wp-json/wc/v3/products",
        params,
        "v3",
        _build_product_from_rest_api,
    )


# ─────────────────────────────────────────────
# Playwright fallback
# ─────────────────────────────────────────────

async def _first_card_name(card: Any) -> str:
    """Pull product name from the first matching selector inside a card."""
    for selector in _WC_NAME_SELECTORS:
        el = await card.query_selector(selector)
        if not el:
            continue
        text = (await el.inner_text()).strip()
        if text:
            return text
    return ""


async def _card_price(card: Any) -> Optional[float]:
    """Extract numeric price from a product card, or None."""
    price_el = await card.query_selector(".price, .woocommerce-Price-amount")
    if not price_el:
        return None
    price_text = (await price_el.inner_text()).strip()
    if not price_text:
        return None
    match = re.search(r"[\d,.]+", price_text.replace(",", ""))
    return _parse_price(match.group()) if match else None


async def _card_url(card: Any, store_url: str) -> Optional[str]:
    """Resolve product URL relative to store_url, or None."""
    link_el = await card.query_selector("a")
    if not link_el:
        return None
    href = await link_el.get_attribute("href")
    return urljoin(store_url, href) if href else None


async def _card_image(card: Any) -> Optional[str]:
    """Resolve image URL (handles protocol-relative URLs), or None."""
    img_el = await card.query_selector("img")
    if not img_el:
        return None
    src = await img_el.get_attribute("src") or await img_el.get_attribute("data-src")
    if src and src.startswith("//"):
        return "https:" + src
    return src


async def _parse_card(card: Any, store_url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse one product card; return None on miss / query mismatch."""
    name = await _first_card_name(card)
    if not name:
        return None
    if query and query.lower() not in name.lower():
        return None
    price_num = await _card_price(card)
    return {
        "title": name,
        "price": price_num,
        "regular_price": price_num,
        "sale_price": None,
        "url": await _card_url(card, store_url),
        "image": await _card_image(card),
        "in_stock": True,
        "sku": None,
    }


async def _query_first_match(page: Any, selectors: tuple[str, ...]) -> list[Any]:
    """Try selectors in order; return first non-empty result list."""
    for selector in selectors:
        cards = await page.query_selector_all(selector)
        if cards:
            return cards
    return []


async def _scroll_and_collect(
    page: Any, store_url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Walk product cards, parse each with narrow exception handling."""
    results: List[Dict[str, Any]] = []
    cards = await _query_first_match(page, _WC_PRODUCT_SELECTORS)
    for card in cards:
        if len(results) >= limit:
            break
        try:
            parsed = await _parse_card(card, store_url, query)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("WooCommerce PW card parse error: %s", exc)
            continue
        if parsed is not None:
            results.append(parsed)
    return results


async def _wait_for_idle(page: Any) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        return


async def _playwright_scrape_woocommerce(
    store_url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright CDP fallback -- port 9232 (kai)."""
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            try:
                await page.goto(
                    f"{store_url.rstrip('/')}/shop/",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await _wait_for_idle(page)
                return await _scroll_and_collect(page, store_url, query, limit)
            finally:
                await close_stealth_context(browser, ctx, page)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "WooCommerce Playwright fallback failed for %s: %s: %s",
            store_url, type(exc).__name__, exc,
        )
        return []


def scrape_woocommerce_products(
    store_url: str,
    query: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape products: Store API -> REST API v3 -> Playwright CDP."""
    base = normalize_base_url(store_url)
    products = _fetch_store_api(base, limit, query)
    if products is not None:
        return products
    products = _fetch_rest_api(base, limit, query)
    if products is not None:
        return products
    logger.warning("WooCommerce APIs failed, falling back to Playwright for %s", base)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(_playwright_scrape_woocommerce(base, query, limit))


# ─────────────────────────────────────────────
# Pydantic Models for router
# ─────────────────────────────────────────────

class _WCProduct(BaseModel):
    title: str
    price: Optional[float] = None
    regular_price: Optional[float] = None
    sale_price: Optional[float] = None
    url: Optional[str] = None
    image: Optional[str] = None
    in_stock: bool = True
    sku: Optional[str] = None


class _WCProductsRequest(BaseModel):
    url: str
    query: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)


class _WCProductsResponse(BaseModel):
    success: bool
    url: str
    products: List[_WCProduct] = Field(default_factory=list)
    total: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


_PRIVATE_HELPERS = (_WCProductsRequest,)


# ─────────────────────────────────────────────
# FastAPI Router
# ─────────────────────────────────────────────

@router.post("/scrape/woocommerce/products", response_model=_WCProductsResponse)
def scrape_woocommerce_products_endpoint(
    request: _WCProductsRequest,
) -> _WCProductsResponse:
    """Scrape WooCommerce products via Store API, REST API, or DOM fallback (sync route)."""
    start_time = time.time()
    base = normalize_base_url(request.url)
    try:
        raw = scrape_woocommerce_products(base, query=request.query, limit=request.limit)
        elapsed_ms = int((time.time() - start_time) * 1000)
        products = [_WCProduct(**p) for p in raw]
        return _WCProductsResponse(
            success=True,
            url=base,
            products=products,
            total=len(products),
            elapsed_ms=elapsed_ms,
        )
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(
            "WooCommerce products endpoint error for %s: %s: %s",
            base, type(exc).__name__, exc,
        )
        return _WCProductsResponse(
            success=False,
            url=base,
            error=str(exc),
            elapsed_ms=elapsed_ms,
        )
