"""hybris_scraper.py -- SAP Hybris (SAP Commerce Cloud) product scraper
Functions:
  detect_hybris(url, headers, html) -> bool
  scrape_hybris_products(url) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9234.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel

from ._cms_helpers import (
    CARD_EXTRACTION_EXCEPTIONS,
    CMS_PLAYWRIGHT_EXCEPTIONS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.hybris")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_CURRENCY_BY_SYMBOL: dict[str, str] = {"€": "EUR", "£": "GBP", "$": "USD"}

_HTML_LOWER_MARKERS: tuple[tuple[str, str], ...] = (
    ("hybris", "'hybris' in HTML"),
    ("yacceleratorstorefront", "'yacceleratorstorefront' in HTML"),
    ('name="generator" content="sap commerce', "SAP Commerce meta tag"),
    ('property="og:site_name" content="hybris', "og:site_name meta tag"),
)

_CARD_SELECTORS: tuple[str, ...] = (
    ".product-item",
    ".product-list-item",
    ".product-grid .item",
    ".product-tile",
    "[data-product-code]",
    ".yCmsComponent .product",
)


def _detect_html(url: str, html: str) -> bool:
    """Detect Hybris by HTML markers."""
    html_lower = html.lower()
    for marker, descr in _HTML_LOWER_MARKERS:
        if marker in html_lower:
            logger.info("Hybris detected via %s at %s", descr, url)
            return True
    if "acc." in html_lower or "acc =" in html or "window.ACC" in html:
        logger.info("Hybris detected via ACC. JS namespace at %s", url)
        return True
    return False


def _detect_headers(url: str, headers: Dict[str, str]) -> bool:
    """Detect Hybris by response headers."""
    headers_str = str(headers).lower()
    if "hybris" in headers_str:
        logger.info("Hybris detected via 'hybris' in headers at %s", url)
        return True
    if "jsessionid" in headers_str:
        logger.info("Hybris detected via JSESSIONID pattern at %s", url)
        return True
    for key, value in headers.items():
        if key.lower() != "set-cookie":
            continue
        lowered = value.lower()
        if "jsessionid" in lowered or "hybris" in lowered:
            logger.info("Hybris detected via cookie patterns at %s", url)
            return True
    return False


def _fetch_for_detection(url: str) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    """Fetch (headers, html) tuple for ad-hoc Hybris detection."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_hybris error for %s: %s: %s", url, type(exc).__name__, exc)
        return None, None
    return dict(resp.headers), resp.text


def detect_hybris(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    html: Optional[str] = None,
) -> bool:
    """Return True if the site appears to be a SAP Hybris/SAP Commerce store."""
    if headers is None and html is None:
        headers, html = _fetch_for_detection(url)
        if headers is None and html is None:
            return False
    if html and _detect_html(url, html):
        return True
    if headers and _detect_headers(url, headers):
        return True
    return False


def _extract_site_from_url(url: str) -> str:
    """Extract site identifier from URL for OCC API calls."""
    common_sites = ["electronics", "apparel", "apparel-uk", "powertools", "b2c", "b2b"]
    for site in common_sites:
        if site in url.lower():
            return site
    return "electronics"


def _coerce_price_str(value: Any) -> str:
    """Format value as 2-decimal string or fallback to str(value)."""
    if value is None:
        return ""
    try:
        return "%.2f" % float(value)
    except (TypeError, ValueError):
        return str(value)


def _parse_price_value(price_data: Any) -> tuple[str, str]:
    """Extract (price, currency) from Hybris price object."""
    if isinstance(price_data, dict):
        value = price_data.get("value", price_data.get("formattedValue", ""))
        currency_block = price_data.get("currency", {})
        if isinstance(currency_block, dict):
            currency = currency_block.get("iso", price_data.get("currencyIso", ""))
        else:
            currency = price_data.get("currencyIso", "")
        return _coerce_price_str(value), currency
    return _coerce_price_str(price_data), ""


