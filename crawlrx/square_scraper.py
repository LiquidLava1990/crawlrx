"""Square Online scraper for CrawlRX -- detect and scrape Square store products."""

import logging
from typing import Optional

import requests
from playwright.async_api import async_playwright

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, network_exception_types
from .stealth_context import make_stealth_context, close_stealth_context

_HTTP_FAILURE_EXCEPTIONS = network_exception_types() + (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)

logger = logging.getLogger(__name__)

# Constants
TIMEOUT = 8
CDP_PORT = 9231
PLAYWRIGHT_TIMEOUT = 15000


def detect_square(url: str, timeout: int = TIMEOUT) -> bool:
    """Detect if a URL is a Square Online-hosted site.
    
    Checks page source for square-online.com or squareup.com/store markers.
    """
    if not url:
        return False
    
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.0"
            )
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text.lower()
        
        markers = ["square-online.com", "squareup.com/store", "square.site"]
        for marker in markers:
            if marker in html:
                logger.info("Square Online detected at %s via marker: %s", url, marker)
                return True
        return False
    except _HTTP_FAILURE_EXCEPTIONS as e:
        logger.warning("Square detection failed for %s: %s", url, e)
        return False


_SQUARE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.0"
    ),
    "Accept": "application/json",
}

_SQUARE_PLAYWRIGHT_JS = """() => {
    const items = [];
    const cards = document.querySelectorAll(
        '[data-testid="product-card"], .product-card, .sqs-product, [data-hook="product-grid-item"]'
    );
    cards.forEach(card => {
        const titleEl = card.querySelector('h3, .product-name, [data-testid="product-title"], .title');
        const priceEl = card.querySelector('.price, [data-testid="product-price"], .sqs-money-native');
        const linkEl = card.querySelector('a');
        const imgEl = card.querySelector('img');
        const title = titleEl ? titleEl.innerText.trim() : '';
        const price = priceEl ? priceEl.innerText.trim() : '';
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const image = imgEl ? imgEl.src : '';
        const outOfStock = card.innerText.toLowerCase().indexOf('out of stock') !== -1 ||
                          card.innerText.toLowerCase().indexOf('sold out') !== -1;
        if (title) {
            items.push({title, price, href, image, in_stock: !outOfStock});
        }
    });
    return items;
}"""


def _api_item_to_product(item: dict, base_url: str) -> dict:
    return {
        "title": item.get("name", item.get("title", "")),
        "price": _normalize_price(item.get("price")),
        "url": _resolve_url(base_url, item.get("url", item.get("permalink", ""))),
        "image": _extract_image(item),
        "in_stock": item.get("in_stock", item.get("inventory", {}).get("available", True)),
    }


def _scrape_square_via_api(base_url: str, query: Optional[str], limit: int) -> list:
    api_url = "%s/api/items" % base_url
    params: dict = {"limit": limit}
    if query:
        params["query"] = query
    try:
        resp = requests.get(api_url, headers=_SQUARE_HEADERS, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except _HTTP_FAILURE_EXCEPTIONS as e:
        logger.info("Square API endpoint failed, trying Playwright: %s", e)
        return []

    items = data.get("items", []) if isinstance(data, dict) else data
    return [
        _api_item_to_product(item, base_url)
        for item in items[:limit]
        if isinstance(item, dict)
    ]


def _playwright_product(prod: dict, base_url: str) -> dict:
    return {
        "title": prod.get("title", ""),
        "price": _normalize_price(prod.get("price")),
        "url": _resolve_url(base_url, prod.get("href", "")),
        "image": prod.get("image", ""),
        "in_stock": prod.get("in_stock", True),
    }


async def _scrape_square_via_playwright(base_url: str, query: Optional[str], limit: int) -> list:
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            shop_url = "%s/shop" % base_url
            if query:
                shop_url += "?q=%s" % query
            await page.goto(shop_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
            await page.wait_for_timeout(2000)
            products = await page.evaluate(_SQUARE_PLAYWRIGHT_JS)
            return [_playwright_product(p, base_url) for p in products[:limit]]
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("Square Playwright scrape failed for %s: %s", base_url, e)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


async def scrape_square_products(
    url: str,
    query: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Scrape Square Online products via Storefront API, fall back to Playwright."""
    if not url:
        return []
    base_url = url.rstrip("/")

    api_results = _scrape_square_via_api(base_url, query, limit)
    if api_results:
        logger.info("Square API scrape: %d products from %s", len(api_results), url)
        return api_results

    pw_results = await _scrape_square_via_playwright(base_url, query, limit)
    logger.info("Square Playwright scrape: %d products from %s", len(pw_results), url)
    return pw_results


def _normalize_price(price_val) -> str:
    """Normalize price to string format."""
    if price_val is None:
        return ""
    if isinstance(price_val, (int, float)):
        return "$%.2f" % price_val
    price_str = str(price_val).strip()
    if not price_str.startswith("$") and price_str.replace(".", "").replace(",", "").isdigit():
        return "$%s" % price_str
    return price_str


def _extract_image(item: dict) -> str:
    """Extract image URL from Square item data."""
    images = item.get("images", [])
    if images and isinstance(images, list):
        first = images[0]
        if isinstance(first, dict):
            return first.get("url", first.get("src", ""))
        return str(first)
    return item.get("image_url", item.get("imageUrl", ""))


def _resolve_url(base: str, path: str) -> str:
    """Resolve relative URL to absolute."""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        parts = base.split("/")
        return "%s//%s%s" % (parts[0], parts[2], path) if len(parts) > 2 else base + path
    return "%s/%s" % (base.rstrip("/"), path)
