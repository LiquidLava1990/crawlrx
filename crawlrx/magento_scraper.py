"""magento_scraper.py -- Magento product scraper
Functions:
  detect_magento(url) -> bool
  scrape_magento_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9236.
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

logger = logging.getLogger("crawlrx.magento")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}



_MAGENTO_HTML_MARKERS = (
    "Mage.Cookies",
    "/skin/frontend/",
    "mage/translate",
    "Magento_Ui",
)


def _has_magento_cookie(resp_cookies: Any) -> bool:
    """True if any response cookie name starts with the Magento prefix."""
    return any(c.name.startswith("MAGE_") for c in resp_cookies)


def _has_magento_html_marker(html: str) -> bool:
    """True if the response HTML contains any known Magento source marker."""
    return any(marker in html for marker in _MAGENTO_HTML_MARKERS)


def detect_magento(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a Magento store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("detect_magento error for %s: %s", url, exc)
        # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; treat as not-Magento
        return False
    if _has_magento_cookie(resp.cookies):
        logger.info("Magento detected via cookie at %s", url)
        return True
    if _has_magento_html_marker(resp.text):
        logger.info("Magento detected via page source at %s", url)
        return True
    return False


def _build_magento2_search_url(base: str, query: Optional[str], limit: int) -> str:
    """Build Magento 2 REST API search URL."""
    params = (
        "searchCriteria[pageSize]=%d"
        "&searchCriteria[currentPage]=1"
    ) % limit

    if query:
        params += (
            "&searchCriteria[filterGroups][0][filters][0][field]=name"
            "&searchCriteria[filterGroups][0][filters][0][value]=%%%s%%"
            "&searchCriteria[filterGroups][0][filters][0][conditionType]=like"
        ) % quote(query)

    return "%s/rest/V1/products?%s" % (base, params)


def _parse_magento_item(item: Dict[str, Any], base: str) -> Dict[str, Any]:
    """Normalize a Magento 2 REST API product dict."""
    price = ""
    price_raw = item.get("price")
    if price_raw is not None:
        try:
            price = "%.2f" % float(price_raw)
        except (TypeError, ValueError):
            price = str(price_raw)

    # Resolve product URL from url_key custom attribute
    url_key = ""
    for attr in item.get("custom_attributes", []):
        if attr.get("attribute_code") == "url_key":
            url_key = attr.get("value", "")
            break

    product_id = item.get("id", "")
    if url_key:
        product_url = "%s/%s.html" % (base, url_key)
    elif product_id:
        product_url = "%s/catalog/product/view/id/%s" % (base, product_id)
    else:
        product_url = base

    # Resolve image from media_gallery_entries
    image = ""
    entries = item.get("media_gallery_entries", [])
    if entries:
        file_path = entries[0].get("file", "")
        if file_path:
            image = "%s/pub/media/catalog/product%s" % (base, file_path)

    return {
        "title": item.get("name", ""),
        "price": price,
        "url": product_url,
        "image": image,
        "sku": str(item.get("sku", "")),
    }


def _scrape_magento_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try Magento 2 REST API (no auth required on public catalogs)."""
    base = url.rstrip("/")
    api_url = _build_magento2_search_url(base, query, limit)
    results: List[Dict[str, Any]] = []
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            for item in items[:limit]:
                results.append(_parse_magento_item(item, base))
            if results:
                logger.info("Magento REST API returned %d products from %s", len(results), base)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Magento REST API failed for %s: %s", base, exc)
    return results


_CARD_SELECTORS = (
    ".product-item",
    ".product-items li",
    "[data-product-id]",
    ".item.product",
)


def _select_first_match(soup: BeautifulSoup, selectors: tuple) -> list:
    """Return the first non-empty selector match list across ``selectors``."""
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            return cards
    return []


def _parse_card(card, base_url: str) -> Optional[Dict[str, Any]]:
    """Extract a single product card; ``None`` when title is missing."""
    title_el = card.select_one(".product-item-name a, .product-item-link, h2 a, h3 a")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None
    price_el = card.select_one(
        ".price-wrapper .price, .price, [data-price-type='finalPrice'] .price"
    )
    price = price_el.get_text(strip=True) if price_el else ""
    link_el = title_el if title_el else card.select_one("a[href]")
    product_url = urljoin(base_url, link_el.get("href", "")) if link_el else ""
    img_el = card.select_one("img.product-image-photo, img[src]")
    image = img_el.get("src", "") if img_el else ""
    sku = card.get("data-product-sku") or ""
    return {
        "title": title,
        "price": price,
        "url": product_url,
        "image": image,
        "sku": str(sku),
    }


def _build_magento_search_url(url: str, query: Optional[str]) -> str:
    """Resolve storefront search URL from base + optional query."""
    target = url.rstrip("/")
    if query:
        return "%s/catalogsearch/result/?q=%s" % (target, quote(query))
    return target


async def _extract_magento_grid(page, url: str, limit: int) -> List[Dict[str, Any]]:
    """Read rendered HTML, return up to ``limit`` parsed product dicts."""
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_match(soup, _CARD_SELECTORS)
    out: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        parsed = _parse_card(card, url)
        if parsed:
            out.append(parsed)
    return out


async def _drive_magento_grid(page: Any, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Navigate the storefront and extract the grid; [] on Playwright timeout."""
    try:
        target = _build_magento_search_url(url, query)
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        return await _extract_magento_grid(page, url, limit)
    except PlaywrightTimeoutError as exc:
        logger.warning("Magento Playwright timeout for %s: %s", url, exc)
        # explicit recovery: PlaywrightTimeoutError non-fatal; caller treats as empty
        return []


async def _scrape_magento_playwright(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate the storefront and scrape the product grid."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            try:
                return await _drive_magento_grid(page, url, query, limit)
            finally:
                await close_stealth_context(browser, ctx, page)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Magento Playwright fallback failed for %s: %s", url, exc)
        # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; caller treats as empty
        return []


def scrape_magento_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """REST API first, Playwright fallback. Returns {title,price,url,image,sku} dicts."""
    results = _scrape_magento_api(url, query, limit)
    if results:
        return results

    logger.info("Magento REST API yielded no results for %s, trying Playwright", url)
    return _scrape_magento_playwright(url, query, limit)
