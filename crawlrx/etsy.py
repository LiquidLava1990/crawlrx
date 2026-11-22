"""etsy.py -- Etsy marketplace scraper.

Endpoints:
- POST /scrape/etsy/search  -- Search Etsy product listings
- POST /scrape/etsy/listing -- Scrape single product detail
- POST /scrape/etsy/shop    -- Scrape Etsy shop information

HTTP + BeautifulSoup with a Playwright fallback. Field-level extractors
live in :mod:`._etsy_helpers` so this file stays focused on the public
FastAPI surface + per-endpoint orchestration.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from fastapi import APIRouter
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel, Field

from ._etsy_helpers import (
    ETSY_BASE_URL,
    PARSE_EXC,
    SearchCardFields,
    build_search_url,
    card_to_fields,
    fetch_html,
    parse_listing_images,
    parse_listing_materials,
    parse_listing_reviews,
    parse_listing_tags,
    parse_listing_title,
    parse_shop_listings,
    select_cards,
    select_first_text,
    select_text,
    extract_int,
    extract_rating,
)
from ._scraper_helpers import SCRAPE_FAILURE_EXCEPTIONS
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.etsy")
router = APIRouter(prefix="/scrape/etsy", tags=["etsy"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class EtsyListing(BaseModel):
    """Single Etsy product listing from search."""
    title: str = Field(..., description="Product title")
    price: str = Field(..., description="Current price")
    original_price: Optional[str] = Field(default=None, description="Original price if on sale")
    shop_name: Optional[str] = Field(default=None, description="Shop/seller name")
    rating: Optional[float] = Field(default=None, description="Product rating (1-5)")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    listing_url: str = Field(..., description="URL to product detail page")
    image_url: Optional[str] = Field(default=None, description="Product image URL")
    free_shipping: Optional[bool] = Field(default=None, description="Whether free shipping is offered")
    made_to_order: Optional[bool] = Field(default=None, description="Whether item is made to order")


class EtsyReview(BaseModel):
    """Etsy product review."""
    author: Optional[str] = Field(default=None, description="Reviewer name")
    rating: Optional[int] = Field(default=None, description="Rating (1-5)")
    text: Optional[str] = Field(default=None, description="Review text")
    date: Optional[str] = Field(default=None, description="Review date")


class EtsyListingDetail(BaseModel):
    """Detailed Etsy listing information."""
    title: str = Field(..., description="Product title")
    description: Optional[str] = Field(default=None, description="Product description")
    price: Optional[str] = Field(default=None, description="Current price")
    original_price: Optional[str] = Field(default=None, description="Original price if on sale")
    images: List[str] = Field(default_factory=list, description="Product image URLs")
    shop_name: Optional[str] = Field(default=None, description="Shop name")
    shop_rating: Optional[float] = Field(default=None, description="Shop rating")
    shop_sales: Optional[int] = Field(default=None, description="Shop total sales")
    materials: List[str] = Field(default_factory=list, description="Materials used")
    shipping_info: Optional[str] = Field(default=None, description="Shipping information")
    returns_policy: Optional[str] = Field(default=None, description="Returns/exchanges policy")
    reviews: List[EtsyReview] = Field(default_factory=list, description="Top reviews")
    tags: List[str] = Field(default_factory=list, description="Product tags")


class EtsyShopListing(BaseModel):
    """Listing in a shop."""
    title: Optional[str] = Field(default=None, description="Listing title")
    price: Optional[str] = Field(default=None, description="Price")
    listing_url: Optional[str] = Field(default=None, description="Listing URL")
    image_url: Optional[str] = Field(default=None, description="Image URL")


class EtsyShopDetail(BaseModel):
    """Etsy shop information."""
    name: str = Field(..., description="Shop name")
    rating: Optional[float] = Field(default=None, description="Shop rating")
    review_count: Optional[int] = Field(default=None, description="Number of shop reviews")
    sales_count: Optional[int] = Field(default=None, description="Total sales")
    location: Optional[str] = Field(default=None, description="Shop location")
    about: Optional[str] = Field(default=None, description="Shop about/description")
    top_listings: List[EtsyShopListing] = Field(default_factory=list, description="Top 10 listings")
    policies: Optional[str] = Field(default=None, description="Shop policies")


class EtsySearchRequest(BaseModel):
    """Request model for POST /scrape/etsy/search."""
    query: str = Field(..., description="Search query")
    min_price: Optional[float] = Field(default=None, description="Minimum price")
    max_price: Optional[float] = Field(default=None, description="Maximum price")
    sort: str = Field(default="relevance", description="Sort order: 'relevance', 'price_asc', 'price_desc', 'newest'")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum listings to return")


class EtsySearchResponse(BaseModel):
    """Response model for POST /scrape/etsy/search."""
    success: bool
    listings: List[EtsyListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class EtsyListingRequest(BaseModel):
    """Request model for POST /scrape/etsy/listing."""
    url: str = Field(..., description="Full Etsy listing URL")


class EtsyListingResponse(BaseModel):
    """Response model for POST /scrape/etsy/listing."""
    success: bool
    listing: Optional[EtsyListingDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


class EtsyShopRequest(BaseModel):
    """Request model for POST /scrape/etsy/shop."""
    shop_name: str = Field(..., description="Etsy shop name (e.g., 'YourShopName')")


class EtsyShopResponse(BaseModel):
    """Response model for POST /scrape/etsy/shop."""
    success: bool
    shop: Optional[EtsyShopDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Stealth helper
# ─────────────────────────────────────────────

async def _random_delay(min_ms: int = 500, max_ms: int = 1200) -> None:
    """Sleep for a random duration between ``min_ms`` and ``max_ms``."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


