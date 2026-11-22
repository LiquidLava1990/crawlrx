# Browser: optional CDP port 9229 (see CRAWLRX_CDP_PORTS)
"""craigslist.py -- Craigslist listings scraper

Endpoints:
- POST /scrape/craigslist/search -- Search Craigslist listings
- POST /scrape/craigslist/listing -- Scrape single listing detail

Uses direct HTTP (httpx) -- Craigslist doesn't require JavaScript rendering.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from bs4 import BeautifulSoup

from ._scraper_helpers import SELECTOR_EXTRACTION_EXCEPTIONS

logger = logging.getLogger("crawlrx.craigslist")
router = APIRouter(prefix="/scrape/craigslist", tags=["craigslist"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class CraigslistListing(BaseModel):
    """Single Craigslist listing"""
    title: str
    price: Optional[str] = None
    location: Optional[str] = None
    url: str
    posted_date: Optional[str] = None
    thumbnail_url: Optional[str] = None


class CraigslistListingDetail(BaseModel):
    """Detailed Craigslist listing"""
    title: str
    price: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    posted_date: Optional[str] = None
    attributes: Dict[str, str] = Field(default_factory=dict)


class CraigslistSearchRequest(BaseModel):
    """Request model for POST /scrape/craigslist/search"""
    query: str = Field(..., description="Search query")
    category: str = Field(default="sss", description="Category code (sss=for sale, hhh=housing, etc.)")
    city: str = Field(default="louisville", description="Craigslist city subdomain")
    max_price: Optional[int] = Field(default=None, description="Maximum price filter")
    min_price: Optional[int] = Field(default=None, description="Minimum price filter")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum listings to return")


class CraigslistSearchResponse(BaseModel):
    """Response model for POST /scrape/craigslist/search"""
    success: bool
    listings: List[CraigslistListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class CraigslistListingRequest(BaseModel):
    """Request model for POST /scrape/craigslist/listing"""
    url: str = Field(..., description="Full Craigslist listing URL")


class CraigslistListingResponse(BaseModel):
    """Response model for POST /scrape/craigslist/listing"""
    success: bool
    listing: Optional[CraigslistListingDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 30.0
_HTTP_HEADERS = {"User-Agent": USER_AGENT}

T = TypeVar("T")


def _build_search_url(
    city: str,
    category: str,
    query: str,
    min_price: Optional[int],
    max_price: Optional[int],
) -> str:
    """Build Craigslist search URL with filters."""
    base_url = f"https://{city}.craigslist.org/search/{category}"
    params = ["?sort=date", f"&query={query.replace(' ', '+')}"]
    if min_price is not None:
        params.append(f"&min_price={min_price}")
    if max_price is not None:
        params.append(f"&max_price={max_price}")
    return base_url + "".join(params)


def _extract_price(price_text: str) -> Optional[str]:
    """Extract price from text like '$1,200'."""
    if not price_text:
        return None
    match = re.search(r'\$[\d,]+', price_text)
    return match.group(0) if match else None


def _try_strptime(date_text: str, fmt: str, current_year: Optional[int]) -> Optional[str]:
    """Best-effort ``strptime`` returning ISO 8601 (or ``None`` on mismatch)."""
    try:
        dt = datetime.strptime(date_text, fmt)
    except ValueError:
        return None
    if current_year is not None:
        dt = dt.replace(year=current_year)
    return dt.isoformat()


def _parse_date(date_text: str) -> Optional[str]:
    """Parse Craigslist short date strings to ISO 8601 (best-effort)."""
    if not date_text:
        return None
    date_text = date_text.strip()
    iso = _try_strptime(date_text, "%b %d, %Y", current_year=None)
    if iso is not None:
        return iso
    iso = _try_strptime(date_text, "%b %d", current_year=datetime.now(tz=timezone.utc).year)
    if iso is not None:
        return iso
    return date_text


def _absolute_href(href: str, base_url: str) -> str:
    """Coerce a relative/protocol-relative ``href`` into a full URL."""
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("http"):
        return urljoin(base_url, href)
    return href


def _row_title(row: Any, link_el: Any) -> str:
    """Pick the best title text from a search-result row."""
    title_el = row.select_one(".title")
    if title_el:
        return title_el.get_text(strip=True)
    return row.get("title") or link_el.get_text(strip=True)


def _row_posted_date(row: Any) -> Optional[str]:
    """Extract the ``time`` element's ISO datetime (or parsed text fallback)."""
    date_el = row.select_one("time")
    if not date_el:
        return None
    return date_el.get("datetime") or _parse_date(date_el.get_text(strip=True))


