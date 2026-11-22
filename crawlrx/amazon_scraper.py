"""amazon_scraper.py -- Amazon product search scraper.

Search Amazon for products and extract results.
Primary: httpx with browser-like headers.
Fallback: Playwright CDP (port 9239) if blocked.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from ._ecommerce_product_helpers import (
    BROWSER_OR_HTTP_EXCEPTIONS,
    CARD_PROBE_EXCEPTIONS,
    HTTPX_EXCEPTIONS,
    DEFAULT_CAPTCHA_SIGNALS,
    parse_amazon_price,
    parse_amazon_rating,
    parse_asin_from_url,
    parse_review_count_int,
    soup_text,
    text_has_captcha,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow

logger = logging.getLogger("crawlrx.amazon_search")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

CAPTCHA_SIGNALS = DEFAULT_CAPTCHA_SIGNALS

_TITLE_CLASS_RE = re.compile(r"a-size")
_TITLE_FALLBACK_RE = re.compile(r"a-size-medium")
_LINK_CLASS_RE = re.compile(r"a-link-normal")
_PRICE_CLASS_RE = re.compile(r"a-price")
_RATING_CLASS_RE = re.compile(r"a-icon-alt")
_REVIEWS_CLASS_RE = re.compile(r"a-size-base")
_IMAGE_CLASS_RE = re.compile(r"s-image")


def _is_captcha_page(content: str) -> bool:
    """Check if page content indicates captcha."""
    return text_has_captcha(content, CAPTCHA_SIGNALS)


def _absolutize(href: str) -> str:
    """Convert a relative Amazon href to an absolute URL on amazon.com."""
    if not href:
        return ""
    if href.startswith("/"):
        return f"https://www.amazon.com{href}"
    if not href.startswith("http"):
        return f"https://www.amazon.com/{href}"
    return href


def _extract_title(container: Any) -> Optional[str]:
    """Read the search-result product title from a soup container."""
    title_elem = container.find("h2", class_=_TITLE_CLASS_RE)
    if not title_elem:
        title_elem = container.find("span", class_=_TITLE_FALLBACK_RE)
    return soup_text(title_elem)


def _extract_url_and_asin(container: Any) -> tuple[Optional[str], Optional[str]]:
    """Read the absolute product URL + ASIN from a search-result container."""
    link_elem = container.find("a", class_=_LINK_CLASS_RE, href=True)
    if not link_elem:
        return None, None
    href = link_elem.get("href", "")
    absolute = _absolutize(href)
    return absolute.split("?")[0], parse_asin_from_url(absolute)


def _extract_price(container: Any) -> Optional[str]:
    """Read the offscreen formatted price from a search-result container."""
    price_elem = container.find("span", class_=_PRICE_CLASS_RE)
    if not price_elem:
        return None
    offscreen = price_elem.find("span", class_="a-offscreen")
    return parse_amazon_price(soup_text(offscreen))


def _extract_rating(container: Any) -> Optional[float]:
    """Read the floating-point rating from a search-result container."""
    rating_elem = container.find("span", class_=_RATING_CLASS_RE)
    if not rating_elem:
        return None
    return parse_amazon_rating(soup_text(rating_elem))


def _extract_review_count(container: Any) -> Optional[int]:
    """Read the integer review count from a search-result container."""
    reviews_elem = container.find("span", class_=_REVIEWS_CLASS_RE)
    if not reviews_elem:
        return None
    text = soup_text(reviews_elem)
    if not text:
        return None
    if "(" not in text and not any(c.isdigit() for c in text):
        return None
    return parse_review_count_int(text)


def _extract_image(container: Any) -> Optional[str]:
    """Read the main product image URL from a search-result container."""
    img_elem = container.find("img", class_=_IMAGE_CLASS_RE)
    if not img_elem:
        return None
    return img_elem.get("src") or img_elem.get("data-src")


def _container_to_product(container: Any) -> Optional[dict]:
    """Normalize a soup search-result container into a product dict."""
    title = _extract_title(container)
    if not title:
        return None
    url, asin = _extract_url_and_asin(container)
    return {
        "title": title,
        "url": url,
        "asin": asin,
        "price": _extract_price(container),
        "rating": _extract_rating(container),
        "reviews": _extract_review_count(container),
        "image": _extract_image(container),
    }


def _parse_search_results_httpx(html: str, base_url: str) -> List[dict]:
    """Parse Amazon search results from HTML using BeautifulSoup."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    products: List[dict] = []
    containers = soup.find_all("div", {"data-component-type": "s-search-result"})
    for container in containers:
        try:
            product = _container_to_product(container)
        except CARD_PROBE_EXCEPTIONS as exc:
            log_and_swallow(logger, logging.DEBUG, "amazon parse container", exc)
            continue
        if product:
            products.append(product)
    return products


