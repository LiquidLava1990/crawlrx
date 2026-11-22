"""Volusion scraper -- detect + scrape Volusion-hosted store products."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    network_exception_types,
)
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
_VOLUSION_MARKERS: tuple[str, ...] = ("volusion.com", "v-list-group", "volusion")
_VOLUSION_EVAL_JS = """() => {
    const items = [];
    const cards = document.querySelectorAll(
        '.product-item, .v-product, .product-card, [class*="product"]'
    );
    cards.forEach(card => {
        const titleEl = card.querySelector('.product-name, h2, h3, .title, a');
        const priceEl = card.querySelector('.price, .product-price, [class*="price"]');
        const linkEl = card.querySelector('a');
        const title = titleEl ? titleEl.innerText.trim() : '';
        const price = priceEl ? priceEl.innerText.trim() : '';
        const href = linkEl ? linkEl.getAttribute('href') : '';
        if (title && title.length > 0) {
            items.push({title, price, href});
        }
    });
    return items;
}"""


def detect_volusion(url: str, timeout: int = TIMEOUT) -> bool:
    """Return True if a URL is a Volusion-hosted store."""
    if not url:
        return False
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text.lower()
    except _NETWORK_ERRORS as exc:
        logger.warning(
            "Volusion detection failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        return False
    for marker in _VOLUSION_MARKERS:
        if marker in html:
            logger.info("Volusion detected at %s via marker: %s", url, marker)
            return True
    return False


def _normalize_price(price_val: Any) -> str:
    """Normalize a price value to a string with leading ``$``."""
    if price_val is None:
        return ""
    if isinstance(price_val, (int, float)):
        return f"${price_val:.2f}"
    price_str = str(price_val).strip()
    if (
        not price_str.startswith("$")
        and price_str.replace(".", "").replace(",", "").isdigit()
    ):
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


def _xml_product_to_dict(prod: Any) -> Optional[dict[str, str]]:
    """Convert a single ``<product>`` XML element to our payload dict."""
    try:
        title_tag = prod.find("productname")
        price_tag = prod.find("saleprice") or prod.find("productprice")
        url_tag = prod.find("producturl")
        title = title_tag.get_text() if title_tag else ""
        price = price_tag.get_text() if price_tag else ""
        prod_url = url_tag.get_text() if url_tag else ""
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Volusion XML row skip: %s: %s", type(exc).__name__, exc)
        return None
    if not title:
        return None
    return {
        "title": title.strip(),
        "price": _normalize_price(price),
        "url": prod_url.strip() if prod_url else "",
    }


def _scrape_xml_feed(base_url: str, limit: int) -> List[dict[str, str]]:
    """Try the ``/nl/xml-exportproduct.asp`` XML feed; return [] on miss."""
    xml_url = f"{base_url}/nl/xml-exportproduct.asp?dnb=y"
    try:
        resp = requests.get(xml_url, headers=_DEFAULT_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except _NETWORK_ERRORS as exc:
        logger.info(
            "Volusion XML endpoint failed: %s: %s",
            type(exc).__name__, exc,
        )
        return []
    soup = BeautifulSoup(resp.content, "xml")
    products = soup.find_all("product")[:limit]
    out: List[dict[str, str]] = []
    for prod in products:
        record = _xml_product_to_dict(prod)
        if record is not None:
            out.append(record)
    return out


async def close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Close a Playwright browser, swallowing only known close-time errors."""
    if not browser:
        return
    try:
        await browser.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s: %s", type(exc).__name__, exc)


async def _scrape_playwright_products(
    base_url: str, limit: int
) -> List[dict[str, str]]:
    """Drive a Playwright session for the Volusion ``/products`` page."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.goto(
                f"{base_url}/products",
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_TIMEOUT,
            )
            await page.wait_for_timeout(2000)
            products = await page.evaluate(_VOLUSION_EVAL_JS) or []
            return _shape_pw_products(products[:limit], base_url)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "Volusion Playwright scrape failed for %s: %s: %s",
            base_url, type(exc).__name__, exc,
        )
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser(browser)


def _shape_pw_products(
    products: List[dict[str, Any]], base_url: str
) -> List[dict[str, str]]:
    """Shape Playwright records into the canonical payload list."""
    out: List[dict[str, str]] = []
    for prod in products:
        out.append({
            "title": prod.get("title", ""),
            "price": _normalize_price(prod.get("price")),
            "url": _resolve_url(base_url, prod.get("href", "")),
        })
    return out


async def scrape_volusion_products(
    url: str, query: Optional[str] = None, limit: int = 50
) -> list:
    """Scrape Volusion store products: XML feed first, Playwright fallback."""
    if not url:
        return []
    base_url = url.rstrip("/")
    xml_results = _scrape_xml_feed(base_url, limit)
    if xml_results:
        logger.info("Volusion XML scrape: %d products from %s", len(xml_results), url)
        return xml_results
    pw_results = await _scrape_playwright_products(base_url, limit)
    logger.info("Volusion Playwright scrape: %d products from %s", len(pw_results), url)
    return pw_results
