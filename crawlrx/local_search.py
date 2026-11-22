"""local_search.py -- POST /search/local -- local business search via Google Maps"""

import asyncio
import logging
import re
from typing import Any, Dict, List
from urllib.parse import quote

from fastapi import APIRouter

from .models import LocalSearchRequest, LocalSearchResponse, LocalSearchResult
from .utils import timer_ms
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.local_search")
router = APIRouter(prefix="/search", tags=["search"])


_NAME_SELECTOR = "h3, div[role='heading'] span, div.fontHeadlineSmall"
_CATEGORY_SELECTOR = "div.fontBodyMedium span, div.W4Efsd span:first-child"
_RATING_SELECTOR = "span[role='img'], span.MW4etd"
_REVIEWS_SELECTOR = "span.MW4etd + span, span[aria-label*='review']"
_ADDRESS_SELECTOR = (
    "span[jstcache] span, div.W4Efsd span:last-child, span[aria-label*='address']"
)
_WEBSITE_SELECTOR = (
    "a[href^='http']:not([href*='google.com']):not([href^='tel:'])"
)
_HOURS_SELECTOR = (
    "span[aria-label*='hours'], span[aria-label*='Open'], span[aria-label*='Closed']"
)
_CARD_SELECTOR = (
    "div[data-result-index], div[role='article'], div[data-async-context]"
)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


async def _scroll_to_load(page: Any, max_results: int) -> None:
    """Scroll the results pane a few times so Google Maps lazy-loads cards."""
    scroll_count = min(max_results // 3 + 1, 5)
    for _ in range(scroll_count):
        try:
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(500)
        except BROWSER_SCRAPE_EXCEPTIONS:
            return


async def _populate_name(card: Any, business: Dict[str, Any]) -> None:
    """Read the business name out of the card heading."""
    name_el = card.query_selector(_NAME_SELECTOR)
    if name_el:
        business["name"] = (await name_el.inner_text()).strip()


async def _populate_category(card: Any, business: Dict[str, Any]) -> None:
    """Read the category text, ignoring it if it merely echoes the name."""
    cat_el = card.query_selector(_CATEGORY_SELECTOR)
    if not cat_el:
        return
    text = (await cat_el.inner_text()).strip()
    if text and text != business.get("name", ""):
        business["category"] = text


def _safe_float(token: str) -> Any:
    """Parse ``token`` to float; return ``None`` on any failure."""
    try:
        return float(token)
    except (ValueError, IndexError):
        return None


def _rating_from_text(text: str) -> Any:
    """Leading float in ``text`` when ``text`` carries a digit."""
    if not text or not any(c.isdigit() for c in text):
        return None
    parts = text.split()
    return _safe_float(parts[0]) if parts else None


def _rating_from_aria(aria: str) -> Any:
    """Leading float in ``aria`` when the label mentions stars."""
    if "star" not in aria.lower():
        return None
    parts = aria.split()
    return _safe_float(parts[0]) if parts else None


def _parse_rating_value(text: str, aria: str) -> Any:
    """Pull the leading float out of either inner text or aria-label."""
    return _rating_from_text(text) or _rating_from_aria(aria)


async def _populate_rating(card: Any, business: Dict[str, Any]) -> None:
    """Convert the rating element into a float."""
    rating_el = card.query_selector(_RATING_SELECTOR)
    if not rating_el:
        return
    aria = await rating_el.get_attribute("aria-label") or ""
    text = (await rating_el.inner_text()).strip()
    rating_val = _parse_rating_value(text, aria)
    if rating_val is not None:
        business["rating"] = rating_val


async def _populate_review_count(card: Any, business: Dict[str, Any]) -> None:
    """Convert the parenthesised review count into an int."""
    reviews_el = card.query_selector(_REVIEWS_SELECTOR)
    if not reviews_el:
        return
    text = (await reviews_el.inner_text()).strip()
    if "(" not in text:
        return
    try:
        business["review_count"] = int(text.strip("()").replace(",", ""))
    except ValueError:
        return


async def _populate_address(card: Any, business: Dict[str, Any]) -> None:
    """Set ``address`` only when the candidate text contains a digit."""
    addr_el = card.query_selector(_ADDRESS_SELECTOR)
    if not addr_el:
        return
    text = (await addr_el.inner_text()).strip()
    if text and len(text) > 5 and any(c.isdigit() for c in text):
        business["address"] = text


async def _populate_phone(card: Any, business: Dict[str, Any]) -> None:
    """Read phone from a ``tel:`` link, fallback to a regex over inner text."""
    phone_el = card.query_selector("a[href^='tel:']")
    if phone_el:
        href = await phone_el.get_attribute("href") or ""
        business["phone"] = href.replace("tel:", "")
        return
    match = _PHONE_RE.search(card.inner_text())
    if match:
        business["phone"] = match.group(0)


async def _populate_website(card: Any, business: Dict[str, Any]) -> None:
    """Pull the first non-Google external link as the business website."""
    website_el = card.query_selector(_WEBSITE_SELECTOR)
    if not website_el:
        return
    href = await website_el.get_attribute("href")
    if href and not href.startswith("/"):
        business["website"] = href


async def _populate_hours(card: Any, business: Dict[str, Any]) -> None:
    """Populate ``hours`` from aria-label, falling back to inner text."""
    hours_el = card.query_selector(_HOURS_SELECTOR)
    if not hours_el:
        return
    aria = await hours_el.get_attribute("aria-label")
    business["hours"] = aria or hours_el.inner_text().strip()


async def _parse_business_card(card: Any) -> Dict[str, Any]:
    """Apply every per-field probe to one Google Maps result card."""
    business: Dict[str, Any] = {}
    try:
        await _populate_name(card, business)
        await _populate_category(card, business)
        await _populate_rating(card, business)
        await _populate_review_count(card, business)
        await _populate_address(card, business)
        await _populate_phone(card, business)
        await _populate_website(card, business)
        await _populate_hours(card, business)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error extracting card (%s): %s", type(exc).__name__, exc)
    return business


async def _extract_local_businesses(page: Any, max_results: int) -> List[Dict[str, Any]]:
    """Drive Google Maps results extraction; returns up to ``max_results`` records."""
    businesses: List[Dict[str, Any]] = []
    try:
        await page.wait_for_selector("[role='main']", timeout=10000)
        await page.wait_for_timeout(2000)
        await _scroll_to_load(page, max_results)
        cards = await page.query_selector_all(_CARD_SELECTOR)
        for card in cards[:max_results]:
            business = await _parse_business_card(card)
            if business.get("name"):
                businesses.append(business)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Error during extraction: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return businesses


def _empty_search_record(query: str, zip_code: str) -> Dict[str, Any]:
    """Skeleton dict every Google Maps search starts with."""
    return {
        "success": False,
        "query": query,
        "zip_code": zip_code,
        "results": [],
        "error": None,
        "elapsed_ms": 0,
    }


async def _wait_for_networkidle_quiet(page: Any, timeout: int) -> None:
    """Wait for networkidle, ignoring timeout (results may still be present)."""
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 20000))
    except PWTimeout:
        return


