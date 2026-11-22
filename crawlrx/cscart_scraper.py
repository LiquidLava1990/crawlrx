"""cscart_scraper.py -- CS-Cart product scraper
Functions:
  detect_cscart(url) -> bool
  scrape_cscart_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9232.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    text_contains_any,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.cscart")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_DETECTION_MARKERS_CI: tuple[str, ...] = ("cs-cart.com",)
_DETECTION_MARKERS: tuple[str, ...] = ("tygh_", "/skins/basic/", "Tygh")

_CARD_SELECTORS: tuple[str, ...] = (
    ".ty-grid-list__item",
    ".ty-product-list",
    ".product-item",
    ".ty-column",
)


def detect_cscart(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a CS-Cart store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_cscart error for %s: %s: %s", url, type(exc).__name__, exc)
        return False
    html = resp.text
    if text_contains_any(html, _DETECTION_MARKERS):
        logger.info("CS-Cart detected via page source at %s", url)
        return True
    if any(marker in html.lower() for marker in _DETECTION_MARKERS_CI):
        logger.info("CS-Cart detected via case-insensitive marker at %s", url)
        return True
    return False


def _parse_price(value: Any) -> str:
    """Parse and format price value."""
    if value is None:
        return ""
    try:
        return "%.2f" % float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return str(value)


def _api_image_path(item: dict) -> str:
    """Pull `main_pair.detailed.image_path` if present."""
    main_pair = item.get("main_pair")
    if not isinstance(main_pair, dict):
        return ""
    detailed = main_pair.get("detailed")
    if not isinstance(detailed, dict):
        return ""
    return detailed.get("image_path", "")


def _build_api_product(item: dict, base: str) -> Dict[str, Any]:
    """Map one CS-Cart REST item to standard dict."""
    product_id = item.get("product_id", item.get("id", ""))
    product_url = ""
    if isinstance(product_id, (int, str)) and product_id:
        product_url = (
            f"{base}/index.php?dispatch=products.view&product_id={product_id}"
        )
    return {
        "title": item.get("product", item.get("name", item.get("title", ""))),
        "price": _parse_price(item.get("price", "")),
        "url": product_url,
        "image": _api_image_path(item),
    }


def _api_items(data: Any) -> list[Any]:
    """Extract items list from list/dict API payload."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("products", data.get("data", []))
    return []


def _scrape_cscart_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try CS-Cart REST API."""
    base = url.rstrip("/")
    api_url = f"{base}/api/products?items_per_page={limit}"
    if query:
        api_url += f"&q={quote(query)}"
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=8)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.debug("CS-Cart REST API failed for %s: %s: %s", base, type(exc).__name__, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.debug("CS-Cart REST JSON decode failed for %s: %s", base, exc)
        return []
    results = [_build_api_product(item, base) for item in _api_items(data)[:limit]]
    if results:
        logger.info("CS-Cart REST API returned %d products from %s", len(results), base)
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
        card,
        ".product-title, .ty-grid-list__item-name a, h2 a, h3 a, .item-title a",
    )
    if not title:
        return None
    href = _select_card_attr(card, "a[href]", "href")
    return {
        "title": title,
        "price": _select_card_text(card, ".ty-price, .price, .product-price"),
        "url": urljoin(base_url, href) if href else "",
        "image": _select_card_attr(card, "img[src]", "src"),
    }


def _select_first_match(soup: BeautifulSoup, selectors: tuple[str, ...]) -> list[Any]:
    """Return first non-empty selector match."""
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            return list(cards)
    return []


def _resolve_pw_target(url: str, query: Optional[str]) -> str:
    """Pick the URL to navigate to."""
    base = url.rstrip("/")
    if query:
        return f"{base}/index.php?dispatch=products.search&q={quote(query)}"
    return f"{base}/index.php?dispatch=categories.catalog"


async def _do_pw_scrape(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Run the actual product walk inside an open page."""
    target = _resolve_pw_target(url, query)
    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        logger.warning("CS-Cart Playwright timeout for %s: %s", url, exc)
        return []
    await page.wait_for_timeout(2000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_match(soup, _CARD_SELECTORS)
    results: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        try:
            built = _build_card_product(card, url)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("CS-Cart card parse error: %s", exc)
            continue
        if built is not None:
            results.append(built)
    return results


async def _scrape_cscart_playwright(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate storefront and scrape product grid."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await _do_pw_scrape(page, url, query, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("CS-Cart Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        if browser:
            try:
                await browser.close()
            except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
                logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously, returning [] when a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return []
    return asyncio.run(coro)


def scrape_cscart_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape CS-Cart products: REST API + Playwright DOM fallback."""
    results = _scrape_cscart_api(url, query, limit)
    if results:
        return results
    logger.info("CS-Cart API yielded no results for %s, trying Playwright", url)
    return _run_async(_scrape_cscart_playwright(url, query, limit))