# ─────────────────────────────────────────────
# Search (HTTP)
# ─────────────────────────────────────────────

def _fields_to_listing(fields: SearchCardFields) -> EtsyListing:
    """Convert a :class:`SearchCardFields` record into an ``EtsyListing``."""
    return EtsyListing(
        title=fields.title,
        price=fields.price or "",
        original_price=fields.original_price,
        shop_name=fields.shop_name,
        rating=fields.rating,
        review_count=fields.review_count,
        listing_url=fields.listing_url or "",
        image_url=fields.image_url,
        free_shipping=fields.free_shipping,
        made_to_order=fields.made_to_order,
    )


def _parse_search_listings(soup: BeautifulSoup, limit: int) -> List[EtsyListing]:
    """Walk Etsy search cards, drop unparseable ones, return up to ``limit``."""
    listings: List[EtsyListing] = []
    for item in select_cards(soup)[:limit]:
        try:
            fields = card_to_fields(item)
        except PARSE_EXC as exc:
            logger.debug("Error parsing Etsy listing (%s): %s", type(exc).__name__, exc)
            continue
        if fields is not None:
            listings.append(_fields_to_listing(fields))
    return listings


async def _scrape_etsy_search_http(
    query: str,
    min_price: Optional[float],
    max_price: Optional[float],
    sort: str,
    limit: int,
) -> Dict[str, Any]:
    """Scrape Etsy search using HTTP + BeautifulSoup."""
    start_time = time.time()
    url = build_search_url(query, min_price, max_price, sort)
    logger.info("Scraping Etsy search via HTTP: %s", url[:100])

    fetch = await asyncio.to_thread(fetch_html, url)
    if not fetch.ok:
        return {"success": False, "error": fetch.error, "listings": []}

    soup = BeautifulSoup(fetch.text, "html.parser")
    await _random_delay(300, 600)
    listings = _parse_search_listings(soup, limit)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info("Found %d Etsy listings via HTTP in %dms", len(listings), elapsed_ms)
    return {
        "success": True,
        "listings": listings,
        "count": len(listings),
        "elapsed_ms": elapsed_ms,
    }


# ─────────────────────────────────────────────
# Listing detail (HTTP)
# ─────────────────────────────────────────────

def _build_listing_detail(soup: BeautifulSoup) -> EtsyListingDetail:
    """Pull every detail field off a listing-page soup."""
    return EtsyListingDetail(
        title=parse_listing_title(soup),
        description=select_text(soup, "#listing-description, .listing-description, [data-description]"),
        price=select_first_text(
            soup,
            (
                ".wt-text-title-03.wt-text-slime",
                ".listing-price",
                "[data-buy-box-price]",
            ),
        ),
        original_price=select_first_text(
            soup, (".original-price", ".wt-text-strikethrough"),
        ),
        images=parse_listing_images(soup),
        shop_name=select_first_text(
            soup,
            (
                ".shop-name-and-title-container h2 a",
                ".shop-name a",
                "[data-shop-name]",
            ),
        ),
        shop_rating=extract_rating(
            select_first_text(soup, (".shop-rating", "[data-shop-rating]")),
        ),
        shop_sales=extract_int(
            select_first_text(soup, (".shop-sales", "[data-shop-sales]")),
        ),
        materials=parse_listing_materials(soup),
        shipping_info=select_first_text(soup, ("#shipping-section", ".shipping-info")),
        returns_policy=select_first_text(soup, ("#returns-exchanges-section", ".returns-policy")),
        reviews=[EtsyReview(**r) for r in parse_listing_reviews(soup, limit=5)],
        tags=parse_listing_tags(soup),
    )


