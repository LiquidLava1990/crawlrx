"""OsCommerce scraper for CrawlRX -- detect and scrape OsCommerce store products."""

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import requests
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from .stealth_context import make_stealth_context, close_stealth_context

logger = logging.getLogger(__name__)

# Constants
TIMEOUT = 8
CDP_PORT = 9231
PLAYWRIGHT_TIMEOUT = 15000

_REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException, requests.ConnectionError, requests.Timeout,
    requests.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError,
)

_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, IndexError, LookupError,
)

_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, ValueError, AttributeError, TypeError, KeyError,
)

_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, RuntimeError, AttributeError, ValueError,
)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"
    )
}


def _detection_marker(url: str, html: str, headers_str: str) -> Optional[str]:
    """Return the first OsCommerce signal present, or None if no match."""
    html_lower = html.lower()
    if "oscommerce" in html_lower:
        return "HTML marker: oscommerce"
    if "/catalog/" in url.lower() or "/catalog/" in html_lower:
        return "/catalog/ path"
    if "oscsid" in html_lower or "oscsid" in headers_str.lower():
        return "osCsid cookie"
    if "osc_" in html_lower:
        return "osC_ JS variables"
    return None


def detect_oscommerce(url: str, headers: dict, html: str) -> bool:
    """Detect if a URL is an OsCommerce-hosted store."""
    if not url or not html:
        return False
    try:
        marker = _detection_marker(url, html, str(headers))
    except _PARSE_EXCEPTIONS as exc:
        logger.warning(
            "OsCommerce detection failed for %s: %s", url, type(exc).__name__,
        )
        return False
    if marker is None:
        return False
    logger.info("OsCommerce detected at %s via %s", url, marker)
    return True


def _normalize_price(price_val: Any) -> str:
    """Normalize price to string format."""
    if price_val is None:
        return ""
    if isinstance(price_val, (int, float)):
        return "$%.2f" % price_val
    price_str = str(price_val).strip()
    match = re.search(r'\$[\d,]+\.?\d*', price_str)
    if match:
        return match.group(0)
    if (
        not price_str.startswith("$")
        and price_str.replace(".", "").replace(",", "").replace(" ", "").isdigit()
    ):
        return "$%s" % price_str
    return price_str


def _resolve_url(base: str, path: str) -> str:
    """Resolve relative URL to absolute."""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        parts = base.split("/")
        if len(parts) > 2:
            return "%s//%s%s" % (parts[0], parts[2], path)
        return base + path
    return "%s/%s" % (base.rstrip("/"), path)


# ---------------------------------------------------------------------------
# Catalog (requests + bs4) strategy
# ---------------------------------------------------------------------------

# Compiled at import-time so `_find_product_containers` does not pay the
# `re.compile` cost on every soup scan.
_PRODUCT_ROW_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("tr", re.compile("product|listing")),
    ("div", re.compile("product|listing|content")),
    ("td", re.compile("product|listing")),
)


def _find_product_containers(soup: BeautifulSoup) -> list:
    """Find product container rows trying each known OsCommerce shape."""
    for tag, class_pattern in _PRODUCT_ROW_PATTERNS:
        containers = soup.find_all(tag, class_=class_pattern)
        if containers:
            return containers
    return []


