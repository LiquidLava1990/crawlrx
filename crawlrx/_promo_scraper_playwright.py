"""Playwright fallback path for ``promo_scraper`` -- JS-rendered coupon pages."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Dict, List

from playwright.async_api import async_playwright

from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger(__name__)

PLAYWRIGHT_TIMEOUT_MS = 15000

_JS_EXTRACT = """(selectors) => {
    const items = [];
    let cards = [];
    for (const sel of selectors.split(',')) {
        const found = document.querySelectorAll(sel.trim());
        if (found.length) { cards = found; break; }
    }
    const get = (el, s) => { const e = el.querySelector(s); return e ? e.innerText.trim() : ''; };
    cards.forEach(card => {
        const code = get(card, '.code-text, .promo-code, [class*="code"]');
        const title = get(card, '.offer-title, [class*="title"], h3, h4');
        const desc = get(card, '.description, [class*="description"], p');
        const expires = get(card, '.expiration, [class*="expir"]') || null;
        const verified = !!card.querySelector('.verified, [class*="verified"]') || card.innerText.toLowerCase().includes('verified');
        if (code || title) items.push({code, title, desc, expires, verified});
    });
    return items;
}"""


def _normalize_card(raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Map a JS-extracted card into the canonical promo record shape."""
    return {
        "code": raw.get("code", ""),
        "discount": raw.get("title", ""),
        "description": raw.get("desc", ""),
        "expires": raw.get("expires"),
        "source": source,
        "verified": raw.get("verified", False),
    }


async def _playwright_scrape(
    merchant: str,
    url: str,
    source: str,
    selectors: str,
) -> List[Dict[str, Any]]:
    """Generic Playwright scrape for JS-rendered coupon pages."""
    if not merchant:
        return []
    results: List[Dict[str, Any]] = []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            await page.wait_for_timeout(2000)
            extracted = await page.evaluate(_JS_EXTRACT, selectors)
            results = [_normalize_card(card, source) for card in extracted]
            logger.info(
                "%s Playwright: %d coupons for %s", source, len(results), merchant
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "%s Playwright scrape failed for %s (%s): %s",
            source,
            merchant,
            type(exc).__name__,
            exc,
        )
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    except PARSE_EXCEPTIONS as exc:
        logger.warning(
            "%s Playwright parse failed for %s (%s): %s",
            source,
            merchant,
            type(exc).__name__,
            exc,
        )
        return None  # explicit recovery: non-fatal: PARSE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
    return results


def scrape_retailmenot_playwright(merchant: str) -> Awaitable[List[Dict[str, Any]]]:
    """Async coroutine: scrape RetailMeNot via Playwright."""
    url = "https://www.retailmenot.com/view/%s.com" % merchant.lower().strip()
    return _playwright_scrape(
        merchant,
        url,
        "retailmenot",
        '[data-testid="coupon-card"], .offer-card, .coupon-card, [class*="coupon"]',
    )


def scrape_coupons_com_playwright(merchant: str) -> Awaitable[List[Dict[str, Any]]]:
    """Async coroutine: scrape Coupons.com via Playwright."""
    url = "https://www.coupons.com/coupon-codes/%s/" % merchant.lower().strip()
    return _playwright_scrape(
        merchant,
        url,
        "coupons.com",
        '.coupon-card, .promo-card, [class*="coupon"], [class*="promo"]',
    )
