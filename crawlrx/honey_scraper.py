"""Honey scraper for CrawlRX -- scrape coupon codes from joinhoney.com."""

import logging
from urllib.parse import urlparse

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel

from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger(__name__)

CDP_PORT = 9232
PLAYWRIGHT_TIMEOUT = 15000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter()


class HoneyRequest(BaseModel):
    url: str


_JS_MAIN = r"""() => {
    const items = [];
    const selectors = [
        '[data-testid="coupon-card"]', '.coupon-card', '.offer-card',
        '[class*="coupon"]', '[class*="offer"]', '.honey-coupon', '.deal-card'
    ];
    let cards = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length) { cards = found; break; }
    }
    const get = (el, s) => { const e = el.querySelector(s); return e ? e.innerText.trim() : ''; };
    cards.forEach(card => {
        const code = get(card, '[data-testid="coupon-code"], .coupon-code, .code, [class*="code"]');
        const discount = get(card, '[data-testid="discount"], .discount, .savings, [class*="discount"]');
        const description = get(card, '[data-testid="description"], .description, .offer-text, [class*="description"]');
        const successText = get(card, '[data-testid="success-rate"], .success-rate, [class*="success"]');
        const verified = !!card.querySelector('[data-testid="verified"], .verified, [class*="verified"]')
                         || card.innerText.toLowerCase().includes('verified');
        let success_rate = null;
        const m = successText.match(/(\d+)%/);
        if (m) success_rate = parseInt(m[1], 10);
        if (code) items.push({code, discount, description, success_rate, verified});
    });
    return items;
}"""

_JS_FALLBACK = r"""() => {
    const items = [];
    document.querySelectorAll('code, .code-text, [class*="promo"], [class*="coupon-code"]').forEach(el => {
        const text = el.innerText.trim();
        if (text && text.length > 2 && text.length < 30) {
            const container = el.closest('[class*="coupon"], [class*="offer"], [class*="deal"]');
            const desc = container ? container.innerText.replace(text, '').trim() : '';
            items.push({code: text, description: desc});
        }
    });
    return items;
}"""


def _normalize_main_card(card: dict) -> dict:
    """Coerce a Honey main-selector card into the canonical coupon dict."""
    return {
        "code": card.get("code", ""),
        "discount": card.get("discount", ""),
        "description": card.get("description", ""),
        "success_rate": card.get("success_rate"),
        "source": "honey",
        "verified": card.get("verified", False),
    }


def _normalize_fallback_card(card: dict) -> dict:
    """Coerce a fallback-selector card into the canonical coupon dict."""
    return {
        "code": card.get("code", ""),
        "discount": "",
        "description": card.get("description", ""),
        "success_rate": None,
        "source": "honey",
        "verified": False,
    }


async def _accept_cookie_banner(page) -> None:
    """Best-effort click of accept/allow cookie banner; suppress errors."""
    try:
        btn = await page.locator(
            'button:has-text("Accept"), button:has-text("Allow"), [data-testid="accept-cookies"]'
        ).first
        if btn.is_visible(timeout=2000):
            await btn.click()
            await page.wait_for_timeout(1000)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Suppressed cookie banner error: %s", exc)


async def _evaluate_with_fallback(page) -> list:
    """Evaluate primary then fallback JS selectors; return normalized coupons."""
    main = await page.evaluate(_JS_MAIN)
    if main:
        return [_normalize_main_card(c) for c in main]
    fallback = await page.evaluate(_JS_FALLBACK)
    return [_normalize_fallback_card(c) for c in fallback]


def _merchant_url(merchant: str) -> str:
    """Build the Honey shop URL for ``merchant`` (lowercase, hyphen-joined)."""
    cleaned = merchant.strip().lower().replace(" ", "-")
    return f"https://www.joinhoney.com/shop/{cleaned}"


async def close_browser_safe(browser: object) -> None:
    """Idempotent browser close — log + swallow any failure."""
    if not browser:
        return
    try:
        await browser.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s", exc)


async def scrape_honey(merchant: str) -> list:
    """Scrape coupon codes from Honey for ``merchant``."""
    if not merchant or not isinstance(merchant, str):
        logger.warning("Invalid merchant provided to scrape_honey: %s", merchant)
        return []

    url = _merchant_url(merchant)
    results: list = []
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("Navigating to Honey: %s", url)
            await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            await page.wait_for_timeout(3000)
            await _accept_cookie_banner(page)
            results = await _evaluate_with_fallback(page)
            logger.info("Honey scrape: %d codes for %s", len(results), merchant)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Honey scrape failed for %s: %s", merchant, exc)
        return []
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_safe(browser)

    return results


def _merchant_from_url(raw: str) -> str:
    """Extract the merchant slug from a URL or return ``raw`` unchanged."""
    if "://" not in raw:
        return raw
    return urlparse(raw).netloc.replace("www.", "").split(".")[0]


def _public_coupon_view(coupon: dict) -> dict:
    """Strip internal fields, keep public coupon shape."""
    return {
        "code": coupon.get("code", ""),
        "discount": coupon.get("discount", ""),
        "description": coupon.get("description", ""),
        "source": coupon.get("source", "honey"),
        "verified": coupon.get("verified", False),
    }


@router.post("/scrape/honey")
def api_scrape_honey(request: HoneyRequest) -> list:
    """POST /scrape/honey -- scrape coupon codes for a merchant URL."""
    merchant = _merchant_from_url(request.url)
    return [_public_coupon_view(c) for c in scrape_honey(merchant)]