def _collect_category_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Pull /catalog/?cPath= links from the index page."""
    out: list[str] = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if "cPath=" not in href:
            continue
        if href.startswith("http"):
            out.append(href)
        else:
            out.append("%s/%s" % (base_url.rstrip("/"), href.lstrip("/")))
    return list(dict.fromkeys(out))[:5]


# Compile-once: container-level helpers all run inside the per-page loop.
_PRODUCTS_ID_HREF_RE = re.compile("products_id=")
_PRICE_TEXT_RE = re.compile(r"\$[\d,]+\.?\d*")
_PRICE_CLASS_RE = re.compile("price|productPrice")


def _container_title_and_url(container: Any, base_url: str) -> tuple[str, str]:
    """Extract title text and product URL from a product container."""
    title_el = container.find("a", href=_PRODUCTS_ID_HREF_RE)
    if not title_el:
        title_el = container.find(["h2", "h3", "h4", "strong", "a"])
    title = title_el.get_text(strip=True) if title_el else ""
    if not title_el or not title_el.get("href"):
        return title, ""
    href = title_el.get("href")
    if href.startswith("http"):
        return title, href
    return title, "%s/%s" % (base_url.rstrip("/"), href.lstrip("/"))


def _container_price(container: Any) -> str:
    """Extract a price string from a product container."""
    price_el = container.find(text=_PRICE_TEXT_RE)
    if price_el:
        return price_el.strip()
    price_el = container.find(class_=_PRICE_CLASS_RE)
    return price_el.get_text(strip=True) if price_el else ""


def _container_image(container: Any, base_url: str) -> str:
    """Extract the product image URL from a container."""
    img_el = container.find("img")
    if not img_el:
        return ""
    img_src = img_el.get("src", "")
    if not img_src:
        return ""
    if img_src.startswith("http"):
        return img_src
    return "%s/%s" % (base_url.rstrip("/"), img_src.lstrip("/"))


def _container_in_stock(container: Any) -> bool:
    """True unless the container text says out-of-stock."""
    stock_text = container.get_text().lower()
    return not ("out of stock" in stock_text or "sold out" in stock_text)


def _container_record(container: Any, base_url: str) -> Optional[dict]:
    """Render one product container into the unified record (or None)."""
    try:
        title, prod_url = _container_title_and_url(container, base_url)
    except _PARSE_EXCEPTIONS:
        return None
    if not title:
        return None
    return {
        "title": title,
        "price": _normalize_price(_container_price(container)),
        "url": prod_url,
        "image": _container_image(container, base_url),
        "in_stock": _container_in_stock(container),
    }


def _scrape_category(cat_url: str, base_url: str, limit: int) -> list[dict]:
    """Pull up to ``limit`` product records from one category page."""
    try:
        cat_resp = requests.get(cat_url, headers=_DEFAULT_HEADERS, timeout=TIMEOUT)
        cat_resp.raise_for_status()
    except _REQUESTS_EXCEPTIONS as exc:
        logger.debug("Failed to scrape category %s: %s", cat_url, type(exc).__name__)
        return []
    cat_soup = BeautifulSoup(cat_resp.content, "html.parser")
    containers = _find_product_containers(cat_soup)
    out: list[dict] = []
    for container in containers[:limit]:
        record = _container_record(container, base_url)
        if record is not None:
            out.append(record)
    return out


def _scrape_oscommerce_catalog(base_url: str, limit: int) -> list[dict]:
    """Try the OsCommerce catalog index + per-category sweep."""
    catalog_url = "%s/catalog/index.php?cPath=" % base_url
    try:
        resp = requests.get(catalog_url, headers=_DEFAULT_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except _REQUESTS_EXCEPTIONS as exc:
        logger.info(
            "OsCommerce catalog endpoint failed, trying Playwright: %s",
            type(exc).__name__,
        )
        return []
    soup = BeautifulSoup(resp.content, "html.parser")
    category_links = _collect_category_links(soup, base_url)
    results: list[dict] = []
    for cat_url in category_links:
        results.extend(_scrape_category(cat_url, base_url, limit))
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Playwright fallback
# ---------------------------------------------------------------------------

_OS_PRODUCT_JS = """() => {
    const items = [];
    const rows = document.querySelectorAll(
        'tr[class*="product"], tr[class*="listing"], ' +
        'div.product, div.productItem, ' +
        'td[class*="product"], .contentContainer'
    );
    rows.forEach(row => {
        const titleEl = row.querySelector('a[href*="products_id="], .productTitle, h2 a, h3 a');
        const priceEl = row.querySelector('.productPrice, .price, [class*="price"]');
        const linkEl = row.querySelector('a[href*="products_id="]');
        const imgEl = row.querySelector('img');

        const title = titleEl ? titleEl.innerText.trim() : '';
        const price = priceEl ? priceEl.innerText.trim() : '';
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const image = imgEl ? imgEl.src : '';
        const stockText = row.innerText.toLowerCase();
        const inStock = !stockText.includes('out of stock') &&
                       !stockText.includes('sold out');

        if (title) {
            items.push({title, price, href, image, inStock});
        }
    });
    return items;
}"""


def _pw_record(prod: dict, base_url: str) -> dict:
    """Render one product extracted via Playwright into the unified shape."""
    return {
        "title": prod.get("title", ""),
        "price": _normalize_price(prod.get("price", "")),
        "url": _resolve_url(base_url, prod.get("href", "")),
        "image": prod.get("image", ""),
        "in_stock": prod.get("inStock", True),
    }


async def safe_close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Best-effort browser teardown that swallows expected cleanup errors."""
    if not browser:
        return
    try:
        await browser.close()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s", type(exc).__name__)


async def _scrape_oscommerce_playwright(
    base_url: str, limit: int,
) -> list[dict]:
    """Playwright fallback: render the catalog page and extract products."""
    results: list[dict] = []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            catalog_url = "%s/catalog/" % base_url
            await page.goto(
                catalog_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT,
            )
            await page.wait_for_timeout(2000)
            products = await page.evaluate(_OS_PRODUCT_JS)
            results = [_pw_record(prod, base_url) for prod in products[:limit]]
            logger.info(
                "OsCommerce Playwright scrape: %d products from %s",
                len(results), base_url,
            )
    except _PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning(
            "OsCommerce Playwright scrape failed for %s: %s",
            base_url, type(exc).__name__,
        )
        return None  # explicit recovery: non-fatal: _PLAYWRIGHT_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await safe_close_browser(browser)
    return results


async def scrape_oscommerce_products(
    url: str,
    query: Optional[str] = None,
    limit: int = 50,
) -> list:
    """Scrape product data from an OsCommerce store (catalog -> Playwright)."""
    if not url:
        return []
    base_url = url.rstrip("/")
    results = _scrape_oscommerce_catalog(base_url, limit)
    if results:
        logger.info(
            "OsCommerce catalog scrape: %d products from %s", len(results), url,
        )
        return results
    return await _scrape_oscommerce_playwright(base_url, limit)


# FastAPI router endpoint
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/scrape", tags=["scrape"])


class OsCommerceScrapeRequest(BaseModel):
    url: str
    query: Optional[str] = None
    limit: int = 50


class OsCommerceScrapeResponse(BaseModel):
    products: list
    count: int
    source: str


@router.post("/oscommerce", response_model=OsCommerceScrapeResponse)
async def scrape_oscommerce_endpoint(request: OsCommerceScrapeRequest) -> OsCommerceScrapeResponse:
    """Scrape products from an OsCommerce store."""
    if not request.url:
        raise HTTPException(status_code=400, detail="URL is required")
    products = await scrape_oscommerce_products(
        url=request.url,
        query=request.query,
        limit=request.limit,
    )
    return OsCommerceScrapeResponse(
        products=products,
        count=len(products),
        source="oscommerce",
    )
