"""kajabi_scraper.py -- Kajabi course/product scraper for CrawlRX.

Detection and product extraction for Kajabi-hosted sites.
Strategy:
  1. Detection: Check page source for Kajabi markers
  2. Scraping: Try {url}/products.json first, Playwright CDP (port 9236) fallback
"""

import logging
from typing import TYPE_CHECKING, Optional, List, Dict, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.kajabi_scraper")

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "application/json, text/html, */*",
}


def detect_kajabi(url: str, timeout: int = _TIMEOUT) -> bool:
    """Return True if URL is a Kajabi site.

    Checks page source for kajabi.com, kjb-header, or kajabi-cdn markers.
    """
    if not url:
        return False

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        src = resp.text.lower()

        markers = ["kajabi.com", "kjb-header", "kajabi-cdn"]
        for marker in markers:
            if marker in src:
                logger.info("Kajabi detected at %s via marker: %s", url, marker)
                return True
        return False
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("Kajabi detection failed for %s: %s", url, e)
        return False


def _matches_query(title: str, description: str, query: Optional[str]) -> bool:
    """Case-insensitive match against title or description."""
    if not query:
        return True
    q_lower = query.lower()
    return q_lower in title.lower() or q_lower in description.lower()


def _extract_price(item: dict) -> str:
    """Pull a price string from a Kajabi item dict."""
    if "price" in item:
        price_val = item["price"]
        if isinstance(price_val, (int, float)):
            return "$%.2f" % price_val
        return str(price_val)
    variants = item.get("variants")
    if isinstance(variants, list) and variants:
        first = variants[0]
        if isinstance(first, dict):
            first_price = first.get("price", "")
            return f"${first_price}" if first_price else ""
    return ""


def _extract_image(item: dict) -> str:
    """Pull an image URL from a Kajabi item dict."""
    image = item.get("image") or item.get("thumbnail", "")
    if isinstance(image, dict):
        return image.get("src") or image.get("url", "")
    return image if isinstance(image, str) else ""


def _extract_product_url(item: dict, base_url: str) -> str:
    """Resolve a Kajabi product URL relative to `base_url`."""
    product_url = item.get("url") or item.get("handle", "")
    if not product_url:
        return ""
    if product_url.startswith("http"):
        return product_url
    return f"{base_url}/products/{product_url.lstrip('/')}"


def _build_json_product(item: dict, url: str, base_url: str) -> Dict[str, Any]:
    """Map a JSON API item to standard product dict."""
    return {
        "title": item.get("title") or item.get("name", ""),
        "price": _extract_price(item),
        "description": item.get("description", ""),
        "url": _extract_product_url(item, base_url) or url,
        "image": _extract_image(item),
    }


def _scrape_json_api(url: str, query: Optional[str], limit: int) -> Optional[List[Dict[str, Any]]]:
    """Try to scrape via {url}/products.json endpoint."""
    base_url = url.rstrip("/")
    json_url = f"{base_url}/products.json"
    try:
        resp = requests.get(json_url, headers=_HEADERS, timeout=_TIMEOUT)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.info("Kajabi JSON endpoint failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        logger.info("Kajabi JSON decode failed: %s", exc)
        return None
    items = data if isinstance(data, list) else data.get("products", [])
    products: list[Dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        built = _build_json_product(item, url, base_url)
        if not _matches_query(built["title"], built["description"], query):
            continue
        products.append(built)
    if products:
        logger.info("Kajabi JSON scrape: %d products from %s", len(products), url)
        return products
    return None


_PW_EXTRACT_JS = """() => {
    const results = [];
    const containers = document.querySelectorAll(
        '.product-card, .product-item, .course-card, .kjb-product-card, ' +
        '[class*="product"], [data-type="product"]'
    );
    containers.forEach(container => {
        const titleEl = container.querySelector('h2, h3, .title, .product-title, [class*="title"]');
        const priceEl = container.querySelector('.price, [class*="price"], .amount');
        const descEl = container.querySelector('.description, p, [class*="description"]');
        const imgEl = container.querySelector('img');
        const linkEl = container.querySelector('a');
        const title = titleEl ? titleEl.innerText.trim() : '';
        const price = priceEl ? priceEl.innerText.trim() : '';
        const description = descEl ? descEl.innerText.trim() : '';
        const image = imgEl ? (imgEl.src || imgEl.getAttribute('data-src') || '') : '';
        const link = linkEl ? linkEl.getAttribute('href') : '';
        if (title) {
            results.push({ title, price, description, image, link });
        }
    });
    return results;
}"""


def _resolve_link(link: str, base_url: str) -> str:
    """Make a DOM-extracted link absolute relative to `base_url`."""
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return base_url.rstrip("/") + "/" + link.lstrip("/")


def _build_pw_product(item: dict, url: str) -> Dict[str, Any]:
    """Build a standard dict from one DOM-extracted Kajabi card."""
    product_url = _resolve_link(item.get("link", ""), url)
    return {
        "title": item.get("title", ""),
        "price": item.get("price", ""),
        "description": item.get("description", ""),
        "url": product_url or url,
        "image": item.get("image", ""),
    }


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a browser, swallowing narrow cleanup failures."""
    if not browser:
        return
    try:
        await browser.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _wait_for_idle(page: Any, timeout_ms: int = 15000) -> None:
    """Best-effort networkidle wait."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return


async def _scrape_playwright(url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Fallback Playwright scrape via CDP port 9236."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("Kajabi Playwright fallback for %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_idle(page)
            await page.wait_for_timeout(2000)
            data = await page.evaluate(_PW_EXTRACT_JS)
        results: List[Dict[str, Any]] = []
        for item in data[:limit]:
            built = _build_pw_product(item, url)
            if not _matches_query(built["title"], built["description"], query):
                continue
            results.append(built)
        logger.info("Kajabi Playwright scrape: %d products from %s", len(results), url)
        return results
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Kajabi Playwright scrape failed for %s: %s", url, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


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


def scrape_kajabi_products(
    url: str, query: Optional[str] = None, limit: int = 20,
) -> List[Dict[str, Any]]:
    """Scrape Kajabi product/course listings (JSON + Playwright fallback)."""
    if not url:
        return []
    products = _scrape_json_api(url, query, limit)
    if products:
        return products[:limit]
    return _run_async(_scrape_playwright(url, query, limit))[:limit]


