"""
gmaps.py -- Google Maps business listings scraper
Endpoint: POST /scrape/google-maps

Uses headless Playwright (fresh browser per request, NOT CDP) to search Google Maps
and extract business listings. Scrolls results panel once before extracting.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import quote

from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from .models import GoogleMapsRequest, GoogleMapsResponse, GoogleMapsBusiness
from .utils import timer_ms
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    find_us_phone,
)

logger = logging.getLogger("crawlrx.gmaps")
router = APIRouter(prefix="/scrape", tags=["scrape"])

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_PANEL_SEL = "div[role='main']"


async def _scroll_results_panel(page, max_results: int) -> None:
    """Scroll the left-side results panel to load more listings before extraction."""
    try:
        await page.wait_for_selector(_PANEL_SEL, timeout=10000)
    except PLAYWRIGHT_EXCEPTIONS:
        return

    max_scrolls = max(max_results // 5, 3)
    prev_count = 0
    for _ in range(max_scrolls):
        try:
            await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); if (el) el.scrollBy(0, 800); }",
                _PANEL_SEL,
            )
            await page.wait_for_timeout(700)
            cur_count = await page.evaluate(
                "() => document.querySelectorAll(\"div[jsaction*='mouseover']\").length"
            )
            if cur_count == prev_count:
                break
            prev_count = cur_count
        except PLAYWRIGHT_EXCEPTIONS:
            break


async def _extract_card_name(card) -> str | None:
    """Best-effort name extraction from a Google Maps card."""
    name_el = card.query_selector("div.fontHeadlineSmall, div[role='heading'] span, h3")
    if not name_el:
        return None
    return (await name_el.inner_text()).strip() or None


async def _extract_card_rating(card) -> str | None:
    """Pull the rating string off a card's star widget."""
    rating_el = card.query_selector("span[role='img'][aria-label*='star'], span.MW4etd")
    if not rating_el:
        return None
    aria = await rating_el.get_attribute("aria-label") or ""
    if "star" in aria.lower():
        parts = aria.split()
        return parts[0] if parts else None
    text = (await rating_el.inner_text()).strip()
    if text and any(c.isdigit() for c in text):
        return text
    return None


async def _extract_card_reviews(card) -> str | None:
    """Pull the review-count text off a card."""
    reviews_el = card.query_selector("span[aria-label*='review'], span.UY7F9")
    if not reviews_el:
        return None
    text = (await reviews_el.inner_text()).strip().strip("()")
    return text or None


async def _extract_card_category(card, name: str | None) -> str | None:
    """Pull the category text, ignoring duplicates of the business name."""
    cat_el = card.query_selector(
        "div.W4Efsd > div.W4Efsd span:first-child, div.fontBodyMedium span"
    )
    if not cat_el:
        return None
    text = (await cat_el.inner_text()).strip()
    if text and text != (name or ""):
        return text
    return None


async def _extract_card_address(card) -> str | None:
    """Pull the address text, requiring a digit so we don't grab labels."""
    addr_el = card.query_selector(
        "span[aria-label*='address'], div.W4Efsd span:last-child"
    )
    if not addr_el:
        return None
    text = (await addr_el.inner_text()).strip()
    if len(text) > 5 and any(c.isdigit() for c in text):
        return text
    return None


async def _extract_card_phone(card) -> str | None:
    """Phone via ``tel:`` link, falling back to a regex on card text."""
    phone_el = card.query_selector("a[href^='tel:']")
    if phone_el:
        href = await phone_el.get_attribute("href") or ""
        return href.replace("tel:", "").strip() or None
    card_text = card.inner_text()
    return find_us_phone(card_text)


async def _extract_card_website(card) -> str | None:
    """External (non-google) website href on the card."""
    website_el = card.query_selector(
        "a[href^='http']:not([href*='google.com']):not([href^='tel:'])"
    )
    if not website_el:
        return None
    href = await website_el.get_attribute("href") or ""
    if href and not href.startswith("/"):
        return href
    return None


async def _extract_card_hours(card) -> str | None:
    """Hours text from an ``hour``-tagged element, if present."""
    hours_el = card.query_selector("span[aria-label*='hour'], div[class*='hour']")
    if not hours_el:
        return None
    return (await hours_el.inner_text()).strip() or None


async def _extract_one_business(card) -> dict | None:
    """Return one business dict from a card, or ``None`` if the card has no name."""
    name = await _extract_card_name(card)
    if not name:
        return None
    business: dict = {"name": name}
    rating = await _extract_card_rating(card)
    if rating:
        business["rating"] = rating
    reviews = await _extract_card_reviews(card)
    if reviews:
        business["reviews"] = reviews
    category = await _extract_card_category(card, name)
    if category:
        business["category"] = category
    address = await _extract_card_address(card)
    if address:
        business["address"] = address
    phone = await _extract_card_phone(card)
    if phone:
        business["phone"] = phone
    website = await _extract_card_website(card)
    if website:
        business["website"] = website
    hours = await _extract_card_hours(card)
    if hours:
        business["hours"] = hours
    return business


