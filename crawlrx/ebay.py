"""ebay.py -- eBay listing scraper.

Endpoints:
  POST /scrape/ebay/search -- Search eBay listings
  POST /scrape/ebay/item   -- Scrape single eBay item

HTTP + BeautifulSoup with a Playwright fallback. Card / detail / fallback
field extractors live in :mod:`._ebay_helpers` so this file stays focused
on orchestration + the public router surface.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from fastapi import APIRouter
from playwright.async_api import (
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from ._ebay_helpers import (
    CardFields,
    HttpFetchResult,
    _safe_inner_text,
    PARSE_EXC,
    SELECTOR_EXC,
    build_search_url,
    card_to_fields,
    extract_item_id,
    http_get_text,
    parse_detail_images,
    parse_detail_price,
    parse_detail_seller,
    parse_detail_sold_count,
    parse_detail_specs,
    parse_detail_text,
    parse_detail_title,
    parse_detail_watchers,
)
from .models import (
    EbayItemRequest, EbayItemResponse,
    EbayListing, EbaySearchRequest, EbaySearchResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.ebay")
router = APIRouter(prefix="/scrape/ebay", tags=["ebay"])


# ─────────────────────────────────────────────
# Search (HTTP)
# ─────────────────────────────────────────────

_CARD_SELECTORS = (
    "li.s-card",
    ".s-item",
    ".srp-results .s-item__wrapper",
)


def _select_cards(soup: BeautifulSoup) -> List[Any]:
    """Return the first non-empty card list, walking the selector ladder."""
    for selector in _CARD_SELECTORS:
        items = soup.select(selector)
        if items:
            return items
    return []


def _card_to_listing(fields: CardFields) -> EbayListing:
    """Convert a :class:`CardFields` record into an ``EbayListing`` row."""
    return EbayListing(
        title=fields.title,
        price=fields.price or "",
        condition=fields.condition,
        shipping=fields.shipping,
        location=fields.location,
        seller_name=fields.seller_name,
        seller_rating=fields.seller_rating,
        listing_url=fields.listing_url or "",
        image_url=fields.image_url,
        bids=fields.bids,
        time_left=fields.time_left,
        item_id=fields.item_id,
    )


def _parse_search_listings(soup: BeautifulSoup, limit: int) -> List[EbayListing]:
    """Walk search cards and build :class:`EbayListing` rows."""
    listings: List[EbayListing] = []
    for item in _select_cards(soup)[:limit]:
        try:
            fields = card_to_fields(item)
        except PARSE_EXC as exc:
            logger.debug("Error parsing eBay item (%s): %s", type(exc).__name__, exc)
            continue
        if fields is not None:
            listings.append(_card_to_listing(fields))
    return listings


def _scrape_ebay_search_http(params: Dict[str, Any]) -> Dict[str, Any]:
    """Scrape eBay search using HTTP + BeautifulSoup."""
    start_time = time.time()
    url = build_search_url(params)
    logger.info("Scraping eBay search via HTTP: %s", url)

    fetch: HttpFetchResult = http_get_text(url, timeout=30.0)
    if not fetch.ok:
        return {"success": False, "error": fetch.error, "listings": []}

    soup = BeautifulSoup(fetch.text, "html.parser")
    listings = _parse_search_listings(soup, params.get("limit", 20))
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info("Found %d eBay listings in %dms", len(listings), elapsed_ms)
    return {
        "success": True,
        "listings": listings,
        "count": len(listings),
        "elapsed_ms": elapsed_ms,
    }


# ─────────────────────────────────────────────
# Item detail (HTTP)
# ─────────────────────────────────────────────

def _build_item_payload(soup: BeautifulSoup) -> Dict[str, Any]:
    """Pull every field off an item detail soup into a flat dict."""
    seller = parse_detail_seller(soup)
    return {
        "title": parse_detail_title(soup),
        "price": parse_detail_price(soup),
        "condition": parse_detail_text(
            soup,
            '[data-testid="x-item-condition-label"], #vi-itm-cond, .u-flL.condText',
        ),
        "description": parse_detail_text(
            soup,
            '#viTabs_0_is, .item-description, [data-testid="x-item-description"]',
        ),
        "seller": seller or None,
        "shipping_options": _parse_shipping_options(soup),
        "images": parse_detail_images(soup, limit=10),
        "specs": parse_detail_specs(soup),
        "sold_count": parse_detail_sold_count(soup),
        "watchers": parse_detail_watchers(soup),
    }


def _parse_shipping_options(soup: BeautifulSoup) -> List[str]:
    """Collect shipping-section text into a single-element list."""
    text = parse_detail_text(
        soup, '#vi-acc-shpsToLbl-cnt, [data-testid="x-shipping-section"]',
    )
    return [text] if text else []


def _scrape_ebay_item_http(item_url: str) -> Dict[str, Any]:
    """Scrape single eBay item via HTTP + BeautifulSoup."""
    start_time = time.time()
    item_id = extract_item_id(item_url)
    logger.info("Scraping eBay item via HTTP: %s (ID: %s)", item_url, item_id)

    fetch = http_get_text(item_url, timeout=30.0)
    if not fetch.ok:
        return {"success": False, "error": fetch.error}

    soup = BeautifulSoup(fetch.text, "html.parser")
    payload = _build_item_payload(soup)
    payload.update(
        success=True,
        url=item_url,
        item_id=item_id,
        elapsed_ms=int((time.time() - start_time) * 1000),
    )
    return payload


# ─────────────────────────────────────────────
# Item detail (Playwright fallback)
# ─────────────────────────────────────────────

_PW_FIELDS = (
    ("title", 'h1[data-testid="x-item-title-label"], #itemTitle'),
    ("price", '[data-testid="x-price-primary"], #prcIsum'),
    ("condition", '[data-testid="x-item-condition-label"], #vi-itm-cond'),
    ("description", '#viTabs_0_is, [data-testid="x-item-description"]'),
)


async def _extract_pw_seller(page: Page) -> Optional[Dict[str, str]]:
    """Best-effort seller info from the Playwright detail page."""
    text = await _safe_inner_text(page, '[data-testid="x-seller-section"]')
    return {"info": text} if text else None


async def _extract_pw_fields(page: Page) -> Dict[str, Any]:
    """Concurrent inner_text for each declared selector pair."""
    out: Dict[str, Any] = {}
    for key, selector in _PW_FIELDS:
        out[key] = await _safe_inner_text(page, selector)
    out["seller"] = await _extract_pw_seller(page)
    return out


def _empty_pw_payload(item_url: str, item_id: Optional[str], elapsed_ms: int) -> Dict[str, Any]:
    """Default payload shape for a successful Playwright scrape."""
    return {
        "title": None,
        "price": None,
        "condition": None,
        "description": None,
        "seller": None,
        "shipping_options": [],
        "images": [],
        "specs": {},
        "sold_count": None,
        "watchers": None,
        "url": item_url,
        "item_id": item_id,
        "elapsed_ms": elapsed_ms,
        "success": True,
    }


async def _scrape_ebay_item_playwright(
    item_url: str, timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Playwright CDP fallback for a single eBay item."""
    start_time = time.time()
    item_id = extract_item_id(item_url)
    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await make_stealth_context(playwright_obj)
            logger.info("Navigating to eBay item via Playwright: %s", item_url)
            await page.goto(item_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(2000)

            payload = _empty_pw_payload(
                item_url,
                item_id,
                int((time.time() - start_time) * 1000),
            )
            payload.update(await _extract_pw_fields(page))
            payload["elapsed_ms"] = int((time.time() - start_time) * 1000)
            return payload
    except PlaywrightTimeoutError as exc:
        logger.error("Timeout scraping eBay item: %s", exc)
        return {
            "success": False,
            "error": f"Timeout: {exc}",
            "url": item_url,
            "item_id": item_id,
            "elapsed_ms": int((time.time() - start_time) * 1000),
        }
    except SELECTOR_EXC as exc:
        logger.error("Selector error scraping eBay item: %s", exc)
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "url": item_url,
            "item_id": item_id,
            "elapsed_ms": int((time.time() - start_time) * 1000),
        }
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Search (Playwright fallback)
# ─────────────────────────────────────────────

