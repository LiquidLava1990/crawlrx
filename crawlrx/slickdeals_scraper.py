"""Slickdeals scraper for CrawlRX webforge."""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger(__name__)

CDP_PORT = 9239
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter()

_BASE_URL = "https://slickdeals.net"

_DEAL_TITLE_SELECTOR = (
    ".dealCard__title a, .dealTitle a, .fpItem__title a, .thread-title a, "
    "a[data-role='dealTitle'], h3 a, h4 a, .title a"
)
_DEAL_PRICE_SELECTOR = (
    ".dealCard__price, .dealPrice, .fpItem__price, .price, .thread-price"
)
_DEAL_ORIGINAL_SELECTOR = (
    ".dealCard__originalPrice, .originalPrice, .was-price, .oldPrice"
)
_DEAL_STORE_SELECTOR = (
    ".dealCard__store, .storeName, .fpItem__store, .merchant, .thread-merchant"
)
_DEAL_VOTES_SELECTOR = (
    ".dealCard__thumbs, .thumbsUp, .score, .thread-votes, .vote-count"
)


class SlickdealsRequest(BaseModel):
    query: str


class SlickdealsResponse(BaseModel):
    success: bool = True
    results: List[Dict[str, Any]]
    count: int
    source: str


# --- helpers -------------------------------------------------------------

async def _launch_browser(p: Any) -> Any:
    """Launch a headless Chromium for the Slickdeals scrape."""
    return await p.chromium.launch(headless=True)


def _calc_discount_pct(price: str, original: str) -> str:
    """Return discount percentage string, or '' on failure."""
    try:
        p_val = float(price.replace("$", "").replace(",", "").strip())
        o_val = float(original.replace("$", "").replace(",", "").strip())
        if o_val > 0:
            return "%d%%" % int(((o_val - p_val) / o_val) * 100)
    except (ValueError, TypeError):
        pass
    return ""


def _text_or_default(elem: Any, default: str = "") -> str:
    """Return ``elem.inner_text().strip()`` or ``default`` if elem is None."""
    if not elem:
        return default
    try:
        return elem.inner_text().strip()
    except SELECTOR_EXTRACTION_EXCEPTIONS:
        return default


def _parse_deal_card(card: Any, base_url: str) -> Dict[str, Any]:
    """Extract deal fields from a Playwright element handle."""
    title_elem = card.query_selector(_DEAL_TITLE_SELECTOR)
    title = _text_or_default(title_elem)
    deal_url = title_elem.get_attribute("href") if title_elem else ""
    if deal_url and not deal_url.startswith("http"):
        deal_url = urljoin(base_url, deal_url)

    price = _text_or_default(card.query_selector(_DEAL_PRICE_SELECTOR))
    original_price = _text_or_default(card.query_selector(_DEAL_ORIGINAL_SELECTOR))
    store = _text_or_default(card.query_selector(_DEAL_STORE_SELECTOR))
    thumbs_up = _text_or_default(card.query_selector(_DEAL_VOTES_SELECTOR), "0")

    discount_pct = (
        _calc_discount_pct(price, original_price)
        if price and original_price else ""
    )
    return {
        "title": title,
        "price": price,
        "original_price": original_price,
        "discount_pct": discount_pct,
        "store": store,
        "url": deal_url,
        "thumbs_up": thumbs_up,
        "source": "slickdeals",
    }


def _collect_deals_from_cards(cards: List[Any]) -> List[Dict[str, Any]]:
    """Translate raw card handles into deal dicts; skip cards that fail parse."""
    results: List[Dict[str, Any]] = []
    for card in cards[:20]:
        try:
            deal = _parse_deal_card(card, _BASE_URL)
        except SELECTOR_EXTRACTION_EXCEPTIONS as e:
            logger.debug("Error parsing deal card: %s", e)
            continue
        if deal["title"]:
            results.append(deal)
    return results


async def _query_cards(page: Any, primary: str, fallback: Optional[str] = None) -> List[Any]:
    """Query a card selector with optional fallback."""
    cards = await page.query_selector_all(primary)
    if not cards and fallback:
        cards = await page.query_selector_all(fallback)
    return cards


async def _scrape_with_browser(
    target_url: str,
    primary_selector: str,
    fallback_selector: Optional[str],
    log_label: str,
) -> Tuple[Any, Any, Any, List[Dict[str, Any]]]:
    """Open a stealth browser, fetch ``target_url``, return ``(browser, ctx, page, deals)``."""
    async with async_playwright() as p:
        await _launch_browser(p)  # ensures CDP / chromium available
        browser, ctx, page = await make_stealth_context(p)
        await page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        cards = await _query_cards(page, primary_selector, fallback_selector)
        logger.debug("Found %d %s cards", len(cards), log_label)
        deals = _collect_deals_from_cards(cards)
        return browser, ctx, page, deals


async def _scrape_slickdeals_page(
    target_url: str,
    primary_selector: str,
    fallback_selector: Optional[str],
    log_label: str,
) -> List[Dict[str, Any]]:
    """Outer scrape pipeline: open browser, parse cards, ensure cleanup."""
    browser = ctx = page = None
    try:
        browser, ctx, page, deals = await _scrape_with_browser(
            target_url, primary_selector, fallback_selector, log_label,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("Error scraping Slickdeals %s: %s", log_label, e)
        deals = []
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
    logger.info("Scraped %d %s deals from Slickdeals", len(deals), log_label)
    return deals


async def scrape_slickdeals(query: str) -> List[Dict[str, Any]]:
    """Search Slickdeals for deals matching ``query``."""
    url = (
        "https://slickdeals.net/newsearch.php?q=%s&searchin=first&pp=20"
        % quote(query)
    )
    logger.info("Scraping Slickdeals search: %s", query)
    return await _scrape_slickdeals_page(
        target_url=url,
        primary_selector=".dealCard",
        fallback_selector=None,
        log_label="search",
    )


async def get_frontpage_deals() -> List[Dict[str, Any]]:
    """Scrape Slickdeals frontpage hot deals."""
    logger.info("Scraping Slickdeals frontpage")
    return await _scrape_slickdeals_page(
        target_url="https://slickdeals.net/",
        primary_selector=(
            ".dealCard, .frontpageCard, [data-module='frontpageDeal']"
        ),
        fallback_selector=".fpItem, .deal-row, .gridDeal",
        log_label="frontpage",
    )


@router.post("/scrape/slickdeals", response_model=SlickdealsResponse)
async def scrape_slickdeals_endpoint(request: SlickdealsRequest) -> SlickdealsResponse:
    """POST /scrape/slickdeals -- search Slickdeals for deals."""
    logger.info("POST /scrape/slickdeals - query: %s", request.query)
    deals = await scrape_slickdeals(request.query)
    return SlickdealsResponse(
        success=True, results=deals, count=len(deals), source="slickdeals",
    )