async def _do_local_search(
    query: str, zip_code: str, limit: int, timeout: int,
) -> Dict[str, Any]:
    """Synchronous Playwright scrape of Google Maps for local search."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    start = timer_ms()
    result = _empty_search_record(query, zip_code)
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            search_url = (
                f"https://www.google.com/maps/search/{quote(query)}+near+{quote(zip_code)}"
            )
            logger.info("Navigating to Google Maps: %s", search_url)
            await page.goto(search_url, timeout=timeout, wait_until="domcontentloaded")
            await _wait_for_networkidle_quiet(page, timeout)
            businesses = await _extract_local_businesses(page, limit)
            if businesses:
                result["success"] = True
                result["results"] = businesses
                logger.info(
                    "Extracted %d local businesses for '%s near %s'",
                    len(businesses), query, zip_code,
                )
            else:
                result["error"] = "No business listings found"
                logger.warning(
                    "No businesses found for query: %s near %s", query, zip_code,
                )
    except PWTimeout as exc:
        result["error"] = f"Timeout waiting for Google Maps: {exc}"
        logger.error("Timeout for local search '%s near %s': %s", query, zip_code, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        result["error"] = f"Search failed: {exc}"
        logger.error(
            "Error in local search for '%s near %s': %s",
            query, zip_code, exc, exc_info=True,
        )
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        result["elapsed_ms"] = int(timer_ms() - start)
    return result


@router.post(
    "/local",
    response_model=LocalSearchResponse,
    summary="Search local businesses via Google Maps",
)
async def search_local(request: LocalSearchRequest) -> LocalSearchResponse:
    """Search for local businesses near a ZIP code using Google Maps."""
    timeout = 30000
    wall_timeout = timeout // 1000 + 10
    limit = min(request.limit, 20)
    try:
        raw = await asyncio.wait_for(
            _do_local_search(request.query, request.zip_code, limit, timeout),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for local search")
        return LocalSearchResponse(
            success=False,
            query=request.query,
            zip_code=request.zip_code,
            error=f"Global timeout exceeded ({wall_timeout}s wall-clock)",
            elapsed_ms=wall_timeout * 1000,
        )
    if raw.get("results"):
        raw["results"] = [LocalSearchResult(**b) for b in raw["results"]]
    return LocalSearchResponse(**raw)