_SEARCH_PW_JS = """
(limit) => {
    const cards = Array.from(document.querySelectorAll('ul.srp-results > li.s-card, ul.srp-results > li.s-item'));
    return cards.slice(0, limit).map(el => {
        const rawText = el.innerText || '';
        const titleLines = rawText.split('\\n').filter(l => l.trim() && !l.includes('Opens in a new'));
        const title = titleLines[0] ? titleLines[0].replace(/^NEW LISTING/, '').trim() : '';
        if (!title || title === 'Shop on eBay') return null;

        const priceEl = el.querySelector('[class*="price"]');
        const linkEl = el.querySelector('a[href*="/itm/"], a[href*="ebay.com"]');
        const imgEl = el.querySelector('img');
        return {
            title: title,
            price: priceEl ? priceEl.textContent.trim() : '',
            listing_url: linkEl ? linkEl.href : '',
            image_url: imgEl ? (imgEl.src || null) : null,
            condition: null, shipping: null, location: null,
            seller_name: null, seller_rating: null, bids: null, time_left: null, item_id: null,
        };
    }).filter(Boolean);
}
"""


def _hydrate_pw_card(item: Dict[str, Any]) -> EbayListing:
    """Fill in ``item_id`` from the listing URL and build an ``EbayListing``."""
    listing_url = item.get("listing_url") or ""
    if listing_url:
        match = re.search(r"/itm/(\d+)", listing_url)
        if match:
            item["item_id"] = match.group(1)
    return EbayListing(**item)


