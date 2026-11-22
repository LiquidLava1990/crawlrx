"""Per-source adapters for the ecommerce-search pipeline.

Each adapter (SearXNG, Google Shopping, DDGS shopping, store-platform
scrapers) lives here. The runner module composes these into the priority
ladder; this module owns I/O and per-source raw->ProductResult mapping.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

from .config import SEARXNG_URL as _SEARXNG_URL, SEARXNG_WEB_ENGINES
from ._ecommerce_search_filters import (
    _coerce_product_url,
    _parse_price,
    _store_for,
)
from ._ecommerce_search_models import ProductResult
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    network_exception_types,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger('crawlrx.ecommerce_search')

_NETWORK_EXC = network_exception_types()


# Per-source product-builder spec
# ─────────────────────────────────────────────


from dataclasses import dataclass


@dataclass(frozen=True)
class _ProductSpec:
    """Per-source binding for :func:`_build_product`."""

    platform: str
    parse_price: bool = False
    image_field: str = ""
    default_store: str = ""


def _build_product(
    item: Dict[str, Any], spec: _ProductSpec,
) -> Optional[ProductResult]:
    """Generic raw-dict -> ProductResult converter shared by all shims."""
    pair = _coerce_product_url(item)
    if pair is None:
        return None
    title, url = pair
    return ProductResult(
        title=title,
        price=_parse_price(item.get("price")) if spec.parse_price else None,
        url=url,
        store_url=_store_for(url, spec.default_store),
        platform=spec.platform,
        image=item.get(spec.image_field) if spec.image_field else None,
        in_stock=True,
    )


_GS_SPEC = _ProductSpec(
    platform="google_shopping",
    parse_price=True,
    image_field="image",
    default_store="https://shopping.google.com",
)
_DDGS_SPEC = _ProductSpec(platform="ddgs_shopping")
_SEARXNG_SPEC = _ProductSpec(platform="searxng")


def _ddgs_class() -> Optional[type]:
    """Return the installed DDGS class, supporting both package names."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            return None


def _ddgs_text_rows(query: str, limit: int, platform: str) -> List[Dict[str, Any]]:
    """Run DDGS text search and normalize raw hits for ecommerce callers."""
    ddgs_cls = _ddgs_class()
    if ddgs_cls is None:
        logger.warning("ddgs package unavailable for ecommerce search")
        return []
    with ddgs_cls() as ddgs:
        hits = list(ddgs.text(query, max_results=limit, region="en-US"))
    rows = [_ddgs_hit_to_row(h) for h in hits]
    for row in rows:
        row["platform"] = platform
    return rows


# ─────────────────────────────────────────────
# Source 1: SearXNG direct HTTP
# ─────────────────────────────────────────────


def _searxng_direct_blocking(query: str, limit: int) -> List[Dict[str, Any]]:
    """Blocking SearXNG fetch + decode (runs inside ``asyncio.to_thread``)."""
    url = f"{_SEARXNG_URL}/search"
    response = requests.get(
        url,
        params={
            "q": query,
            "format": "json",
            "engines": SEARXNG_WEB_ENGINES,
        },
        headers={"X-Real-IP": "127.0.0.1"},
        timeout=4,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("content", ""), "platform": "searxng", "source": "searxng"}
        for r in response.json().get("results", [])[:limit]
    ]


