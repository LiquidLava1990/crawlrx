"""salesforce_scraper.py -- Salesforce Commerce Cloud (Demandware) scraper.

Public surface:
  detect_salesforce(url, headers, html) -> bool
  scrape_salesforce_products(url) -> list[dict]
  POST /scrape/salesforce
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel

from ._salesforce_parsers import (
    HTML_PARSE_EXCEPTIONS,
    extract_site_id,
    parse_api_products,
    parse_html_cards,
)
from ._scraper_helpers import network_exception_types
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.salesforce")

_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_NETWORK_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    *network_exception_types(),
    requests.RequestException,
)


# ---------------------------------------------------------------------------
# detect_salesforce
# ---------------------------------------------------------------------------

def _url_indicates_salesforce(parsed_url: Any) -> bool:
    """True if the parsed URL netloc/path mentions ``demandware``."""
    return (
        "demandware" in parsed_url.netloc.lower()
        or "demandware" in parsed_url.path.lower()
    )


def _has_dw_header(headers: Any) -> bool:
    """True if any header name starts with ``x-dw-`` (case-insensitive)."""
    return any(name.lower().startswith("x-dw-") for name in headers)


_HTML_NEEDLES_LOWER: Tuple[str, ...] = (
    "demandware",
    'name="salesforce commerce cloud"',
    'content="salesforce commerce cloud"',
)


def _html_indicates_salesforce(html: str) -> bool:
    """True if the HTML body contains any Salesforce signature."""
    lower = html.lower()
    if any(needle in lower for needle in _HTML_NEEDLES_LOWER):
        return True
    return "dw." in html


def _has_dwsid_cookie(cookies: Any) -> bool:
    """True if any cookie's name (uppercased) is ``DWSID``."""
    return any(cookie.name.upper() == "DWSID" for cookie in cookies)


def _detect_via_inputs(
    url: str, headers: Optional[Dict[str, str]], html: Optional[str]
) -> bool:
    """Cheap detection from already-known headers / html / URL shape."""
    if _url_indicates_salesforce(urlparse(url)):
        logger.info("Salesforce detected via URL at %s", url)
        return True
    if headers and _has_dw_header(headers):
        logger.info("Salesforce detected via x-dw- header at %s", url)
        return True
    if html and _html_indicates_salesforce(html):
        logger.info("Salesforce detected via HTML signature at %s", url)
        return True
    return False


