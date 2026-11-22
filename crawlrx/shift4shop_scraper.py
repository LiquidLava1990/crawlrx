"""shift4shop_scraper.py -- Shift4Shop (3dcart) product scraper.

Functions:
  detect_shift4shop(url) -> bool
  scrape_shift4shop_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9232.
"""

import json
import logging
import re
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

logger = logging.getLogger("crawlrx.shift4shop")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_THEME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'/v/vspfiles/themes/([^/"]+)/', re.IGNORECASE),
    re.compile(r'"theme"[:\s]*"([^"]+)"', re.IGNORECASE),
    re.compile(r'theme["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.IGNORECASE),
)

_DETECT_MARKERS: tuple[str, ...] = ("3dcart.net", "shift4shop.com", "/v/vspfiles/")

_CARD_SELECTORS: tuple[str, ...] = (
    ".product-item",
    ".v-product",
    ".product",
    ".v65-productDisplay",
    "[id^='product_']",
)

_TITLE_SEL = ".product-name, .v-product-name, h3 a, h2 a, .name a, a[href*='/p/']"
_PRICE_SEL = ".product-price, .v-product-price, .price, .yourprice"


def detect_shift4shop(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a Shift4Shop (3dcart) store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_shift4shop error for %s: %s", url, exc)
        return False
    html = resp.text
    if any(marker in html for marker in _DETECT_MARKERS):
        logger.info("Shift4Shop detected via page source at %s", url)
        return True
    return False


def _extract_theme_from_html(html: str) -> Optional[str]:
    """Extract theme name from Shift4Shop page source."""
    for pattern in _THEME_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _to_product_dict(item: Dict[str, Any], base: str, link_keys: tuple[str, ...]) -> Dict[str, Any]:
    """Normalize a Shift4Shop API item into the standard product shape."""
    title = item.get("name") or item.get("title") or ""
    href = ""
    for key in link_keys:
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate:
            href = candidate
            break
    return {
        "title": title,
        "price": parse_price_string(item.get("price", "")),
        "url": urljoin(base, href),
        "image": item.get("image", item.get("thumbnail", "")),
    }


def _decode_products_payload(data: Any) -> List[Dict[str, Any]]:
    """Coerce a JSON response into a list of product dicts."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("products", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _fetch_json(url: str, timeout: int = 8) -> Optional[Any]:
    """GET a URL and decode JSON, returning None on any narrow failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    except REQUESTS_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"shift4shop GET {url}", exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        log_and_swallow(logger, logging.DEBUG, f"shift4shop json decode {url}", exc)
        return None


def _fetch_storefront_html(base: str) -> Optional[str]:
    """GET the storefront HTML, returning None on any narrow transport failure."""
    try:
        resp = requests.get(base, headers=_HEADERS, timeout=8)
    except REQUESTS_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"shift4shop storefront {base}", exc)
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def _scrape_shift4shop_json(url: str, limit: int) -> List[Dict[str, Any]]:
    """Try Shift4Shop products.json endpoint."""
    base = url.rstrip("/")
    html = _fetch_storefront_html(base)
    if not html:
        return []
    theme = _extract_theme_from_html(html)
    if not theme:
        return []
    json_url = f"{base}/v/vspfiles/themes/{theme}/data/json/products.json"
    payload = _fetch_json(json_url)
    if payload is None:
        return []
    raw_products = _decode_products_payload(payload)
    results = [
        _to_product_dict(item, base, ("url", "link"))
        for item in raw_products[:limit]
    ]
    if results:
        logger.info("Shift4Shop products.json returned %d products from %s", len(results), base)
    return results


def _build_api_url(base: str, query: Optional[str], limit: int) -> str:
    """Compose the Shift4Shop REST API URL with an optional search."""
    api_url = f"{base}/api/v1/products?limit={limit}"
    if query:
        api_url = f"{api_url}&search={quote(query)}"
    return api_url


def _scrape_shift4shop_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try Shift4Shop REST API."""
    base = url.rstrip("/")
    payload = _fetch_json(_build_api_url(base, query, limit))
    if payload is None:
        return []
    raw_products = _decode_products_payload(payload)
    results = [
        _to_product_dict(item, base, ("url", "permalink"))
        for item in raw_products[:limit]
    ]
    if results:
        logger.info("Shift4Shop REST API returned %d products from %s", len(results), base)
    return results


def _select_first_cards(soup: BeautifulSoup) -> List[Any]:
    """Return cards from the first selector in `_CARD_SELECTORS` that matches."""
    for selector in _CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            return cards
    return []


def _card_to_product(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Normalize a soup product card; None when the title cannot be read."""
    title_el = card.select_one(_TITLE_SEL)
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None
    price_el = card.select_one(_PRICE_SEL)
    price = price_el.get_text(strip=True) if price_el else ""
    link_el = card.select_one("a[href]")
    product_url = urljoin(base_url, link_el.get("href", "")) if link_el else ""
    img_el = card.select_one("img[src]")
    image = img_el.get("src", "") if img_el else ""
    return {
        "title": title,
        "price": price,
        "url": product_url,
        "image": image,
    }


def _build_search_target(url: str, query: Optional[str]) -> str:
    """Compose the storefront URL for Playwright navigation."""
    target = url.rstrip("/")
    if query:
        return f"{target}/search.asp?keyword={quote(query)}"
    return target


async def close_browser_safely(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing narrow shutdown failures."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "shift4shop browser close", exc)


async def _pw_fetch_cards(page: Any, params: StoreScrapeParams) -> List[Dict[str, Any]]:
    """Drive the Playwright page through navigation + soup-card extraction."""
    target = _build_search_target(params.url, params.query)
    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        logger.warning("Shift4Shop Playwright timeout for %s: %s", params.url, exc)
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
            log_and_swallow(logger, logging.DEBUG, "shift4shop card probe", exc)
            continue
        if product:
            results.append(product)
    return results


async def _scrape_shift4shop_playwright(
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
        logger.error("Shift4Shop Playwright fallback failed for %s: %s", url, exc)
        return None  # explicit recovery: non-fatal: BROWSER_OR_HTTP_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_safely(browser)
    return results


def scrape_shift4shop_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Scrape products from Shift4Shop (3dcart): json -> REST -> Playwright fallback."""
    results = _scrape_shift4shop_json(url, limit)
    if results:
        return results

    results = _scrape_shift4shop_api(url, query, limit)
    if results:
        return results

    logger.info("Shift4Shop HTTP methods yielded no results for %s, trying Playwright", url)
    return _scrape_shift4shop_playwright(url, query, limit)