def _parse_search_row(row: Any, base_url: str) -> Optional[CraigslistListing]:
    """Convert one search-result row into a :class:`CraigslistListing` (or None)."""
    link_el = row.select_one("a")
    if not link_el:
        return None
    href = _absolute_href(link_el.get("href", ""), base_url)
    title = _row_title(row, link_el)
    if not title:
        return None
    price_el = row.select_one(".price, .priceinfo")
    price = _extract_price(price_el.get_text(strip=True)) if price_el else None
    location_el = row.select_one(".location, .result-hood, .supertitle")
    location = location_el.get_text(strip=True).strip("()") if location_el else None
    thumb_el = row.select_one("img")
    thumbnail_url = thumb_el.get("src") if thumb_el else None
    return CraigslistListing(
        title=title,
        price=price,
        location=location,
        url=href,
        posted_date=_row_posted_date(row),
        thumbnail_url=thumbnail_url,
    )


def _safe_parse_row(row: object, base_url: str) -> Optional[CraigslistListing]:
    """Parse a single row, returning None if the row is malformed."""
    try:
        return _parse_search_row(row, base_url)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.warning(
            "Failed to parse listing row (%s): %s",
            type(exc).__name__,
            exc,
        )
        # explicit recovery: SELECTOR_EXTRACTION_EXCEPTIONS is non-fatal; caller skips row
        return None


