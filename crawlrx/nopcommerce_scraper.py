"""nopcommerce_scraper.py -- NopCommerce product scraper
Functions:
  detect_nopcommerce(url) -> bool
  scrape_nopcommerce_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9232.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.nopcommerce")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}



def detect_nopcommerce(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a NopCommerce store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        html = resp.text
        if (
            "nopCommerce" in html
            or "/Themes/DefaultClean/" in html
            or 'generator" content="nopCommerce' in html.lower()
            or 'name="generator" content="nopCommerce' in html
        ):
            logger.info("NopCommerce detected via page source at %s", url)
            return True
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("detect_nopcommerce error for %s: %s", url, exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return False


def _parse_price(value: Any) -> str:
    """Parse and format price value."""
    if value is None:
        return ""
    try:
        return "%.2f" % float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return str(value)


_API_PRODUCT_KEYS: tuple[str, ...] = ("products", "data")
_CARD_SELECTORS: tuple[str, ...] = (
    ".product-item",
    ".item-box",
    ".product-grid .item",
    ".product-list .item",
)


def _api_items(data: Any) -> list[Any]:
    """Extract items list from list or dict API payload."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _API_PRODUCT_KEYS:
            if key in data:
                return data[key]
    return []


def _build_api_product(item: dict, base: str) -> Dict[str, Any]:
    """Map one NopCommerce REST item to standard dict."""
    product_id = item.get("id", item.get("product_id", ""))
    product_url = ""
    if isinstance(product_id, (int, str)) and product_id:
        product_url = f"{base}/{item.get('se_name', f'p/{product_id}')}"
    return {
        "title": item.get("name", item.get("title", "")),
        "price": _parse_price(item.get("price", "")),
        "url": product_url,
        "image": item.get("image", item.get("picture", "")),
        "sku": str(item.get("sku", "")),
    }


def _scrape_nopcommerce_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try NopCommerce REST API (requires API plugin enabled)."""
    base = url.rstrip("/")
    api_url = f"{base}/api/products?limit={limit}"
    if query:
        api_url += f"&q={quote(query)}"
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=8)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("NopCommerce REST API failed for %s: %s", base, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.debug("NopCommerce REST JSON decode failed for %s: %s", base, exc)
        return []
    results = [_build_api_product(item, base) for item in _api_items(data)[:limit]]
    if results:
        logger.info("NopCommerce REST API returned %d products from %s", len(results), base)
    return results


def _select_card_text(card: Any, selector: str) -> str:
    """Pull the first matching selector's text, or empty string."""
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _select_card_attr(card: Any, selector: str, attr: str) -> str:
    """Pull the first matching selector's attribute, or empty string."""
    el = card.select_one(selector)
    return el.get(attr, "") if el else ""


def _build_card_product(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Map one DOM product card to standard dict; None if no title."""
    title = _select_card_text(
        card, ".product-title, h2 a, h3 a, .title a, a[href*='/p/']",
    )
    if not title:
        return None
    href = _select_card_attr(card, "a[href]", "href")
    return {
        "title": title,
        "price": _select_card_text(card, ".price, .actual-price, .product-price"),
        "url": urljoin(base_url, href) if href else "",
        "image": _select_card_attr(card, "img[src]", "src"),
        "sku": _select_card_attr(card, "[data-sku], .sku", "data-sku"),
    }


def _select_first_match(soup: BeautifulSoup, selectors: tuple[str, ...]) -> list[Any]:
    """Return first non-empty selector match list."""
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            return list(cards)
    return []


def _resolve_pw_target(url: str, query: Optional[str]) -> str:
    """Pick the storefront URL to navigate to."""
    base = url.rstrip("/")
    if query:
        return f"{base}/search?q={quote(query)}"
    return f"{base}/products"


async def _do_pw_scrape(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Run the actual product walk inside an open page."""
    target = _resolve_pw_target(url, query)
    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        logger.warning("NopCommerce Playwright timeout for %s: %s", url, exc)
        return []
    await page.wait_for_timeout(2000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_match(soup, _CARD_SELECTORS)
    results: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        built = _build_card_product(card, url)
        if built is not None:
            results.append(built)
    return results


async def _scrape_nopcommerce_playwright(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate storefront and scrape product grid."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await _do_pw_scrape(page, url, query, limit)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("NopCommerce Playwright fallback failed for %s: %s", url, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        if browser:
            try:
                await browser.close()
            except BROWSER_SCRAPE_EXCEPTIONS as exc:
                logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously, returning [] when a loop is running."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(coro)


def scrape_nopcommerce_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape NopCommerce products: REST API + Playwright DOM fallback."""
    results = _scrape_nopcommerce_api(url, query, limit)
    if results:
        return results
    logger.info("NopCommerce API yielded no results for %s, trying Playwright", url)
    return _run_async(_scrape_nopcommerce_playwright(url, query, limit))
