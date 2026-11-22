"""shopify.py -- Shopify store detection + product scraping for CrawlRX

Standalone functions:
    detect_shopify(url, timeout) -> bool
    scrape_shopify_products(store_url, query, limit) -> List[Dict]
"""

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin

import requests
from fastapi import APIRouter
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from ._cms_helpers import REQUESTS_EXCEPTIONS
from ._ecommerce_product_helpers import (
    BROWSER_OR_HTTP_EXCEPTIONS,
    CARD_PROBE_EXCEPTIONS,
    build_shopify_product,
    parse_price_decimal,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.shopify")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_PRICE_NUM_RE = re.compile(r"[\d,.]+")

_PW_CARD_SELECTOR = (
    ".product-card, .product-item, .grid__item, [class*=\"product-card\"]"
)
_PW_TITLE_SELECTOR = (
    "h2, h3, .product-card__title, .product__title, [class*=\"title\"]"
)
_PW_PRICE_SELECTOR = '.price, [class*="price"]'


def _has_shopify_products_json(base: str, timeout: int) -> bool:
    """True if `{base}/products.json` returns 200 + a `products` key."""
    try:
        resp = requests.get(
            f"{base}/products.json",
            headers=_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify products.json", exc)
        return False
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify products.json decode", exc)
        return False
    return isinstance(data, dict) and "products" in data


def _has_shopify_html_markers(base: str, timeout: int) -> bool:
    """True if the HTML at `base` contains known Shopify CDN / theme markers."""
    try:
        resp = requests.get(
            base,
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify html probe", exc)
        return False
    if resp.status_code != 200:
        return False
    text = resp.text
    return "cdn.shopify.com" in text or "Shopify.theme" in text


def detect_shopify(url: str, timeout: int = 8) -> bool:
    """Check if a URL is a Shopify store. Returns False on any error."""
    base = url.strip().rstrip("/")
    if _has_shopify_products_json(base, timeout):
        return True
    return _has_shopify_html_markers(base, timeout)


async def _pw_card_title(card: Any) -> Optional[str]:
    """Read the product-card title; None when missing."""
    title_el = await card.query_selector(_PW_TITLE_SELECTOR)
    if not title_el:
        return None
    text = (await title_el.inner_text()).strip()
    return text or None


async def _pw_card_price(card: Any) -> Optional[float]:
    """Read the product-card price as a numeric value; None when missing."""
    price_el = await card.query_selector(_PW_PRICE_SELECTOR)
    if not price_el:
        return None
    raw_text = (await price_el.inner_text()).strip()
    if not raw_text:
        return None
    match = _PRICE_NUM_RE.search(raw_text.replace(",", ""))
    if not match:
        return None
    return parse_price_decimal(match.group())


async def _pw_card_url(card: Any, store_url: str) -> Optional[str]:
    """Read the product-card URL; None when missing."""
    link_el = await card.query_selector("a")
    if not link_el:
        return None
    href = await link_el.get_attribute("href")
    if not href:
        return None
    return urljoin(store_url, href)


async def _pw_card_image(card: Any) -> Optional[str]:
    """Read the product-card image URL; None when missing."""
    img_el = await card.query_selector("img")
    if not img_el:
        return None
    src = await img_el.get_attribute("src") or await img_el.get_attribute("data-src")
    if not src:
        return None
    if src.startswith("//"):
        return f"https:{src}"
    return src


async def _pw_extract_card(card: Any, store_url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract a single product card; None when title is missing or filtered out."""
    try:
        title = await _pw_card_title(card)
    except CARD_PROBE_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify pw card title", exc)
        return None
    if not title:
        return None
    if query and query.lower() not in title.lower():
        return None
    try:
        price_num = await _pw_card_price(card)
        product_url = await _pw_card_url(card, store_url)
        image = await _pw_card_image(card)
    except CARD_PROBE_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify pw card field", exc)
        return None
    return {
        "title": title,
        "price": price_num,
        "compare_at_price": None,
        "url": product_url,
        "image": image,
        "available": True,
        "sku": None,
        "vendor": None,
    }


async def pw_close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing narrow shutdown failures."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shopify pw browser close", exc)


async def _pw_wait_networkidle(page: Any) -> None:
    """Wait for networkidle, swallowing the expected Playwright timeout."""
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass


async def _pw_collect_cards(
    page: Any, store_url: str, query: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Iterate the collections-grid cards and produce normalized products."""
    cards = await page.query_selector_all(_PW_CARD_SELECTOR)
    results: List[Dict[str, Any]] = []
    for card in cards:
        if len(results) >= limit:
            break
        product = await _pw_extract_card(card, store_url, query)
        if product:
            results.append(product)
    return results


async def _playwright_scrape_shopify(
    store_url: str, query: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Playwright CDP fallback -- port 9231 (kai)."""
    browser = None
    ctx = None
    page = None
    results: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser, ctx, page = await make_stealth_context(pw)
            await page.goto(
                f"{store_url}/collections/all",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await _pw_wait_networkidle(page)
            results = await _pw_collect_cards(page, store_url, query, limit)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.warning("Shopify Playwright fallback failed for %s: %s", store_url, exc)
        return None  # explicit recovery: non-fatal: BROWSER_OR_HTTP_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await pw_close_browser(browser)
    return results


def _http_scrape_shopify_json(base: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch and decode `/products.json?limit=250`. None on transport / decode failure."""
    try:
        resp = requests.get(
            f"{base}/products.json?limit=250",
            headers=_HEADERS,
            timeout=8,
            allow_redirects=True,
        )
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("Shopify JSON API error for %s: %s", base, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Shopify /products.json returned %d for %s", resp.status_code, base)
        return None
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.warning("Shopify JSON decode failed for %s: %s", base, exc)
        return None
    raw = data.get("products", []) if isinstance(data, dict) else []
    return raw if isinstance(raw, list) else []


def _filter_shopify_products(
    raw_products: List[Dict[str, Any]],
    base: str,
    query: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Filter and normalize raw Shopify products."""
    results: List[Dict[str, Any]] = []
    for product in raw_products:
        if query and query.lower() not in product.get("title", "").lower():
            continue
        results.append(build_shopify_product(product, base))
        if len(results) >= limit:
            break
    return results


def scrape_shopify_products(
    store_url: str,
    query: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape products from a Shopify store.

    PRIMARY: GET {store_url}/products.json?limit=250
    Filter by query on title if provided.
    Each product: {title, price (float), compare_at_price (float or None), url,
    image, available (bool), sku, vendor}.

    FALLBACK: if JSON API returns non-200, use Playwright on port 9231.
    Returns [] on total failure, logs a warning -- never raises.
    """
    base = store_url.strip().rstrip("/")
    raw = _http_scrape_shopify_json(base)
    if raw is not None:
        results = _filter_shopify_products(raw, base, query, limit)
        if results:
            logger.info("Shopify JSON: %d products from %s", len(results), base)
            return results
    logger.warning("Shopify falling back to Playwright for %s", base)
    return _playwright_scrape_shopify(base, query, limit)


# -- FastAPI Router ------------------------------------------------------------

router = APIRouter(prefix="", tags=["shopify"])


class ShopifyScrapeRequest(BaseModel):
    """Request model for POST /scrape/shopify."""

    url: str
    query: Optional[str] = None
    limit: int = 50


class ShopifyProduct(BaseModel):
    """Single Shopify product."""

    title: str
    price: Optional[float] = None
    compare_at_price: Optional[float] = None
    url: Optional[str] = None
    image: Optional[str] = None
    available: bool = True
    sku: Optional[str] = None
    vendor: Optional[str] = None


class ShopifyScrapeResponse(BaseModel):
    """Response model for POST /scrape/shopify."""

    success: bool
    store_url: str
    products: List[ShopifyProduct]
    count: int
    error: Optional[str] = None


@router.post(
    "/scrape/shopify",
    response_model=ShopifyScrapeResponse,
    summary="Scrape Shopify store products",
)
async def scrape_shopify_endpoint(request: ShopifyScrapeRequest) -> ShopifyScrapeResponse:
    """Scrape products from a Shopify store."""
    try:
        products = scrape_shopify_products(request.url, request.query, request.limit)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("Shopify scrape failed: %s", exc)
        return ShopifyScrapeResponse(
            success=False,
            store_url=request.url,
            products=[],
            count=0,
            error=str(exc),
        )
    return ShopifyScrapeResponse(
        success=True,
        store_url=request.url,
        products=[ShopifyProduct(**p) for p in products],
        count=len(products),
    )