def _parse_search_listings(html: str, base_url: str, limit: int) -> List[CraigslistListing]:
    """Parse search results page and extract listings."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("li.cl-static-search-result, .result-row")[:limit]
    listings: List[CraigslistListing] = []
    for row in rows:
        listing = _safe_parse_row(row, base_url)
        if listing is not None:
            listings.append(listing)
    return listings


def _detail_description(soup: BeautifulSoup) -> Optional[str]:
    """Extract the listing description with QR-code notices stripped."""
    desc_el = soup.select_one("section#postingbody, #postingbody")
    if not desc_el:
        return None
    for notice in desc_el.select(".print-information, .print-qrcode-container"):
        notice.decompose()
    return desc_el.get_text(separator="\n", strip=True)


def _detail_images(soup: BeautifulSoup) -> List[str]:
    """Collect deduped full-size image URLs from the listing gallery."""
    images: List[str] = []
    for img in soup.select(".swipe-wrap img, .gallery img, .thumb img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        full_size = src.replace("50x50c", "600x450").replace("300x300", "600x450")
        if full_size not in images:
            images.append(full_size)
    return images


def _detail_posted_date(soup: BeautifulSoup) -> Optional[str]:
    """First ``datetime`` attribute from any time tag we know about."""
    for selector in ("time.date.timeago", ".postinginfo time"):
        elem = soup.select_one(selector)
        if elem and elem.get("datetime"):
            return elem.get("datetime")
    return None


def _detail_location(soup: BeautifulSoup) -> Optional[str]:
    """First location string from the detail page (paren-stripped)."""
    location_el = soup.select_one(".mapaddress, .posting-viewer-map")
    if not location_el:
        location_el = soup.select_one(".postingtitletext small")
    return location_el.get_text(strip=True).strip("()") if location_el else None


def _detail_attributes(soup: BeautifulSoup) -> Dict[str, str]:
    """Parse the attribute group spans into a flat key/value map."""
    attributes: Dict[str, str] = {}
    for attr in soup.select(".attrgroup span, .mapAndAttrs .attrgroup span"):
        text = attr.get_text(strip=True)
        if ":" in text:
            key, value = text.split(":", 1)
            attributes[key.strip()] = value.strip()
        elif text:
            attributes["detail"] = text
    return attributes


def _parse_listing_detail(html: str, url: str) -> CraigslistListingDetail:
    """Parse individual listing page for full details."""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("span#titletextonly, h1.postingtitle")
    title = title_el.get_text(strip=True) if title_el else "Unknown"
    price_el = soup.select_one("span.price, .price")
    price = _extract_price(price_el.get_text(strip=True)) if price_el else None
    return CraigslistListingDetail(
        title=title,
        price=price,
        description=_detail_description(soup),
        location=_detail_location(soup),
        images=_detail_images(soup),
        posted_date=_detail_posted_date(soup),
        attributes=_detail_attributes(soup),
    )


async def _http_get_text(url: str) -> str:
    """``GET url`` via httpx; returns response body or raises HTTPStatusError."""
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        headers=_HTTP_HEADERS,
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


_HTTP_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    OSError,
    ValueError,
    KeyError,
)


async def _do_search(request: CraigslistSearchRequest, start_time: float) -> CraigslistSearchResponse:
    """Run the search request → response build inside the timing window."""
    search_url = _build_search_url(
        request.city,
        request.category,
        request.query,
        request.min_price,
        request.max_price,
    )
    logger.info("Searching Craigslist: %s", search_url)
    html = await _http_get_text(search_url)
    listings = _parse_search_listings(html, search_url, request.limit)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("Found %d Craigslist listings in %dms", len(listings), elapsed)
    return CraigslistSearchResponse(
        success=True,
        listings=listings,
        count=len(listings),
        elapsed_ms=elapsed,
    )


async def _do_listing(request: CraigslistListingRequest, start_time: float) -> CraigslistListingResponse:
    """Run the listing-detail request → response build inside the timing window."""
    logger.info("Scraping Craigslist listing: %s", request.url)
    html = await _http_get_text(request.url)
    listing = _parse_listing_detail(html, request.url)
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("Scraped listing '%s' in %dms", listing.title[:40], elapsed)
    return CraigslistListingResponse(
        success=True,
        listing=listing,
        elapsed_ms=elapsed,
    )


def _format_http_status_error(exc: httpx.HTTPStatusError) -> str:
    """Stable error string for an HTTP failure (used by both endpoints)."""
    return "HTTP %d: %s" % (exc.response.status_code, str(exc))


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

async def _run_with_error_capture(
    *,
    label: str,
    body: Callable[[float], Awaitable[T]],
    on_error: Callable[[str, int], T],
) -> T:
    """Time + run ``body``; map known HTTP/parse failures through ``on_error``."""
    start_time = time.time()
    try:
        return await body(start_time)
    except httpx.HTTPStatusError as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("HTTP error %s: %s", label, exc)
        return on_error(_format_http_status_error(exc), elapsed)
    except _HTTP_FAILURE_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Error %s (%s): %s", label, type(exc).__name__, exc)
        return on_error(str(exc), elapsed)


def _search_failure(error_message: str, elapsed: int) -> CraigslistSearchResponse:
    """Build a search failure response with empty result list."""
    return CraigslistSearchResponse(
        success=False,
        listings=[],
        count=0,
        error=error_message,
        elapsed_ms=elapsed,
    )


def _listing_failure(error_message: str, elapsed: int) -> CraigslistListingResponse:
    """Build a listing-detail failure response."""
    return CraigslistListingResponse(
        success=False,
        error=error_message,
        elapsed_ms=elapsed,
    )


@router.post(
    "/search",
    response_model=CraigslistSearchResponse,
    summary="Search Craigslist listings"
)
async def search_craigslist(request: CraigslistSearchRequest) -> CraigslistSearchResponse:
    """Search Craigslist listings by query and filters."""
    return await _run_with_error_capture(
        label="searching Craigslist",
        body=lambda start: _do_search(request, start),
        on_error=_search_failure,
    )


@router.post(
    "/listing",
    response_model=CraigslistListingResponse,
    summary="Scrape Craigslist listing detail"
)
async def scrape_craigslist_listing(request: CraigslistListingRequest) -> CraigslistListingResponse:
    """Scrape detailed info from a single Craigslist listing URL."""
    return await _run_with_error_capture(
        label="scraping Craigslist listing",
        body=lambda start: _do_listing(request, start),
        on_error=_listing_failure,
    )
