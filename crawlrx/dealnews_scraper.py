"""DealNews scraper for CrawlRX webforge."""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urljoin, quote

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel

from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger(__name__)

CDP_PORT = 9239
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

router = APIRouter()

_DEALNEWS_BASE_URL = "https://www.dealnews.com"
_PRIMARY_CARD_SELECTOR = ".deal, .deal-item, .content-card, article.deal"
_FALLBACK_CARD_SELECTOR = "[data-deal-id], .dealCard, .offer-card"
_TITLE_SELECTOR = (
    "h3 a, h4 a, .title a, .deal-title a, a.deal-link, a[href*='/deal/']"
)
_PRICE_SELECTOR = ".price, .deal-price, .price-block, .amount, [class*='price']"
_SAVINGS_SELECTOR = (
    ".savings, .discount, .deal-savings, .save-amount, "
    "[class*='saving'], [class*='discount']"
)
_STORE_SELECTOR = (
    ".store, .merchant, .retailer, .source, .deal-source, "
    "[class*='store'], [class*='merchant']"
)
_RATING_SELECTOR = ".rating, .stars, .deal-rating, .user-rating"


class DealNewsRequest(BaseModel):
    query: str


class DealNewsResponse(BaseModel):
    success: bool = True
    results: List[Dict[str, Any]]
    count: int
    source: str


def _elem_text(elem: Any) -> str:
    """Inner text of ``elem`` (stripped) or ``""``."""
    return elem.inner_text().strip() if elem else ""


def _absolute_deal_url(href: str) -> str:
    """Promote a relative deal href to an absolute URL."""
    if href and not href.startswith("http"):
        return urljoin(_DEALNEWS_BASE_URL, href)
    return href


_FIELD_SELECTORS = (
    ("price", _PRICE_SELECTOR),
    ("savings", _SAVINGS_SELECTOR),
    ("store", _STORE_SELECTOR),
    ("rating", _RATING_SELECTOR),
)


def _parse_deal_card(card: Any) -> Optional[Dict[str, Any]]:
    """Render one deal card into the unified record (None when title missing)."""
    try:
        title_elem = card.query_selector(_TITLE_SELECTOR)
        title = _elem_text(title_elem)
        if not title:
            return None
        deal_url = title_elem.get_attribute("href") if title_elem else ""
        record: Dict[str, Any] = {
            "title": title,
            "url": _absolute_deal_url(deal_url),
            "source": "dealnews",
        }
        for field, selector in _FIELD_SELECTORS:
            record[field] = _elem_text(card.query_selector(selector))
        return record
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error parsing deal card (%s): %s", type(exc).__name__, exc)
        return None


async def _collect_deal_cards(page: Any) -> List[Dict[str, Any]]:
    """Resolve cards on the page and parse up to 20 records."""
    cards = await page.query_selector_all(_PRIMARY_CARD_SELECTOR)
    if not cards:
        cards = await page.query_selector_all(_FALLBACK_CARD_SELECTOR)
    logger.debug("Found %d deal cards", len(cards))
    out: List[Dict[str, Any]] = []
    for card in cards[:20]:
        record = _parse_deal_card(card)
        if record is not None:
            out.append(record)
    return out


async def safe_close_browser(browser: "Browser | BrowserContext | Page | None") -> None:
    """Best-effort browser teardown that swallows expected cleanup errors."""
    if not browser:
        return
    try:
        await browser.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s", type(exc).__name__)


async def scrape_dealnews(query: str) -> List[Dict[str, Any]]:
    """Search DealNews for deals matching the query.

    Returns: [{title, price, savings, store, url, rating, source}]
    """
    url = f"{_DEALNEWS_BASE_URL}/search/?q={quote(query)}"
    logger.info("Scraping DealNews search: %s", query)

    browser = ctx = page = None
    results: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            results = await _collect_deal_cards(page)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "Error scraping DealNews (%s): %s", type(exc).__name__, exc,
        )
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await safe_close_browser(browser)

    logger.info("Scraped %d deals from DealNews", len(results))
    return results


@router.post("/scrape/dealnews", response_model=DealNewsResponse)
def scrape_dealnews_endpoint(request: DealNewsRequest) -> DealNewsResponse:
    """POST /scrape/dealnews -- search DealNews for deals."""
    logger.info("POST /scrape/dealnews - query: %s", request.query)
    deals = scrape_dealnews(request.query)
    return DealNewsResponse(
        success=True, results=deals, count=len(deals), source="dealnews",
    )
