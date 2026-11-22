"""google_maps.py -- Google Maps scraper (new endpoints)
Router prefix: /scrape/google_maps

1. POST /scrape/google_maps/search -- search Google Maps for businesses/places
2. POST /scrape/google_maps/place -- full detail for a single place

Uses Playwright CDP 9229. Google Maps is JS-heavy.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from ._google_maps_helpers import (
    PLACE_EXTRACTION_EXCEPTIONS,
    extract_coordinates_from_url,
    extract_place_card,
    get_place_address,
    get_place_hours,
    get_place_name,
    get_place_open_now,
    get_place_phone,
    get_place_rating,
    get_place_review_count,
    get_place_reviews,
    get_place_website,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .models import (
    GoogleMapsPlace,
    GoogleMapsPlaceDetail,
    GoogleMapsPlaceRequest,
    GoogleMapsPlaceResponse,
    GoogleMapsPlaceReview,
    GoogleMapsSearchRequest,
    GoogleMapsSearchResponse,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.google_maps")
router = APIRouter(prefix="/scrape/google_maps", tags=["google_maps"])


_CARD_SELECTORS: tuple[str, ...] = (
    "div[data-result-index]",
    "div[role='article']",
    "div[data-async-context]",
    ".bfdHYd",
    ".THOPZb",
)


def _build_search_query(query: str, location: Optional[str], place_type: str) -> str:
    """Compose the user query string Google Maps will search for."""
    search_query = query
    if location:
        search_query = f"{query} in {location}"
    if place_type != "any":
        search_query = f"{place_type} {search_query}"
    return search_query


async def _scroll_results(page: Any, limit: int) -> None:
    """Scroll the results panel a handful of times to load more cards."""
    scroll_count = min(limit // 3 + 1, 5)
    for _ in range(scroll_count):
        try:
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(800)
        except PLACE_EXTRACTION_EXCEPTIONS:
            return


async def _find_place_cards(page: Any) -> list:
    """Try the known result selectors until one returns at least one card."""
    for selector in _CARD_SELECTORS:
        cards = await page.query_selector_all(selector)
        if cards:
            return cards
    return []


async def _wait_for_main_panel(page: Any, timeout_ms: int) -> None:
    """Wait for the Maps `[role='main']` panel, swallowing timeout."""
    try:
        await page.wait_for_selector("[role='main']", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def _collect_place_dicts(cards: list, page: Any, fallback_url: str, limit: int) -> List[Dict[str, Any]]:
    """Walk the result cards, extract dicts, dedup-skip on errors."""
    places: List[Dict[str, Any]] = []
    for card in cards[:limit]:
        try:
            place = await extract_place_card(card, page, fallback_url)
        except PLACE_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Error extracting place card: %s", exc)
            continue
        if place is not None:
            places.append(place)
    return places


def _search_failure(query: str, exc: BaseException, label: str, elapsed_ms: int) -> Dict[str, Any]:
    """Build the standard failure-result dict for a search-side exception."""
    logger.error("%s Google Maps for '%s': %s", label, query, exc)
    return {
        "success": False,
        "places": [],
        "count": 0,
        "error": f"{label}: {exc}" if label == "Timeout" else str(exc),
        "elapsed_ms": elapsed_ms,
    }


async def _execute_maps_search(query: str, location: Optional[str], limit: int, place_type: str) -> Dict[str, Any]:
    """Drive the Maps search inside the Playwright context."""
    browser = None
    ctx = None
    page = None
    start_time = time.time()
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_query = _build_search_query(query, location, place_type)
            search_url = f"https://www.google.com/maps/search/{quote(search_query)}"
            logger.info("Navigating to Google Maps: %s", search_url)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_main_panel(page, 15000)
            await page.wait_for_timeout(2000)
            await _scroll_results(page, limit)
            cards = await _find_place_cards(page)
            logger.info("Found %d potential place cards", len(cards))
            place_dicts = await _collect_place_dicts(cards, page, search_url, limit)
            elapsed_ms = int((time.time() - start_time) * 1000)
            places = [GoogleMapsPlace(**d) for d in place_dicts]
            logger.info(
                "Extracted %d places from Google Maps in %dms", len(places), elapsed_ms
            )
            return {
                "success": True,
                "places": places,
                "count": len(places),
                "elapsed_ms": elapsed_ms,
            }
    finally:
        await close_stealth_context(browser, ctx, page)


async def _do_search_google_maps(query: str, location: Optional[str], limit: int, place_type: str) -> Dict[str, Any]:
    """Public Maps-search entry point with timeout / browser-failure handling."""
    start_time = time.time()
    try:
        return await _execute_maps_search(query, location, limit, place_type)
    except PlaywrightTimeoutError as exc:
        return _search_failure(
            query, exc, "Timeout", int((time.time() - start_time) * 1000)
        )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Error searching Google Maps: %s", exc, exc_info=True)
        return {
            "success": False,
            "places": [],
            "count": 0,
            "error": str(exc),
            "elapsed_ms": elapsed,
        }


# ---------------------------------------------------------------------------
# Place detail
# ---------------------------------------------------------------------------


def _empty_place_data() -> Dict[str, Any]:
    """Initial place-detail dict before any fields are populated."""
    return {
        "name": None,
        "rating": None,
        "review_count": None,
        "address": None,
        "phone": None,
        "website": None,
        "hours": {},
        "hours_open_now": None,
        "price_level": None,
        "categories": [],
        "coordinates": None,
        "reviews": [],
        "photos_count": None,
        "popular_times": None,
        "qa_highlights": [],
    }


async def _populate_place_data(page: Any) -> Dict[str, Any]:
    """Fetch every detail-panel field, returning the assembled dict."""
    data = _empty_place_data()
    data["name"] = await get_place_name(page)
    data["rating"] = await get_place_rating(page)
    data["review_count"] = await get_place_review_count(page)
    data["address"] = await get_place_address(page)
    data["phone"] = await get_place_phone(page)
    data["website"] = await get_place_website(page)
    data["hours"] = await get_place_hours(page)
    data["hours_open_now"] = await get_place_open_now(page)
    raw_reviews = await get_place_reviews(page)
    data["reviews"] = [GoogleMapsPlaceReview(**r) for r in raw_reviews]
    coords = extract_coordinates_from_url(page.url)
    if coords:
        data["coordinates"] = coords
    return data


async def _navigate_to_place_by_url(page: Any, place_url: str) -> None:
    """Navigate the Playwright page directly to a Google Maps place URL."""
    logger.info("Navigating to place URL: %s", place_url)
    await page.goto(place_url, wait_until="domcontentloaded", timeout=30000)


async def _click_first_search_result(page: Any) -> None:
    """Best-effort click on the first card to open its detail panel."""
    try:
        await page.wait_for_selector("[role='main']", timeout=10000)
        first_result = await page.query_selector(
            "div[data-result-index='0'], div[role='article']"
        )
        if first_result:
            await first_result.click()
            await page.wait_for_timeout(2000)
    except PLACE_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("Could not click first result: %s", exc)
        return None  # explicit recovery: non-fatal: PLACE_EXTRACTION_EXCEPTIONS is recorded but does not abort the caller


async def _navigate_to_place_by_query(page: Any, query: str, location: Optional[str]) -> None:
    """Search Maps for ``query`` (+ optional ``location``) and open the first result."""
    search_query = query if not location else f"{query} in {location}"
    search_url = f"https://www.google.com/maps/search/{quote(search_query)}"
    logger.info("Searching for place: %s", search_query)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await _click_first_search_result(page)


def _missing_target_response(start_time: float) -> Dict[str, Any]:
    """Standard 'no place_url and no query' failure payload."""
    return {
        "success": False,
        "error": "Either place_url or query is required",
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }


async def _navigate_to_place(
    page: Any,
    place_url: Optional[str],
    query: Optional[str],
    location: Optional[str],
) -> bool:
    """Drive the page to the requested place. Returns True if a target was set."""
    if place_url:
        await _navigate_to_place_by_url(page, place_url)
        return True
    if query:
        await _navigate_to_place_by_query(page, query, location)
        return True
    return False


async def _execute_place_detail(
    place_url: Optional[str], query: Optional[str], location: Optional[str], start_time: float
) -> Dict[str, Any]:
    """Drive the place-detail flow inside the Playwright context."""
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            navigated = await _navigate_to_place(page, place_url, query, location)
            if not navigated:
                return _missing_target_response(start_time)
            await page.wait_for_timeout(2000)
            place_data = await _populate_place_data(page)
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.info(
                "Extracted place details for '%s' in %dms",
                place_data.get("name"),
                elapsed_ms,
            )
            return {
                "success": True,
                "place": GoogleMapsPlaceDetail(**place_data),
                "elapsed_ms": elapsed_ms,
            }
    finally:
        await close_stealth_context(browser, ctx, page)


async def _do_get_place_detail(
    place_url: Optional[str], query: Optional[str], location: Optional[str]
) -> Dict[str, Any]:
    """Public place-detail entry point with timeout / browser-failure handling."""
    start_time = time.time()
    try:
        return await _execute_place_detail(place_url, query, location, start_time)
    except PlaywrightTimeoutError as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Timeout getting place details: %s", exc)
        return {"success": False, "error": f"Timeout: {exc}", "elapsed_ms": elapsed}
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        elapsed = int((time.time() - start_time) * 1000)
        logger.error("Error getting place details: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc), "elapsed_ms": elapsed}


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.post("/search", response_model=GoogleMapsSearchResponse)
async def search_google_maps(request: GoogleMapsSearchRequest) -> GoogleMapsSearchResponse:
    """Search Google Maps for businesses/places."""
    result = await asyncio.wait_for(
        _do_search_google_maps(request.query, request.location, request.limit, request.type),
        timeout=60,
    )
    return GoogleMapsSearchResponse(**result)


@router.post("/place", response_model=GoogleMapsPlaceResponse)
async def get_google_maps_place(request: GoogleMapsPlaceRequest) -> GoogleMapsPlaceResponse:
    """Get full details for a single Google Maps place."""
    if not request.place_url and not request.query:
        return GoogleMapsPlaceResponse(
            success=False,
            error="Either place_url or query is required",
            elapsed_ms=0,
        )
    result = await asyncio.wait_for(
        _do_get_place_detail(request.place_url, request.query, request.location),
        timeout=60,
    )
    return GoogleMapsPlaceResponse(**result)
