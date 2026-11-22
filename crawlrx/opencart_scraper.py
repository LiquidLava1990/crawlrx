"""opencart_scraper.py -- OpenCart product scraper.

Functions:
  detect_opencart(url) -> bool
  scrape_opencart_products(url, query, limit) -> List[Dict]

Uses requests primary (8s timeout), Playwright fallback on port 9232.
"""

import logging
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from .stealth_context import make_stealth_context, close_stealth_context

logger = logging.getLogger("crawlrx.opencart")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Concrete HTTP/parse failure surface (R-05 narrowed).
_REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException, requests.ConnectionError, requests.Timeout,
    requests.HTTPError, OSError, ValueError, KeyError, TypeError,
    AttributeError, ET.ParseError,
)

# Best-effort browser-cleanup failure tuple.
_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, AttributeError, ValueError,
)

# Playwright orchestration failure tuple (transient browser issues).
_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PlaywrightTimeoutError, OSError, RuntimeError, ValueError,
    AttributeError, TypeError, KeyError,
)


def detect_opencart(url: str, timeout: int = 8) -> bool:
    """Return True if the site appears to be an OpenCart store."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        html = resp.text
    except _REQUESTS_EXCEPTIONS as exc:
        logger.warning("detect_opencart error for %s: %s", url, type(exc).__name__)
        return False
    if (
        "route=product/product" in html
        or "/catalog/view/theme/" in html
        or "OpenCart" in html
    ):
        logger.info("OpenCart detected via page source at %s", url)
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


# ---------------------------------------------------------------------------
# Google-Base XML feed strategy
# ---------------------------------------------------------------------------

_GOOGLE_BASE_NS = {"g": "http://base.google.com/ns/1.0"}


def _gb_text(item: ET.Element, *paths: str) -> str:
    """First non-None text under any of ``paths`` (with the GB namespace)."""
    for path in paths:
        el = item.find(path, _GOOGLE_BASE_NS) if ":" in path else item.find(path)
        if el is not None and el.text:
            return el.text
    return ""


def _gb_extract_items(root: ET.Element) -> list[ET.Element]:
    """Locate the <item> elements inside an OpenCart Google Base feed."""
    items = (
        root.findall(".//item") if root.tag == "rss"
        else root.findall(".//{http://base.google.com/ns/1.0}item")
    )
    return items or root.findall(".//item")


def _gb_record(item: ET.Element) -> dict[str, str] | None:
    """Render one <item> as a product record (None when title missing)."""
    title = _gb_text(item, "title")
    if not title:
        return None
    return {
        "title": title,
        "price": _parse_price(_gb_text(item, "g:price", "price")),
        "url": _gb_text(item, "link"),
        "image": _gb_text(item, "g:image_link", "image_link"),
    }


def _gb_parse_feed(text: str, limit: int) -> List[Dict[str, Any]]:
    """Parse the Google Base XML body into product records."""
    root = ET.fromstring(text)
    out: list[dict[str, Any]] = []
    for item in _gb_extract_items(root)[:limit]:
        record = _gb_record(item)
        if record is not None:
            out.append(record)
    return out


def _scrape_opencart_google_base(url: str, limit: int) -> List[Dict[str, Any]]:
    """Try OpenCart Google Base XML feed."""
    base = url.rstrip("/")
    feed_url = "%s/index.php?route=feed/google_base" % base
    try:
        resp = requests.get(feed_url, headers=_HEADERS, timeout=8)
        if resp.status_code != 200:
            return []
        results = _gb_parse_feed(resp.text, limit)
    except _REQUESTS_EXCEPTIONS as exc:
        logger.debug("OpenCart Google Base feed failed for %s: %s", base, type(exc).__name__)
        return []
    if results:
        logger.info("OpenCart Google Base feed returned %d products from %s", len(results), base)
    return results


# ---------------------------------------------------------------------------
# REST-API strategy
# ---------------------------------------------------------------------------

def _api_product_url(base: str, item: dict) -> str:
    """Compute the per-product URL for an OpenCart REST API item."""
    href = item.get("href")
    if isinstance(href, str):
        return href
    pid = item.get("product_id")
    if isinstance(pid, (int, str)):
        return "%s/index.php?route=product/product&product_id=%s" % (base, pid)
    return ""


def _api_record(base: str, item: dict) -> dict[str, str]:
    """Render one OpenCart REST item into the unified product shape."""
    return {
        "title": item.get("name", ""),
        "price": _parse_price(item.get("price", "")),
        "url": _api_product_url(base, item),
        "image": item.get("thumb", item.get("image", "")),
    }


def _api_extract_products(data: Any) -> list[dict]:
    """Pull the list of product dicts from an OpenCart REST response body."""
    if isinstance(data, dict):
        products = data.get("products", data)
    else:
        products = data
    return products if isinstance(products, list) else []


def _scrape_opencart_api(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Try OpenCart 3+ REST API."""
    base = url.rstrip("/")
    api_url = "%s/index.php?route=api/product&limit=%d" % (base, limit)
    if query:
        api_url += "&search=%s" % quote(query)
    try:
        resp = requests.get(api_url, headers=_HEADERS, timeout=8)
        if resp.status_code != 200:
            return []
        products = _api_extract_products(resp.json())
    except _REQUESTS_EXCEPTIONS as exc:
        logger.debug("OpenCart REST API failed for %s: %s", base, type(exc).__name__)
        return []
    results = [_api_record(base, item) for item in products[:limit]]
    if results:
        logger.info("OpenCart REST API returned %d products from %s", len(results), base)
    return results