async def _scrape_with_httpx(query: str, max_results: int, timeout: float = 20.0) -> List[dict]:
    """Try scraping with httpx (fast, no browser)."""
    url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.text
    if _is_captcha_page(content):
        logger.warning("[Amazon] Captcha detected with httpx, will fallback to Playwright")
        return []
    products = _parse_search_results_httpx(content, url)
    logger.info("[Amazon] httpx returned %d products", len(products))
    return products[:max_results]


_PLAYWRIGHT_EVAL_JS = """() => {
    const items = document.querySelectorAll('[data-component-type="s-search-result"]');
    const products = [];

    items.forEach(item => {
        const product = {};

        const titleEl = item.querySelector('h2 a span') || item.querySelector('h2 span') || item.querySelector('[data-cy="title-recipe-title"]');
        product.title = titleEl ? titleEl.textContent.trim() : null;

        const linkEl = item.querySelector('h2 a') || item.querySelector('a[href*="/dp/"]');
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            product.url = href.startsWith('http') ? href : 'https://www.amazon.com' + href.split('?')[0];
            const asinMatch = href.match(/\\/dp\\/([A-Z0-9]{10})/);
            product.asin = asinMatch ? asinMatch[1] : null;
        }

        const priceEl = item.querySelector('.a-price .a-offscreen');
        product.price = priceEl ? priceEl.textContent.trim() : null;

        const ratingEl = item.querySelector('.a-icon-alt');
        if (ratingEl) {
            const ratingText = ratingEl.textContent;
            const match = ratingText.match(/([0-9.]+)/);
            product.rating = match ? parseFloat(match[1]) : null;
        }

        const reviewsEl = item.querySelector('a[href*="#customerReviews"] span');
        if (reviewsEl) {
            const text = reviewsEl.textContent;
            const match = text.match(/([0-9,]+)/);
            product.reviews = match ? parseInt(match[1].replace(',', '')) : null;
        }

        const imgEl = item.querySelector('.s-image');
        product.image = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src')) : null;

        if (product.title) {
            products.push(product);
        }
    });

    return products;
}"""


async def _pw_wait_for_results(page: Any) -> None:
    """Wait for search results, swallowing the expected timeout."""
    try:
        await page.wait_for_selector('[data-component-type="s-search-result"]', timeout=5000)
    except PlaywrightTimeoutError:
        logger.warning("[Amazon] Playwright timeout waiting for results")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal


async def disconnect_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Disconnect a Playwright CDP browser; swallow narrow shutdown failures."""
    if not browser:
        return
    try:
        await browser.disconnect()
        logger.debug("[Amazon] Browser disconnected (CDP)")
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "amazon browser disconnect", exc)


async def _pw_collect_results(page: Any, max_results: int) -> List[dict]:
    """Run the in-page extractor JS and return up to ``max_results`` products."""
    body_text = await page.inner_text("body")
    if _is_captcha_page(body_text):
        logger.error("[Amazon] Captcha detected with Playwright")
        return []
    results = await page.evaluate(_PLAYWRIGHT_EVAL_JS)
    return results[:max_results]


async def _scrape_with_playwright(query: str, max_results: int, timeout: float = 20.0) -> List[dict]:
    """Fallback: Use Playwright CDP on port 9239."""
    url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
    products: List[dict] = []
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
            await _pw_wait_for_results(page)
            products = await _pw_collect_results(page, max_results)
            logger.info("[Amazon] Playwright returned %d products", len(products))
            await page.close()
            await context.close()
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[Amazon] Playwright error: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_OR_HTTP_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await disconnect_browser(browser)
    return products


async def _try_httpx(query: str, max_results: int) -> Optional[List[dict]]:
    """Attempt the httpx fast path; None when it fails so the fallback runs."""
    try:
        products = await _scrape_with_httpx(query, max_results, timeout=20.0)
    except HTTPX_EXCEPTIONS as exc:
        logger.warning("[Amazon] httpx failed: %s", exc)
        return None
    return products if products else None


async def scrape_amazon_products(query: str, max_results: int = 20) -> List[dict]:
    """Search Amazon for products and return structured results."""
    if not query or not query.strip():
        return []
    httpx_results = await _try_httpx(query, max_results)
    if httpx_results:
        return httpx_results
    logger.info("[Amazon] Falling back to Playwright CDP")
    try:
        return await _scrape_with_playwright(query, max_results, timeout=20.0)
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        logger.error("[Amazon] Playwright fallback failed: %s", exc)
        return []


def scrape_amazon_products_sync(query: str, max_results: int = 20) -> List[dict]:
    """Synchronous wrapper for scrape_amazon_products."""
    return asyncio.run(scrape_amazon_products(query, max_results))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = scrape_amazon_products_sync("wireless headphones", max_results=5)
    print(f"Found {len(result)} products:")
    for p in result:
        title = (p.get("title") or "N/A")[:60]
        print(f"  - {title}... | ${p.get('price', 'N/A')} | {p.get('rating', 'N/A')} stars")
