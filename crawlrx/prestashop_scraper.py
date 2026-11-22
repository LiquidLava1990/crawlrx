"""prestashop_scraper.py -- PrestaShop product scraper.

Functions:
  detect_prestashop(url) -> bool
  scrape_prestashop_products(url, query, limit) -> List[Dict]

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

logger = logging.getLogger("crawlrx.prestashop")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException, requests.ConnectionError, requests.Timeout,
    requests.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError,
)

_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PlaywrightTimeoutError, OSError, RuntimeError, ValueError,
    AttributeError, TypeError, KeyError,
)


def detect_prestashop(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be a PrestaShop store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        html = resp.text
    except _REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_prestashop error for %s: %s", url, type(exc).__name__)
        return False
    if (
        "prestashop" in html.lower()
        or "/themes/default-bootstrap/" in html
        or "PrestaShop" in html
    ):
        logger.info("PrestaShop detected via page source at %s", url)
        return True
    return False


def _parse_price_text(text: str) -> str:
    """Strip whitespace and collapse spaces in a price string."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip())


def _coerce_ws_name(name: Any) -> str:
    """Resolve the various PrestaShop name shapes (list of dicts, str, int)."""
    if isinstance(name, list) and name:
        first = name[0]
        if isinstance(first, dict):
            return first.get("value", "")
        return str(first)
    return str(name)


def _coerce_ws_price(price_raw: Any) -> str:
    """Format a webservice price as a 2-dp string when numeric."""
    try:
        return "%.2f" % float(price_raw)
    except (TypeError, ValueError):
        return str(price_raw)


def _ws_record(base: str, item: dict) -> dict[str, str]:
    """Render one webservice product item into the unified shape."""
    return {
        "title": _coerce_ws_name(item.get("name", "")),
        "price": _coerce_ws_price(item.get("price", "")),
        "url": "%s/index.php?id_product=%s&controller=product" % (
            base, item.get("id", ""),
        ),
        "image": "",
    }


def _extract_product_list(data: Any) -> list[dict]:
    """Pull the list of product dicts from a PrestaShop JSON body."""
    if isinstance(data, dict):
        products = data.get("products", data)
    else:
        products = data
    return products if isinstance(products, list) else []


def _scrape_ps_webservice(url: str, limit: int) -> List[Dict[str, Any]]:
    """Try the PrestaShop open webservice (no API key required on some stores)."""
    base = url.rstrip("/")
    api_url = "%s/api/products?output_format=JSON&limit=%d&display=full" % (base, limit)
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=8)
        if resp.status_code != 200:
            return []
        products = _extract_product_list(resp.json())
    except _REQUESTS_EXCEPTIONS as exc:
        logger.debug("PrestaShop webservice failed for %s: %s", base, type(exc).__name__)
        return []
    results = [_ws_record(base, item) for item in products[:limit]]
    if results:
        logger.info("PrestaShop webservice returned %d products from %s", len(results), base)
    return results


def _ajax_image(item: dict) -> str:
    """Resolve the cover-image URL out of a PrestaShop AJAX search item."""
    cover = item.get("cover")
    if not isinstance(cover, dict):
        return ""
    by_size = cover.get("bySize")
    if not isinstance(by_size, dict):
        return ""
    first = next(iter(by_size.values()), {})
    return first.get("url", "") if isinstance(first, dict) else ""


def _ajax_record(item: dict) -> dict[str, str]:
    """Render one AJAX-search item into the unified product shape."""
    return {
        "title": str(item.get("name", item.get("product_name", ""))),
        "price": str(item.get("price_tax_exc", item.get("price", ""))),
        "url": item.get("link", item.get("url", "")),
        "image": _ajax_image(item),
    }