_CARD_SUPPRESS = SELECTOR_EXTRACTION_EXCEPTIONS + PLAYWRIGHT_EXCEPTIONS


async def _extract_businesses(page, max_results: int) -> list:
    """Scroll results panel once, then extract business listings."""
    businesses: list = []
    try:
        await page.wait_for_timeout(1500)
        await _scroll_results_panel(page, max_results)
        cards = await page.query_selector_all(
            "div[jsaction*='mouseover'], div[role='article']"
        )
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.warning("[GMaps] Extraction error: %s", exc)
        return businesses

    for card in cards[:max_results]:
        try:
            business = await _extract_one_business(card)
        except _CARD_SUPPRESS as exc:
            logger.debug("[GMaps] Card extraction error: %s", exc)
            continue
        if business is not None:
            businesses.append(business)
    return businesses


async def _safe_close_one(obj: Any, name: str) -> None:
    """Close a single browser object, logging recoverable errors."""
    if obj is None:
        return
    try:
        await obj.close()
    except PLAYWRIGHT_EXCEPTIONS as close_err:
        logger.warning("[GMaps] Error closing %s: %s", name, close_err)
        return None  # explicit recovery: cleanup is best-effort; PLAYWRIGHT_EXCEPTIONS is non-fatal


async def close_browser_objects(page: "Page", context: "BrowserContext | None", browser: "Browser | BrowserContext | Page | None") -> None:
    """Close the page/context/browser triple, logging individual close errors."""
    await _safe_close_one(page, "page")
    await _safe_close_one(context, "context")
    await _safe_close_one(browser, "browser")


async def _open_browser_page(p):
    """Boot a headless Chromium + context + page using the project UA."""
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1920, "height": 1080},
    )
    page = await context.new_page()
    return browser, context, page


async def _navigate_and_extract(page, query: str, max_results: int, timeout: int) -> list:
    """Drive the Google Maps page to the query, then extract listings."""
    search_url = "https://www.google.com/maps/search/%s" % quote(query)
    logger.info("[GMaps] Navigating to %s", search_url)
    await page.goto(search_url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 20000))
    except PWTimeout:
        pass
    return await _extract_businesses(page, max_results)


def _record_scrape_outcome(result: dict, businesses: list, query: str) -> None:
    """Mutate ``result`` in place with success/empty status for the scrape."""
    if businesses:
        result["success"] = True
        result["results"] = businesses
        logger.info("[GMaps] Extracted %d businesses", len(businesses))
        return
    result["error"] = "No business listings found"
    logger.warning("[GMaps] No results for query: %s", query)


async def _do_scrape_gmaps(query: str, max_results: int, timeout: int) -> dict:
    """Synchronous Playwright scrape of Google Maps."""
    start = timer_ms()
    result: dict = {
        "success": False,
        "query": query,
        "results": [],
        "error": None,
        "elapsed_ms": 0,
    }
    browser = None
    context = None
    page = None
    try:
        async with async_playwright() as p:
            browser, context, page = await _open_browser_page(p)
            businesses = await _navigate_and_extract(page, query, max_results, timeout)
            _record_scrape_outcome(result, businesses, query)
    except PWTimeout as exc:
        result["error"] = "Timeout: %s" % exc
        logger.error("[GMaps] Timeout for query '%s': %s", query, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        result["error"] = "Scrape failed: %s" % exc
        logger.error("[GMaps] Error for '%s': %s", query, exc, exc_info=True)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_browser_objects(page, context, browser)
        result["elapsed_ms"] = int(timer_ms() - start)
    return result


@router.post(
    "/google-maps",
    response_model=GoogleMapsResponse,
    summary="Scrape Google Maps business listings",
)
async def scrape_google_maps(request: GoogleMapsRequest) -> GoogleMapsResponse:
    """
    Search Google Maps and extract business listings.

    - `query`: Search query like "coffee shops in Louisville KY"
    - `max_results`: Max businesses to return (1-50, default 10)
    - `timeout`: Timeout in milliseconds (default 30000)

    Scrolls results panel to load listings before extraction.
    Extracts: name, address, phone, hours, rating, review count, category, website.
    """
    wall_timeout = request.timeout // 1000 + 10

    try:
        raw = await asyncio.wait_for(
            _do_scrape_gmaps(request.query, request.max_results, request.timeout),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("[GMaps] Wall-clock timeout exceeded")
        return GoogleMapsResponse(
            success=False,
            query=request.query,
            error="Global timeout exceeded (%ds)" % wall_timeout,
            elapsed_ms=wall_timeout * 1000,
        )

    if raw.get("results"):
        raw["results"] = [GoogleMapsBusiness(**b) for b in raw["results"]]

    return GoogleMapsResponse(**raw)