async def _scrape_ebay_search_playwright(params: dict) -> dict:
    """Playwright CDP fallback for an eBay search."""
    start_time = time.time()
    url = build_search_url(params)
    limit = params.get("limit", 20)
    listings: List[EbayListing] = []

    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await make_stealth_context(playwright_obj)
            logger.info("[eBay] Playwright search: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector("li.s-card, li.s-item", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            raw = await page.evaluate(_SEARCH_PW_JS, limit)
            for item in raw:
                listings.append(_hydrate_pw_card(item))
    except (PlaywrightTimeoutError, *SELECTOR_EXC) as exc:
        logger.error("[eBay] Playwright search error (%s): %s", type(exc).__name__, exc)
        return None  # explicit recovery: non-fatal: (PlaywrightTimeoutError, *SELECTOR_EXC) is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)

    return {
        "success": True,
        "listings": listings,
        "count": len(listings),
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

def _request_to_params(request: EbaySearchRequest) -> Dict[str, Any]:
    """Translate the FastAPI request into the param dict used downstream."""
    return {
        "query": request.query,
        "condition": request.condition,
        "min_price": request.min_price,
        "max_price": request.max_price,
        "listing_type": request.listing_type,
        "limit": request.limit,
        "sort": request.sort,
    }


@router.post("/search", response_model=EbaySearchResponse)
async def search_ebay(request: EbaySearchRequest) -> EbaySearchResponse:
    """Search eBay listings with HTTP scraper + Playwright fallback."""
    params = _request_to_params(request)

    result = await asyncio.wait_for(
        asyncio.to_thread(_scrape_ebay_search_http, params),
        timeout=45,
    )

    if result.get("success") and result.get("count", 0) == 0:
        logger.info("[eBay] HTTP returned 0 listings, trying Playwright fallback")
        result = await asyncio.wait_for(
            _scrape_ebay_search_playwright(params),
            timeout=60,
        )

    return EbaySearchResponse(**result)


def _resolve_item_url(request: EbayItemRequest) -> Optional[str]:
    """Pick the explicit URL or build one from the item id."""
    if request.url:
        return request.url
    if request.item_id:
        return f"https://www.ebay.com/itm/{request.item_id}"
    return None


@router.post("/item", response_model=EbayItemResponse)
async def scrape_ebay_item(request: EbayItemRequest) -> EbayItemResponse:
    """Scrape a single eBay item by URL or item ID."""
    item_url = _resolve_item_url(request)
    if not item_url:
        return EbayItemResponse(
            success=False,
            error="Either url or item_id is required",
            elapsed_ms=0,
        )

    result = await asyncio.wait_for(
        asyncio.to_thread(_scrape_ebay_item_http, item_url),
        timeout=30,
    )
    if not result.get("success"):
        logger.info("HTTP scrape failed, trying Playwright fallback for: %s", item_url)
        result = await asyncio.wait_for(
            _scrape_ebay_item_playwright(item_url, 30000),
            timeout=45,
        )
    return EbayItemResponse(**result)
