"""bigcommerce_scraper.py -- BigCommerce product scraper.

Functions:
  detect_bigcommerce(url) -> bool
  scrape_bigcommerce_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9236.
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from ._cms_helpers import REQUESTS_EXCEPTIONS
from ._ecommerce_product_helpers import (
    BROWSER_OR_HTTP_EXCEPTIONS,
    CARD_PROBE_EXCEPTIONS,
    StoreScrapeParams,
    parse_price_string,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.bigcommerce")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_DETECT_HTML_MARKERS: tuple[str, ...] = ("cdn.bigcommerce.com", "bc-sf-filter")
_BC_HEADER_PREFIX = "x-bc-"

_CARD_SELECTORS: tuple[str, ...] = (
    "article.card",
    "[data-product-id]",
    ".productGrid .product",
    ".product-item",
)

_TITLE_SEL = ".card-title a, .productView-title, [data-product-title], h4, h3"
_PRICE_SEL = '.price--withTax, .price--withoutTax, .price, [data-product-price]'


def _has_bc_response_header(headers: Any) -> bool:
    """True if the response carries any BigCommerce ``x-bc-*`` header."""
    return any(name.lower().startswith(_BC_HEADER_PREFIX) for name in headers)


def detect_bigcommerce(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a BigCommerce store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_bigcommerce error for %s: %s", url, exc)
        return False
    if _has_bc_response_header(resp.headers):
        logger.info("BigCommerce detected via response header at %s", url)
        return True
    if any(marker in resp.text for marker in _DETECT_HTML_MARKERS):
        logger.info("BigCommerce detected via page source at %s", url)
        return True
    return False


def _resolve_image_from_item(item: Dict[str, Any]) -> str:
    """Pick the best primary image URL from a BigCommerce product dict."""
    primary = item.get("primary_image") or {}
    if isinstance(primary, dict):
        for key in ("url_standard", "url"):
            value = primary.get(key)
            if isinstance(value, str) and value:
                return value
    images = item.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get("url_standard", "")
    return ""


def _resolve_product_url(item: Dict[str, Any], base_url: str) -> str:
    """Compose the absolute product URL from a BigCommerce custom_url field."""
    custom_url = item.get("custom_url") or {}
    if isinstance(custom_url, dict):
        path = custom_url.get("url", "")
    else:
        path = str(custom_url)
    return urljoin(base_url, path) if path else ""


def _is_in_stock(availability: Any) -> bool:
    """True when the BigCommerce availability marker is not disabled / preorder."""
    if not isinstance(availability, str) or not availability:
        return True
    return availability.lower() not in ("disabled", "preorder")


def _parse_product(item: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Normalize a BigCommerce API product dict into the standard shape."""
    price_raw = (
        item.get("calculated_price")
        or item.get("sale_price")
        or item.get("price")
        or 0
    )
    return {
        "title": item.get("name", ""),
        "price": parse_price_string(price_raw),
        "url": _resolve_product_url(item, base_url),
        "image": _resolve_image_from_item(item),
        "in_stock": _is_in_stock(item.get("availability", "")),
        "sku": str(item.get("sku", "")),
    }


def _fetch_json(url: str) -> Optional[Any]:
    """GET a URL and decode JSON; None on transport / decode failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8)
    except REQUESTS_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"bc GET {url}", exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except (ValueError, TypeError) as exc:
        log_and_swallow(logger, logging.DEBUG, f"bc json decode {url}", exc)
        return None


def _try_catalog_endpoint(base: str, limit: int) -> List[Dict[str, Any]]:
    """Probe the open catalog endpoint and normalize results."""
    catalog_url = f"{base}/api/storefront/catalog/products?limit={limit}"
    payload = _fetch_json(catalog_url)
    if not isinstance(payload, list):
        return []
    results = [_parse_product(item, base) for item in payload[:limit]]
    if results:
        logger.info("BigCommerce catalog API returned %d products from %s", len(results), base)
    return results


def _try_search_endpoint(base: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Probe the storefront search endpoint and normalize results."""
    search_url = f"{base}/api/storefront/search?term={quote(query)}&limit={limit}"
    payload = _fetch_json(search_url)
    items = payload.get("products", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    results = [_parse_product(item, base) for item in items[:limit]]
    if results:
        logger.info("BigCommerce search API returned %d products from %s", len(results), base)
    return results


def _scrape_bc_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try BigCommerce Storefront API endpoints (works on stores with open CORS)."""
    base = url.rstrip("/")
    catalog_results = _try_catalog_endpoint(base, limit)
    if catalog_results:
        return catalog_results
    if query:
        return _try_search_endpoint(base, query, limit)
    return []


def _select_first_cards(soup: BeautifulSoup) -> List[Any]:
    """Return the first non-empty list of product cards."""
    for selector in _CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            return cards
    return []


def _card_to_product(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Normalize a soup product card; None when title cannot be read."""
    title_el = card.select_one(_TITLE_SEL)
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None
    price_el = card.select_one(_PRICE_SEL)
    price = price_el.get_text(strip=True) if price_el else ""
    link_el = card.select_one("a[href]")
    product_url = urljoin(base_url, link_el["href"]) if link_el else ""
    img_el = card.select_one("img[src]")
    image = img_el.get("src", "") if img_el else ""
    return {
        "title": title,
        "price": price,
        "url": product_url,
        "image": image,
        "in_stock": True,
        "sku": "",
    }


def _build_search_target(url: str, query: Optional[str]) -> str:
    """Compose the storefront URL for Playwright navigation."""
    target = url.rstrip("/")
    if query:
        return f"{target}/search?q={quote(query)}"
    return target


async def close_browser_safely(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing narrow shutdown failures."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "bc browser close", exc)


async def _pw_fetch_cards(page: Any, params: StoreScrapeParams) -> List[Dict[str, Any]]:
    """Drive the Playwright page through navigation + soup-card extraction."""
    target = _build_search_target(params.url, params.query)
    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        logger.warning("BigCommerce Playwright timeout for %s: %s", params.url, exc)
        return []
    await page.wait_for_timeout(2000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_cards(soup)
    results: List[Dict[str, Any]] = []
    for card in cards[: params.limit]:
        try:
            product = _card_to_product(card, params.url)
        except CARD_PROBE_EXCEPTIONS as exc:
            log_and_swallow(logger, logging.DEBUG, "bc card probe", exc)
            continue
        if product:
            results.append(product)
    return results


async def _scrape_bc_playwright(
    url: str, query: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate the storefront and scrape the product grid."""
    params = StoreScrapeParams(url=url, query=query, limit=limit)
    browser = None
    ctx = None
    page = None
    results: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            results = await _pw_fetch_cards(page, params)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("BigCommerce Playwright fallback failed for %s: %s", url, exc)
        return None  # explicit recovery: non-fatal: BROWSER_OR_HTTP_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_safely(browser)
    return results


def scrape_bigcommerce_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Scrape products from BigCommerce: Storefront API -> Playwright fallback."""
    results = _scrape_bc_api(url, query, limit)
    if results:
        return results
    logger.info("BigCommerce API yielded no results for %s, trying Playwright", url)
    return _scrape_bc_playwright(url, query, limit)