def _scrape_ps_ajax_search(url: str, query: str, limit: int) -> List[Dict[str, Any]]:
    """Try the PrestaShop AJAX search endpoint (JSON on many themes)."""
    base = url.rstrip("/")
    search_url = "%s/index.php?controller=search&s=%s&ajax=true" % (base, quote(query))
    try:
        resp = requests.get(search_url, headers=_HEADERS, timeout=8)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or not ctype.startswith("application/json"):
            return []
        products = _extract_product_list(resp.json())
    except _REQUESTS_EXCEPTIONS as exc:
        logger.debug("PrestaShop AJAX search failed for %s: %s", base, type(exc).__name__)
        return []
    results = [_ajax_record(item) for item in products[:limit]]
    if results:
        logger.info("PrestaShop AJAX search returned %d products from %s", len(results), base)
    return results


# ---------------------------------------------------------------------------
# Playwright fallback
# ---------------------------------------------------------------------------

_PS_CARD_SELECTORS: tuple[str, ...] = (
    ".product-miniature",
    ".product_list .product-container",
    "article.product-item",
    ".ajax_block_product",
)

_PS_TITLE_SELECTOR = ".product-title a, .product-name a, h2 a, h3 a, .product_name"
_PS_PRICE_SELECTOR = ".price, .product-price, span[itemprop='price'], .product_price"


def _select_first_nonempty(soup: BeautifulSoup, selectors: tuple[str, ...]) -> list:
    """Return the first non-empty selector match list under ``soup``."""
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            return cards
    return []


def _ps_card_record(card: Any, base_url: str) -> dict[str, str] | None:
    """Render one PrestaShop product card into the unified shape."""
    title_el = card.select_one(_PS_TITLE_SELECTOR)
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None
    price_el = card.select_one(_PS_PRICE_SELECTOR)
    price = _parse_price_text(price_el.get_text()) if price_el else ""
    link_el = title_el if title_el else card.select_one("a[href]")
    product_url = urljoin(base_url, link_el.get("href", "")) if link_el else ""
    img_el = card.select_one("img[src]")
    image = img_el.get("src", "") if img_el else ""
    return {"title": title, "price": price, "url": product_url, "image": image}


def _ps_target_url(url: str, query: Optional[str]) -> str:
    """Build the PrestaShop URL Playwright should navigate to."""
    target = url.rstrip("/")
    if query:
        return "%s/search?controller=search&s=%s" % (target, quote(query))
    return target


async def _ps_collect_results(page: Any, url: str, limit: int) -> list[dict]:
    """Pull HTML, parse product cards, return up to ``limit`` records."""
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_nonempty(soup, _PS_CARD_SELECTORS)
    out: list[dict] = []
    for card in cards[:limit]:
        record = _ps_card_record(card, url)
        if record is not None:
            out.append(record)
    return out


async def _scrape_ps_playwright(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate storefront and scrape product cards."""
    results: list[dict] = []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            try:
                target = _ps_target_url(url, query)
                await page.goto(target, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                results = await _ps_collect_results(page, url, limit)
            except PlaywrightTimeoutError as exc:
                logger.warning("PrestaShop Playwright timeout for %s: %s", url, exc)
                return None  # explicit recovery: non-fatal: PlaywrightTimeoutError is recorded but does not abort the caller
            finally:
                await close_stealth_context(browser, ctx, page)
    except _PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "PrestaShop Playwright fallback failed for %s: %s",
            url, type(exc).__name__,
        )
        return None  # explicit recovery: non-fatal: _PLAYWRIGHT_EXCEPTIONS is recorded but does not abort the caller
    return results


def scrape_prestashop_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape products from a PrestaShop store (webservice -> ajax -> Playwright)."""
    results = _scrape_ps_webservice(url, limit)
    if results:
        return results[:limit]
    if query:
        results = _scrape_ps_ajax_search(url, query, limit)
        if results:
            return results
    logger.info("PrestaShop HTTP methods yielded no results for %s, trying Playwright", url)
    return _scrape_ps_playwright(url, query, limit)