def _scrape_etsy_listing_http(listing_url: str) -> Dict[str, Any]:
    """Scrape single Etsy listing using HTTP + BeautifulSoup."""
    start_time = time.time()
    logger.info("Scraping Etsy listing via HTTP: %s", listing_url[:100])

    fetch = fetch_html(listing_url)
    if not fetch.ok:
        return {"success": False, "error": fetch.error}

    soup = BeautifulSoup(fetch.text, "html.parser")
    detail = _build_listing_detail(soup)
    return {
        "success": True,
        "listing": detail,
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


# ─────────────────────────────────────────────
# Shop detail (HTTP)
# ─────────────────────────────────────────────

def _build_shop_detail(soup: BeautifulSoup, default_name: str) -> EtsyShopDetail:
    """Pull every shop field off a shop-page soup."""
    name = select_first_text(
        soup, ("h1.shop-name", ".shop-title h1", "[data-shop-name]"),
    ) or default_name
    return EtsyShopDetail(
        name=name,
        rating=extract_rating(
            select_first_text(soup, (".shop-rating", ".stars", "[data-rating]")),
        ),
        review_count=extract_int(
            select_first_text(soup, (".review-count", ".reviews-link")),
        ),
        sales_count=extract_int(
            select_first_text(soup, (".sales-count", "[data-sales]")),
        ),
        location=select_first_text(soup, (".shop-location", "[data-location]")),
        about=select_first_text(soup, (".shop-about", ".about-section", "[data-about]")),
        top_listings=[
            EtsyShopListing(**listing) for listing in parse_shop_listings(soup, limit=10)
        ],
        policies=select_first_text(soup, (".shop-policies", ".policies-section")),
    )


def _scrape_etsy_shop_http(shop_name: str) -> Dict[str, Any]:
    """Scrape Etsy shop via HTTP + BeautifulSoup."""
    start_time = time.time()
    shop_url = f"{ETSY_BASE_URL}/shop/{shop_name}"
    logger.info("Scraping Etsy shop via HTTP: %s", shop_url)

    fetch = fetch_html(shop_url)
    if not fetch.ok:
        return {"success": False, "error": fetch.error}

    soup = BeautifulSoup(fetch.text, "html.parser")
    return {
        "success": True,
        "shop": _build_shop_detail(soup, shop_name),
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


# ─────────────────────────────────────────────
# Search (Playwright fallback)
# ─────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


_SEARCH_PW_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-listing-id], .v2-listing-card, .listing-card');
    cards.forEach(card => {
        const titleEl = card.querySelector('h2 a, .listing-title a, .v2-listing-card__title');
        const priceEl = card.querySelector('.currency-value, .v2-listing-card__price');
        const shopEl = card.querySelector('.shop-name, .v2-listing-card__shop');
        const linkEl = card.querySelector('a[href*="/listing/"]');

        if (titleEl) {
            results.push({
                title: titleEl.textContent.trim(),
                price: priceEl ? priceEl.textContent.trim() : '',
                shop_name: shopEl ? shopEl.textContent.trim() : '',
                url: linkEl ? linkEl.href : ''
            });
        }
    });
    return results;
}
"""


async def _scrape_etsy_search_playwright(
    query: str,
    min_price: Optional[float],
    max_price: Optional[float],
    sort: str,
    limit: int,
) -> Dict[str, Any]:
    """Playwright CDP fallback for an Etsy search."""
    start_time = time.time()
    url = build_search_url(query, min_price, max_price, sort)
    logger.info("Scraping Etsy search via Playwright: %s", url[:100])

    browser = ctx = page = None
    try:
        async with async_playwright() as playwright_obj:
            browser, ctx, page = await make_stealth_context(playwright_obj)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(1500, 2500)
            raw = await page.evaluate(_SEARCH_PW_JS)
            listings = [
                EtsyListing(
                    title=data.get("title", ""),
                    price=data.get("price", ""),
                    shop_name=data.get("shop_name") or None,
                    listing_url=data.get("url", ""),
                )
                for data in raw[:limit]
            ]
            return {
                "success": True,
                "listings": listings,
                "count": len(listings),
                "elapsed_ms": int((time.time() - start_time) * 1000),
            }
    except (PlaywrightTimeoutError, *SCRAPE_FAILURE_EXCEPTIONS) as exc:
        logger.error("Playwright error scraping Etsy search (%s): %s", type(exc).__name__, exc)
        return {"success": False, "error": str(exc), "listings": []}
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/search", response_model=EtsySearchResponse)
async def search_etsy(request: EtsySearchRequest) -> EtsySearchResponse:
    """Search Etsy product listings."""
    result = await asyncio.wait_for(
        _scrape_etsy_search_http(
            request.query,
            request.min_price,
            request.max_price,
            request.sort,
            request.limit,
        ),
        timeout=45,
    )
    if not result.get("success"):
        logger.info("HTTP scrape failed, trying Playwright fallback for Etsy search")
        result = await asyncio.wait_for(
            _scrape_etsy_search_playwright(
                request.query,
                request.min_price,
                request.max_price,
                request.sort,
                request.limit,
            ),
            timeout=60,
        )
    return EtsySearchResponse(**result)


@router.post("/listing", response_model=EtsyListingResponse)
async def scrape_etsy_listing(request: EtsyListingRequest) -> EtsyListingResponse:
    """Scrape a single Etsy listing by URL."""
    result = await asyncio.wait_for(
        asyncio.to_thread(_scrape_etsy_listing_http, request.url),
        timeout=45,
    )
    return EtsyListingResponse(**result)


@router.post("/shop", response_model=EtsyShopResponse)
async def scrape_etsy_shop(request: EtsyShopRequest) -> EtsyShopResponse:
    """Scrape an Etsy shop by name."""
    result = await asyncio.wait_for(
        asyncio.to_thread(_scrape_etsy_shop_http, request.shop_name),
        timeout=45,
    )
    return EtsyShopResponse(**result)
