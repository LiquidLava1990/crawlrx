"""Rakuten scraper for CrawlRX -- cashback rates and coupon codes."""

import logging
import re

import requests
from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, PLAYWRIGHT_EXCEPTIONS, SELECTOR_EXTRACTION_EXCEPTIONS
from .stealth_context import make_stealth_context, close_stealth_context

logger = logging.getLogger(__name__)

TIMEOUT = 15
CDP_PORT = 9232
PLAYWRIGHT_TIMEOUT = 15000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter()


class RakutenRequest(BaseModel):
    merchant: str


_JS_STORES = r"""() => {
    const items = [];
    const selectors = [
        '[data-testid="store-card"]', '.store-card', '.retailer-card',
        '[class*="store-tile"]', '[class*="retailer"]', '.merchant-card', '[data-cy="store-result"]'
    ];
    let cards = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length) { cards = found; break; }
    }
    if (!cards.length) {
        const pat = /(\d+(?:\.\d+)?%)\s*cash\s*back/i;
        document.querySelectorAll('div, a').forEach(el => {
            if (pat.test(el.innerText)) cards.push(el);
        });
    }
    cards.forEach(card => {
        const text = card.innerText || '';
        const pctM = text.match(/(\d+(?:\.\d+)?)%\s*cash\s*back/i);
        const flatM = text.match(/\$([\d,]+(?:\.\d+)?)\s*cash\s*back/i);
        const nameEl = card.querySelector('h3, h2, .store-name, [class*="name"], .retailer-name');
        const descEl = card.querySelector('[class*="description"], [class*="offer"], p');
        const codeM = text.match(/code[\s:]*([A-Z0-9]+)/i);
        items.push({
            store_name: nameEl ? nameEl.innerText.trim() : '',
            cashback_pct: pctM ? parseFloat(pctM[1]) : null,
            cashback_flat: flatM ? parseFloat(flatM[1].replace(',', '')) : null,
            code: codeM ? codeM[1] : '',
            description: descEl ? descEl.innerText.trim() : '',
        });
    });
    return items;
}"""


async def _try_dismiss_cookie_banner(page) -> None:
    """Click any visible cookie/consent button; ignore if not present."""
    selector = (
        'button:has-text("Accept"), button:has-text("Allow All"), '
        'button:has-text("Agree"), [id*="accept"]'
    )
    try:
        btn = page.locator(selector).first
        if await btn.is_visible():
            await btn.click()
            await page.wait_for_timeout(1000)
    except SELECTOR_EXTRACTION_EXCEPTIONS + PLAYWRIGHT_EXCEPTIONS as _exc:
        logger.debug("Cookie banner dismissal skipped: %s", _exc)


def _store_to_offer(store: dict, merchant: str) -> dict:
    return {
        "merchant": store.get("store_name") or merchant,
        "cashback_pct": store.get("cashback_pct"),
        "cashback_flat": store.get("cashback_flat"),
        "code": store.get("code", ""),
        "description": store.get("description", ""),
        "source": "rakuten",
    }


def _fallback_pct_offers(content: str, merchant: str) -> list[dict]:
    pat = r'(\d+(?:\.\d+)?)%\s*cash\s*back'
    return [
        {
            "merchant": merchant,
            "cashback_pct": float(pct),
            "cashback_flat": None,
            "code": "",
            "description": "",
            "source": "rakuten",
        }
        for pct in re.findall(pat, content, re.IGNORECASE)[:3]
    ]


async def _collect_rakuten_offers(page, merchant: str) -> list[dict]:
    stores = await page.evaluate(_JS_STORES)
    results = [_store_to_offer(s, merchant) for s in stores]
    if results:
        return results
    content = await page.content()
    return _fallback_pct_offers(content, merchant)


async def close_browser_quietly(browser: object) -> None:
    if browser is None:
        return
    try:
        await browser.close()
    except (OSError, RuntimeError, AttributeError) as _exc:
        logger.debug("Cleanup failed (non-critical): %s", _exc)


async def scrape_rakuten(merchant: str) -> list:
    """Scrape cashback rates and coupon codes from Rakuten."""
    if not merchant or not isinstance(merchant, str):
        logger.warning("Invalid merchant provided to scrape_rakuten: %s", merchant)
        return []

    search_url = "https://www.rakuten.com/search?keyword=%s" % requests.utils.quote(merchant.strip())
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("Navigating to Rakuten search: %s", search_url)
            await page.goto(search_url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            await page.wait_for_timeout(3000)
            await _try_dismiss_cookie_banner(page)
            results = await _collect_rakuten_offers(page, merchant)
            logger.info("Rakuten scrape: %d results for %s", len(results), merchant)
            return results
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("Rakuten scrape failed for %s: %s", merchant, e)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)


@router.post("/scrape/rakuten")
def api_scrape_rakuten(request: RakutenRequest) -> list:
    """POST /scrape/rakuten -- cashback + coupon codes for a merchant."""
    return [
        {
            "merchant": o.get("merchant", request.merchant),
            "cashback_pct": o.get("cashback_pct"),
            "cashback_flat": o.get("cashback_flat"),
            "code": o.get("code", ""),
            "description": o.get("description", ""),
            "source": o.get("source", "rakuten"),
        }
        for o in scrape_rakuten(request.merchant)
    ]