async def _search_searxng_direct(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Direct HTTP call to local SearXNG instance (fast, no rate limits)."""
    try:
        return await asyncio.to_thread(_searxng_direct_blocking, query, limit)
    except (*_NETWORK_EXC, *PARSE_EXCEPTIONS) as e:
        # explicit recovery: network/parse failure non-fatal; caller treats as empty
        logger.warning("SearXNG direct search failed: %s", e)
        return []


def _searxng_to_product(item: Dict[str, Any]) -> Optional[ProductResult]:
    """Convert a SearXNG raw dict to ProductResult."""
    return _build_product(item, _SEARXNG_SPEC)


# ─────────────────────────────────────────────
# Source 2: Google Shopping via Playwright CDP
# ─────────────────────────────────────────────


async def _gs_item_to_dict(item: Any) -> Optional[Dict[str, Any]]:
    """Map one Google-Shopping grid result to a raw dict; ``None`` on probe error."""
    try:
        title_el = await item.query_selector("h3, .tAxDx")
        price_el = await item.query_selector(".a8Pemb, .kHxwFf")
        link_el = await item.query_selector("a")
        img_el = await item.query_selector("img")
        merchant_el = await item.query_selector(".aULzUe, .IuHnof")
        return {
            "title": (await title_el.inner_text()) if title_el else "",
            "price": (await price_el.inner_text()) if price_el else None,
            "url": (await link_el.get_attribute("href")) if link_el else "",
            "image": (await img_el.get_attribute("src")) if img_el else None,
            "merchant": (await merchant_el.inner_text()) if merchant_el else None,
            "platform": "google_shopping",
            "source": "google_shopping",
        }
    except BROWSER_SCRAPE_EXCEPTIONS:
        return None


async def _gs_collect_results(p: Any, query: str, limit: int) -> List[Dict[str, Any]]:
    """Open the stealth context once, run the query, return raw items."""
    results: list = []
    browser = ctx = page = None
    try:
        browser, ctx, page = await make_stealth_context(p)
        page.set_default_timeout(7000)
        url = "https://www.google.com/search?tbm=shop&q=" + urllib.parse.quote(query)
        await page.goto(url, timeout=7000)
        await page.wait_for_selector(".sh-dgr__grid-result, .mnr-c", timeout=4000)
        items = await page.query_selector_all(".sh-dgr__grid-result")
        for item in items[:limit]:
            record = await _gs_item_to_dict(item)
            if record:
                results.append(record)
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("google shopping collect failed: %s", e)
    finally:
        if browser is not None:
            await close_stealth_context(browser, ctx, page)
        # Do NOT call browser.close() -- that kills the CDP connection.
    return results


async def _search_google_shopping_playwright(
    query: str, limit: int = 10,
) -> List[Dict[str, Any]]:
    """Scrape Google Shopping via Playwright CDP (CRAWLRX_CDP_URL, default port 9222)."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            return await _gs_collect_results(p, query, limit)
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("google shopping playwright failed: %s", e)
        return []  # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal


def _google_shopping_to_product(item: Dict[str, Any]) -> Optional[ProductResult]:
    """Convert a Google Shopping raw dict to ProductResult."""
    return _build_product(item, _GS_SPEC)


# ─────────────────────────────────────────────
# Source 3: DDGS site-targeted shopping
# ─────────────────────────────────────────────


def _ddgs_hit_to_row(h: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw DDGS hit dict to our normalized shopping row shape."""
    return {
        "title": h.get("title", ""),
        "url": h.get("href", ""),
        "snippet": h.get("body", ""),
        "platform": "ddgs_shopping",
        "source": "ddgs",
    }


def _search_ddgs_shopping(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """DDGS search targeting major shopping sites (last resort, rate-limited)."""
    shopping_query = (
        query
        + " site:amazon.com OR site:walmart.com OR site:target.com"
        + " OR site:bestbuy.com price"
    )
    try:
        return _ddgs_text_rows(shopping_query, limit, "ddgs_shopping")
    except (*_NETWORK_EXC, *PARSE_EXCEPTIONS, ImportError, RuntimeError) as e:
        logger.warning("ddgs shopping search failed: %s", e)
        # explicit recovery: network/parse failure non-fatal; caller treats as empty
        return []


def _ddgs_shopping_to_product(item: Dict[str, Any]) -> Optional[ProductResult]:
    """Convert a DDGS shopping raw dict to ProductResult."""
    return _build_product(item, _DDGS_SPEC)


def _search_ecommerce_ddgs(query: str, limit: int = 30) -> List[Dict[str, Any]]:
    """DDGS search optimized for e-commerce stores (Shopify/Woo/etc)."""
    results: List[Dict[str, Any]] = []
    enhanced_query = (
        "%s (shopify OR woocommerce OR store OR buy OR product) -amazon -ebay"
        % query
    )

    try:
        logger.debug("Running DDGS store search with query: %s", enhanced_query)
        search_results = _ddgs_text_rows(enhanced_query, limit, "ddgs_search")

        for r in search_results:
            url = r.get('url', '')
            if not url or not url.startswith('http'):
                continue
            results.append({
                'title': r.get('title', 'Product'),
                'url': url,
                'snippet': r.get('snippet', ''),
                'price': None,
                'platform': 'ddgs_search',
            })

        logger.info("DDGS store search returned %d results", len(results))
    except (*_NETWORK_EXC, *PARSE_EXCEPTIONS, ImportError, RuntimeError) as e:
        logger.warning("DDGS store search error: %s", e)
        # explicit recovery: non-fatal; caller treats as empty
        return []

    return results[:limit]


# ─────────────────────────────────────────────
# Source 4: Store-platform scrapers
# ─────────────────────────────────────────────


def _scrape_shopify(store_url: str, query: str) -> List[ProductResult]:
    """Call the sync Shopify scraper and normalize to ProductResult."""
    from .shopify import scrape_shopify_products

    try:
        raw: List[Dict[str, Any]] = scrape_shopify_products(
            store_url, query=query, limit=50,
        )
    except SCRAPE_FAILURE_EXCEPTIONS as e:
        logger.warning('Shopify scrape failed for %s: %s', store_url, e)
        return []

    results: List[ProductResult] = []
    for p in raw:
        price = _parse_price(p.get('price'))
        price_original = _parse_price(p.get('compare_at_price'))
        if price_original == price:
            price_original = None
        results.append(ProductResult(
            title=p.get('title') or '',
            price=price,
            price_original=price_original,
            url=p.get('url') or store_url,
            store_url=store_url,
            platform='shopify',
            image=p.get('image'),
            in_stock=bool(p.get('available', True)),
        ))
    return results


def _woocommerce_filter_match(name_lower: str, query_lower: str) -> bool:
    """True if ``name_lower`` matches the user's free-text query."""
    if query_lower in name_lower:
        return True
    query_words = [w for w in query_lower.split() if len(w) > 3]
    return any(w in name_lower for w in query_words)


def _woocommerce_product_to_result(
    p: Any, store_url: str,
) -> ProductResult:
    """Map a WooCommerce product object to our ProductResult shape."""
    price = _parse_price(p.price)
    price_original = _parse_price(p.regular_price)
    if price_original == price:
        price_original = None
    image = p.images[0] if p.images else None
    in_stock = (p.stock_status == 'instock') if p.stock_status else True
    return ProductResult(
        title=p.name or '',
        price=price,
        price_original=price_original,
        url=p.url or store_url,
        store_url=store_url,
        platform='woocommerce',
        image=image,
        in_stock=in_stock,
    )


def _try_woocommerce_api(base: str) -> Optional[List[Any]]:
    """Run WooCommerce API scraper; return None on recoverable failure."""
    from .woocommerce import _scrape_products_api  # type: ignore[no-redef]  # because local re-import shadows optional binding
    try:
        return asyncio.run(
            _scrape_products_api(base, count=50, page=1, category=None),
        )
    except SCRAPE_FAILURE_EXCEPTIONS as e:
        logger.warning('WooCommerce API failed for %s: %s', base, e)
        return None  # explicit recovery: API fail -> caller falls back to DOM


def _try_woocommerce_dom(base: str) -> Optional[List[Any]]:
    """Run WooCommerce DOM scraper; return None on recoverable failure."""
    from .woocommerce import _scrape_products_dom  # type: ignore[no-redef]  # because local re-import shadows optional binding
    try:
        return _scrape_products_dom(base, count=50, page=1, category=None)
    except SCRAPE_FAILURE_EXCEPTIONS as e:
        logger.warning('WooCommerce DOM failed for %s: %s', base, e)
        return None  # explicit recovery: both API+DOM exhausted upstream


def _scrape_woocommerce(store_url: str, query: str) -> List[ProductResult]:
    """Call the WooCommerce scrapers (API -> DOM) and normalize."""
    from .woocommerce import (  # type: ignore[no-redef]
        _normalize_base_url,
    )

    base = _normalize_base_url(store_url)
    products = _try_woocommerce_api(base)
    if not products:
        products = _try_woocommerce_dom(base)
    if not products:
        return []

    query_lower = query.lower()
    results: List[ProductResult] = []
    for p in products:
        name_lower = (p.name or '').lower()
        if not _woocommerce_filter_match(name_lower, query_lower):
            continue
        results.append(_woocommerce_product_to_result(p, store_url))
    return results


def _scrape_generic(store_url: str, query: str) -> List[ProductResult]:
    """Call the generic product scraper for unknown platforms."""
    from .generic_product_scraper import extract_products_generic

    try:
        raw: List[Dict[str, Any]] = extract_products_generic(
            store_url, query=query, limit=50,
        )
    except SCRAPE_FAILURE_EXCEPTIONS as e:
        logger.warning('Generic scrape failed for %s: %s', store_url, e)
        return []

    results: List[ProductResult] = []
    for p in raw:
        results.append(ProductResult(
            title=p.get('title') or '',
            price=_parse_price(p.get('price')),
            price_original=None,
            url=p.get('url') or store_url,
            store_url=store_url,
            platform='generic',
            image=p.get('image'),
            in_stock=True,
        ))
    return results


def _scrape_store(
    store_url: str, platform: str, query: str,
) -> List[ProductResult]:
    """Dispatch to the correct scraper by platform."""
    if platform == 'shopify':
        return _scrape_shopify(store_url, query)
    if platform == 'woocommerce':
        return _scrape_woocommerce(store_url, query)
    if platform == 'generic':
        return _scrape_generic(store_url, query)
    generic_results = _scrape_generic(store_url, query)
    return generic_results if generic_results else []


def _detect_platform_for_url(url: str) -> Dict[str, Any]:
    """Detect platform for a URL. Returns ``{platform, has_ecommerce}``."""
    from .platform_detect import detect_platform  # type: ignore[no-redef]  because local re-import shadows optional module-level binding

    try:
        result = detect_platform(url, timeout=3)
        platform = result.get('platform') or 'unknown'
        has_ecommerce = result.get('has_ecommerce', False)
        return {'platform': platform, 'has_ecommerce': has_ecommerce}
    except (*_NETWORK_EXC, *PARSE_EXCEPTIONS) as e:
        logger.warning('Platform detect failed for %s: %s', url, e)
        return {'platform': 'unknown', 'has_ecommerce': False}
