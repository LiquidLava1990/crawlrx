"""Weebly scraper -- detect + scrape Weebly-hosted store products."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from playwright.async_api import async_playwright

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, network_exception_types
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger(__name__)

TIMEOUT = 8
CDP_PORT = 9231
PLAYWRIGHT_TIMEOUT = 15000

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"
    ),
}
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    *network_exception_types(),
    requests.RequestException,
    ValueError,
)
_WEEBLY_MARKERS: tuple[str, ...] = ("weebly.com/js/", "cdn.weebly.com")
_WEEBLY_EVAL_JS = """() => {
    const items = [];
    const cards = document.querySelectorAll(
        '.product-card, .wsite-product, [data-hook="product-list-grid"] .product-item, .store-product'
    );
    cards.forEach(card => {
        const titleEl = card.querySelector('.product-title, .wsite-product-title, h2, h3, .title');
        const priceEl = card.querySelector('.product-price, .wsite-product-price, .price, [data-hook="product-price"]');
        const linkEl = card.querySelector('a');
        const imgEl = card.querySelector('img');
        const title = titleEl ? titleEl.innerText.trim() : '';
        const price = priceEl ? priceEl.innerText.trim() : '';
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const image = imgEl ? imgEl.src : '';
        const lower = card.innerText.toLowerCase();
        const soldOut = lower.indexOf('sold out') !== -1 || lower.indexOf('out of stock') !== -1;
        if (title) {
            items.push({title, price, href, image, available: !soldOut});
        }
    });
    return items;
}"""


def detect_weebly(url: str, timeout: int = TIMEOUT) -> bool:
    """Return True if a URL is a Weebly-hosted site."""
    if not url:
        return False
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text.lower()
    except _NETWORK_ERRORS as exc:
        logger.warning(
            "Weebly detection failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        return False
    for marker in _WEEBLY_MARKERS:
        if marker in html:
            logger.info("Weebly detected at %s via marker: %s", url, marker)
            return True
    return False


def _normalize_price(price_val: Any) -> str:
    """Normalize a price value to a string with leading ``$``."""
    if price_val is None:
        return ""
    if isinstance(price_val, (int, float)):
        return f"${price_val:.2f}"
    price_str = str(price_val).strip()
    if not price_str.startswith("$") and price_str.replace(".", "").isdigit():
        return f"${price_str}"
    return price_str


def _resolve_url(base: str, path: str) -> str:
    """Resolve a relative path against ``base`` into an absolute URL."""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        parts = base.split("/")
        if len(parts) > 2:
            return f"{parts[0]}//{parts[2]}{path}"
        return base + path
    return f"{base.rstrip('/')}/{path}"


def _fetch_json(base_url: str, query: Optional[str]) -> List[dict[str, Any]]:
    """Try the Weebly ``/store/p1?format=json`` endpoint."""
    json_url = f"{base_url}/store/p1?format=json"
    if query:
        json_url += f"&q={requests.utils.quote(query)}"
    headers = {**_DEFAULT_HEADERS, "Accept": "application/json"}
    try:
        resp = requests.get(json_url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except _NETWORK_ERRORS as exc:
        logger.info(
            "Weebly JSON endpoint failed: %s: %s",
            type(exc).__name__, exc,
        )
        return []
    if isinstance(data, dict):
        return data.get("items", []) or []
    return data if isinstance(data, list) else []


def _shape_json_item(item: Any, base_url: str) -> Optional[dict[str, Any]]:
    """Convert one JSON-feed item to our payload dict (skip if not a dict)."""
    if not isinstance(item, dict):
        return None
    return {
        "title": item.get("title", ""),
        "price": _normalize_price(item.get("price")),
        "url": _resolve_url(base_url, item.get("url", "")),
        "image": item.get("image") or item.get("imageUrl", ""),
        "available": item.get("available", True),
    }


def _shape_pw_item(item: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Convert one Playwright eval record to our payload dict."""
    return {
        "title": item.get("title", ""),
        "price": _normalize_price(item.get("price")),
        "url": _resolve_url(base_url, item.get("href", "")),
        "image": item.get("image", ""),
        "available": item.get("available", True),
    }


async def close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing only known close-time errors."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _scrape_playwright_products(
    base_url: str, query: Optional[str], limit: int
) -> List[dict[str, Any]]:
    """Drive a Playwright session for the Weebly ``/store`` page."""
    browser = ctx = page = None
    store_url = f"{base_url}/store"
    if query:
        store_url += f"?q={query}"
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.goto(
                store_url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_TIMEOUT,
            )
            await page.wait_for_timeout(2000)
            products = await page.evaluate(_WEEBLY_EVAL_JS) or []
            return [_shape_pw_item(item, base_url) for item in products[:limit]]
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "Weebly Playwright scrape failed for %s: %s: %s",
            base_url, type(exc).__name__, exc,
        )
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser(browser)


async def scrape_weebly_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> list:
    """Scrape Weebly store products: JSON feed first, Playwright fallback."""
    if not url:
        return []
    base_url = url.rstrip("/")
    items = _fetch_json(base_url, query)
    json_results: List[dict[str, Any]] = []
    for item in items[:limit]:
        shaped = _shape_json_item(item, base_url)
        if shaped is not None:
            json_results.append(shaped)
    if json_results:
        logger.info(
            "Weebly JSON scrape: %d products from %s", len(json_results), url
        )
        return json_results
    pw_results = await _scrape_playwright_products(base_url, query, limit)
    logger.info("Weebly Playwright scrape: %d products from %s", len(pw_results), url)
    return pw_results