def _parse_hybris_product(product: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Normalize a Hybris/OCC API product dict."""
    title = product.get("name", "")
    url_key = product.get("url", "")
    product_url = urljoin(base_url, url_key) if url_key else ""
    
    image = ""
    images = product.get("images", [])
    if images:
        image = images[0].get("url", images[0].get("src", ""))
    else:
        image_obj = product.get("image", {})
        if image_obj:
            image = image_obj.get("url", image_obj.get("src", ""))
    
    price, currency = _parse_price_value(product.get("price", {}))
    
    if not price and "priceRange" in product:
        min_price = product["priceRange"].get("minPrice", {})
        price, currency = _parse_price_value(min_price)
    
    in_stock = product.get("stock", {}).get("stockLevelStatus", "") == "inStock"
    if not in_stock and "available" in product:
        in_stock = bool(product.get("available"))
    
    sku = str(product.get("code", product.get("sku", "")))
    brand = product.get("brand", product.get("manufacturer", ""))
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    
    return {
        "title": title,
        "price": price,
        "currency": currency,
        "url": product_url,
        "image": image,
        "in_stock": in_stock,
        "sku": sku,
        "brand": str(brand),
    }


def _items_from_payload(data: Any) -> list[Any]:
    """Extract product list from list/dict payload shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("products", data.get("data", []))
    return []


def _hybris_api_call(
    api_url: str, params: Dict[str, Any], base: str, label: str, page_size: int,
) -> List[Dict[str, Any]]:
    """Generic Hybris JSON API caller -- shared by OCC and alt paths."""
    try:
        resp = requests.get(api_url, headers=_HEADERS, params=params, timeout=8)
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Hybris %s failed for %s: %s: %s", label, base, type(exc).__name__, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.debug("Hybris %s JSON decode failed for %s: %s", label, base, exc)
        return []
    items = _items_from_payload(data)
    results = [_parse_hybris_product(p, base) for p in items[:page_size]]
    if results:
        logger.info("Hybris %s returned %d products from %s", label, len(results), base)
    return results


def _scrape_hybris_occ_api(url: str, site: str, page_size: int = 20) -> List[Dict[str, Any]]:
    """Try SAP Hybris OCC REST API v2."""
    base = url.rstrip("/")
    return _hybris_api_call(
        f"{base}/rest/v2/{site}/products/search",
        {"query": "", "pageSize": page_size},
        base, "OCC API v2", page_size,
    )


def _scrape_hybris_alt_api(url: str, page_size: int = 20) -> List[Dict[str, Any]]:
    """Try alternative /api/products/search endpoint."""
    base = url.rstrip("/")
    return _hybris_api_call(
        f"{base}/api/products/search",
        {"query": "", "limit": page_size},
        base, "alt API", page_size,
    )


def _select_text(card: Any, selector: str) -> str:
    """Pull selected element's text or empty string."""
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _select_attr(card: Any, selector: str, attr: str) -> str:
    """Pull selected element's attribute or empty string."""
    el = card.select_one(selector)
    return el.get(attr, "") if el else ""


def _card_price_currency(card: Any) -> tuple[str, str]:
    """Pull (price, currency) from one DOM card."""
    price_text = _select_text(card, ".price, .product-price, [data-price], .current-price")
    if not price_text:
        return "", ""
    price = ""
    match = re.search(r"[\d,.]+", price_text.replace(",", ""))
    if match:
        try:
            price = "%.2f" % float(match.group())
        except (TypeError, ValueError):
            price = match.group()
    currency = ""
    cur_match = re.search(r"[€£$]", price_text)
    if cur_match:
        currency = _CURRENCY_BY_SYMBOL.get(cur_match.group(), "")
    return price, currency


def _card_image(card: Any) -> str:
    """Pull image URL from data-src or src."""
    img_el = card.select_one("img[src], img[data-src]")
    if not img_el:
        return ""
    return img_el.get("data-src") or img_el.get("src", "")


def _card_in_stock(card: Any) -> bool:
    """Detect in-stock state from class names + .stock element."""
    if "out-of-stock" in card.get("class", []):
        return False
    if "outofstock" in str(card).lower():
        return False
    stock_el = card.select_one(".stock, .availability")
    if stock_el:
        return "in" in stock_el.get_text(strip=True).lower()
    return True


def _card_sku(card: Any) -> str:
    """Pull SKU from data-product-code attr or .code child."""
    sku = card.get("data-product-code", "")
    if sku:
        return str(sku)
    code_el = card.select_one("[data-code], .code")
    if not code_el:
        return ""
    return code_el.get("data-code", code_el.get_text(strip=True))


def _card_url(card: Any, base_url: str) -> str:
    """Resolve product link or empty string."""
    href = _select_attr(card, "a[href]", "href")
    return urljoin(base_url, href) if href else ""


def _parse_pw_card(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Parse one DOM product card; None if no title."""
    title = _select_text(
        card,
        ".product-name a, .product-title, h2 a, h3 a, .name a, a[href*='/p/']",
    )
    if not title:
        return None
    price, currency = _card_price_currency(card)
    return {
        "title": title,
        "price": price,
        "currency": currency,
        "url": _card_url(card, base_url),
        "image": _card_image(card),
        "in_stock": _card_in_stock(card),
        "sku": str(_card_sku(card)),
        "brand": _select_text(card, ".brand, .manufacturer"),
    }


def _select_first_match(soup: BeautifulSoup, selectors: tuple[str, ...]) -> list[Any]:
    """Return first non-empty selector match."""
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            return list(cards)
    return []


async def _do_pw_scrape(page: Any, url: str, limit: int) -> List[Dict[str, Any]]:
    """Run the actual product walk inside an open page."""
    target = url.rstrip("/")
    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        logger.warning("Hybris Playwright timeout for %s: %s", url, exc)
        return []
    await page.wait_for_timeout(2000)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_match(soup, _CARD_SELECTORS)
    results: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        try:
            built = _parse_pw_card(card, url)
        except CARD_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Hybris card parse error: %s", exc)
            continue
        if built is not None:
            results.append(built)
    return results


async def _scrape_hybris_playwright(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate storefront and scrape product grid."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            return await _do_pw_scrape(page, url, limit)
    except CMS_PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error("Hybris Playwright fallback failed for %s: %s: %s", url, type(exc).__name__, exc)
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


def scrape_hybris_products(url: str) -> List[Dict[str, Any]]:
    """Scrape Hybris products: OCC API + alt API + Playwright fallback."""
    site = _extract_site_from_url(url)
    results = _scrape_hybris_occ_api(url, site, page_size=20)
    if results:
        return results
    logger.info("Hybris OCC API v2 yielded no results for %s, trying alt API", url)
    results = _scrape_hybris_alt_api(url, page_size=20)
    if results:
        return results
    logger.info("Hybris alt API yielded no results for %s, trying Playwright", url)
    return _run_async(_scrape_hybris_playwright(url, limit=20))

router = APIRouter(prefix="/scrape", tags=["scrapers"])


class HybrisRequest(BaseModel):
    url: str


@router.post("/hybris")
def scrape_hybris_endpoint(request: HybrisRequest) -> dict:
    """POST /scrape/hybris - scrape products from a SAP Hybris/Commerce Cloud store."""
    import time as _time
    t0 = _time.time()
    if not request.url or not request.url.strip():
        return {"success": True, "products": [], "elapsed_ms": 0}
    try:
        products = scrape_hybris_products(request.url)
        elapsed = int((_time.time() - t0) * 1000)
        return {"success": True, "products": products, "total": len(products), "elapsed_ms": elapsed}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("Hybris endpoint error: %s", e)
        elapsed = int((_time.time() - t0) * 1000)
        return {"success": False, "products": [], "elapsed_ms": elapsed, "error": str(e)}