def _detect_via_probe(url: str) -> bool:
    """Issue a GET and check headers, cookies, and body for SF markers."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
    except _NETWORK_EXCEPTIONS as exc:
        logger.warning("detect_salesforce probe failed for %s: %s", url, exc)
        return False
    if _has_dw_header(resp.headers):
        logger.info("Salesforce detected via response x-dw- header at %s", url)
        return True
    if _has_dwsid_cookie(resp.cookies):
        logger.info("Salesforce detected via DWSID cookie at %s", url)
        return True
    if _html_indicates_salesforce(resp.text):
        logger.info("Salesforce detected via response body at %s", url)
        return True
    return False


def detect_salesforce(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    html: Optional[str] = None,
) -> bool:
    """Return True if the site looks like Salesforce Commerce Cloud."""
    if _detect_via_inputs(url, headers, html):
        return True
    return _detect_via_probe(url)


# ---------------------------------------------------------------------------
# API-driven scrapers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> Optional[Any]:
    """GET ``url`` and return decoded JSON, or ``None`` on any failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8)
    except _NETWORK_EXCEPTIONS as exc:
        logger.debug("Salesforce HTTP error for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except (ValueError, TypeError) as exc:
        logger.debug("Salesforce JSON decode failed for %s: %s", url, exc)
        return None


def _scrape_salesforce_ajax(base: str, site_id: str) -> List[Dict[str, Any]]:
    """Try the Demandware Search-Show AJAX endpoint."""
    url = (
        f"{base}/on/demandware.store/Sites-{site_id}-Site/default/"
        f"Search-Show?format=ajax&sz=50"
    )
    data = _fetch_json(url)
    results = parse_api_products(data, base, ("productResults", "results"))
    if results:
        logger.info(
            "Salesforce AJAX API returned %d products from %s",
            len(results),
            base,
        )
    return results


def _scrape_salesforce_ocapi(base: str, site_id: str) -> List[Dict[str, Any]]:
    """Try the OCAPI ``/dw/shop/v22_4/product_search`` endpoint."""
    url = f"{base}/s/{site_id}/dw/shop/v22_4/product_search?count=50&start=0"
    data = _fetch_json(url)
    results = parse_api_products(data, base, ("hits", "products"))
    if results:
        logger.info(
            "Salesforce OCAPI returned %d products from %s",
            len(results),
            base,
        )
    return results


# ---------------------------------------------------------------------------
# Playwright fallback
# ---------------------------------------------------------------------------

async def _scrape_salesforce_playwright(url: str) -> List[Dict[str, Any]]:
    """Playwright fallback: navigate the storefront and parse the grid."""
    browser = ctx = page = None
    html = ""
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            try:
                await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                html = await page.content()
            except PlaywrightTimeoutError as exc:
                logger.warning(
                    "Salesforce Playwright timeout for %s: %s", url, exc
                )
                return []
    except _NETWORK_EXCEPTIONS as exc:
        logger.error("Salesforce Playwright fallback failed for %s: %s", url, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)

    if not html:
        return []
    try:
        results = parse_html_cards(html, url)
    except HTML_PARSE_EXCEPTIONS as exc:
        logger.warning("Salesforce HTML parse error for %s: %s", url, exc)
        return []
    if results:
        logger.info(
            "Salesforce Playwright returned %d products from %s",
            len(results),
            url,
        )
    return results


# ---------------------------------------------------------------------------
# scrape_salesforce_products
# ---------------------------------------------------------------------------

def _try_apis(base: str, site_id: str) -> List[Dict[str, Any]]:
    """Try AJAX then OCAPI for ``site_id``, returning the first non-empty hit."""
    for fetcher in (_scrape_salesforce_ajax, _scrape_salesforce_ocapi):
        results = fetcher(base, site_id)
        if results:
            return results
    return []


def scrape_salesforce_products(url: str) -> List[Dict[str, Any]]:
    """Scrape products from a Salesforce Commerce Cloud store via API only."""
    base = url.rstrip("/")
    site_id = extract_site_id(url)
    results = _try_apis(base, site_id)
    if results:
        return results
    if site_id != "default":
        results = _try_apis(base, "default")
        if results:
            return results
    logger.info(
        "Salesforce APIs yielded no results for %s -- caller may run Playwright fallback",
        url,
    )
    return []


async def scrape_salesforce_products_async(url: str) -> List[Dict[str, Any]]:
    """Async variant that falls back to Playwright when the APIs miss."""
    results = scrape_salesforce_products(url)
    if results:
        return results
    return await _scrape_salesforce_playwright(url)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/scrape/salesforce", tags=["scrape"])


class SalesforceScrapeRequest(BaseModel):
    """Request body for ``POST /scrape/salesforce``."""

    url: str


class SalesforceScrapeResponse(BaseModel):
    """Response body for ``POST /scrape/salesforce``."""

    success: bool
    products: List[Dict[str, Any]]
    count: int
    error: Optional[str] = None


def _not_a_salesforce_response() -> SalesforceScrapeResponse:
    """Return the canonical ``not a Salesforce site`` failure payload."""
    return SalesforceScrapeResponse(
        success=False,
        products=[],
        count=0,
        error="URL does not appear to be a Salesforce Commerce Cloud store",
    )


@router.post("", response_model=SalesforceScrapeResponse)
async def scrape_salesforce_endpoint(
    req: SalesforceScrapeRequest,
) -> SalesforceScrapeResponse:
    """Scrape products from a Salesforce Commerce Cloud store."""
    try:
        if not detect_salesforce(req.url):
            return _not_a_salesforce_response()
        products = await scrape_salesforce_products_async(req.url)
    except _NETWORK_EXCEPTIONS as exc:
        logger.error("Salesforce scrape endpoint error: %s", exc)
        return SalesforceScrapeResponse(
            success=False, products=[], count=0, error=str(exc),
        )
    return SalesforceScrapeResponse(
        success=True, products=products, count=len(products),
    )