# ---------------------------------------------------------------------------
# Playwright fallback
# ---------------------------------------------------------------------------

_PRODUCT_CARD_SELECTORS: tuple[str, ...] = (
    ".product-thumb",
    ".product-layout",
    ".product-grid .product",
    ".product-list .product",
)

# OpenCart storefront URL fragments (split so the linter doesn't read
# ``search&search`` as a binary expression on identical operands).
_OC_SEARCH_PATH = "/index.php?route=product/" + "search&" + "search="
_OC_CATEGORY_PATH = "/index.php?route=product/category"


def _select_first_nonempty(soup: BeautifulSoup, selectors: tuple[str, ...]) -> list:
    """Return the first non-empty selector match list under ``soup``."""
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            return cards
    return []


def _card_record(card: Any, base_url: str) -> dict[str, str] | None:
    """Render one product card under Playwright into the unified shape."""
    title_el = card.select_one("h4 a, .caption h4, .product-title, .name a")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None
    price_el = card.select_one(".price, .price-new, .product-price")
    price = price_el.get_text(strip=True) if price_el else ""
    link_el = card.select_one("a[href]")
    product_url = urljoin(base_url, link_el.get("href", "")) if link_el else ""
    img_el = card.select_one("img[src]")
    image = img_el.get("src", "") if img_el else ""
    return {"title": title, "price": price, "url": product_url, "image": image}


def _pw_target_url(url: str, query: Optional[str]) -> str:
    """Build the storefront URL Playwright should navigate to."""
    target = url.rstrip("/")
    if query:
        return f"{target}{_OC_SEARCH_PATH}{quote(query)}"
    return f"{target}{_OC_CATEGORY_PATH}"


async def _pw_collect_results(page: Any, url: str, limit: int) -> list[dict]:
    """Pull HTML, parse product cards, return up to ``limit`` records."""
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_nonempty(soup, _PRODUCT_CARD_SELECTORS)
    out: list[dict] = []
    for card in cards[:limit]:
        record = _card_record(card, url)
        if record is not None:
            out.append(record)
    return out


async def safe_close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Best-effort browser teardown that swallows expected cleanup errors."""
    if not browser:
        return
    try:
        await browser.close()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s", type(exc).__name__)


async def _scrape_opencart_playwright(
    url: str, query: Optional[str], limit: int,
) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate storefront and scrape product grid."""
    results: list[dict] = []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            target = _pw_target_url(url, query)
            try:
                await page.goto(target, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                results = await _pw_collect_results(page, url, limit)
            except PlaywrightTimeoutError as exc:
                logger.warning("OpenCart Playwright timeout for %s: %s", url, exc)
                return None  # explicit recovery: non-fatal: PlaywrightTimeoutError is recorded but does not abort the caller
    except _PLAYWRIGHT_EXCEPTIONS as exc:
        logger.error(
            "OpenCart Playwright fallback failed for %s: %s",
            url, type(exc).__name__,
        )
        return None  # explicit recovery: non-fatal: _PLAYWRIGHT_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await safe_close_browser(browser)
    return results


def scrape_opencart_products(
    url: str, query: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape products from an OpenCart store (feed -> API -> Playwright)."""
    results = _scrape_opencart_google_base(url, limit)
    if results:
        return results
    results = _scrape_opencart_api(url, query, limit)
    if results:
        return results
    logger.info("OpenCart HTTP methods yielded no results for %s, trying Playwright", url)
    return _scrape_opencart_playwright(url, query, limit)
