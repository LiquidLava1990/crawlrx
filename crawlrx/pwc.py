"""
pwc.py -- CrawlRX PWC/Jet Ski pricing endpoints
POST /pwc/value    -- Book value from KBB + J.D. Power
POST /pwc/listings -- Market listings from PWCTrader + BoatTrader

Usage:
    POST /pwc/value
    {
        "year": 2022,
        "make": "Sea-Doo",
        "model": "RXT-X 300",
        "condition": "good"        # optional: excellent|good|fair
    }

    POST /pwc/listings
    {
        "zip": "40202",
        "radius": 200,
        "year_min": 2022,
        "year_max": 2026,
        "condition": "used",       # optional: used|new|all
        "sort": "price_asc"        # optional: price_asc|price_desc|newest
    }
"""

import asyncio
import re
import time
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, List, Dict, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .browser_pool import get_browser, release_browser
from playwright.async_api import async_playwright
from .cache import get_cache
from .stealth_context import make_stealth_context, close_stealth_context

logger = logging.getLogger("crawlrx.pwc")
router = APIRouter(prefix="/pwc", tags=["pwc"])


# ─── Request / Response Models ──────────────────────────────────

class PWCValueRequest(BaseModel):
    year: int = Field(..., ge=1990, le=2030, description="Model year")
    make: str = Field(..., description="Make (e.g. Sea-Doo, Yamaha, Kawasaki)")
    model: str = Field(..., description="Model name (e.g. RXT-X 300, GP1800R)")
    condition: str = Field(default="good", description="Condition: excellent|good|fair")

class PWCValueResult(BaseModel):
    source: str
    url: str
    trade_in: Optional[str] = None
    private_party: Optional[str] = None
    retail: Optional[str] = None
    msrp: Optional[str] = None
    raw_text: Optional[str] = None
    error: Optional[str] = None

class PWCValueResponse(BaseModel):
    success: bool
    year: int
    make: str
    model: str
    condition: str
    results: List[PWCValueResult]
    elapsed_ms: int

class PWCListingsRequest(BaseModel):
    zip: str = Field(default="40202", description="ZIP code center")
    radius: int = Field(default=200, ge=25, le=500, description="Search radius in miles")
    year_min: int = Field(default=2022, ge=1990, le=2030)
    year_max: int = Field(default=2030, ge=1990, le=2030)
    condition: str = Field(default="used", description="used|new|all")
    sort: str = Field(default="price_asc", description="price_asc|price_desc|newest")

class PWCListing(BaseModel):
    title: str
    price: Optional[str] = None
    year: Optional[str] = None
    location: Optional[str] = None
    hours: Optional[str] = None
    url: Optional[str] = None
    source: str

class PWCListingsResponse(BaseModel):
    success: bool
    count: int
    listings: List[PWCListing]
    elapsed_ms: int
    error: Optional[str] = None


# ─── Helpers ────────────────────────────────────────────────────


async def _cdp_page():
    """Launch fresh headless browser and return (pw, browser, ctx, page)."""
    pw = await async_playwright().start()
    browser, ctx, page = await make_stealth_context(pw)
    return pw, browser, ctx, page

async def cdp_close(pw: "Playwright | None", browser: "Browser | BrowserContext | Page | None", ctx: "BrowserContext | None", page: "Page") -> None:
    """Tear down stealth context + Playwright instance, swallowing close errors."""
    await close_stealth_context(browser, ctx, page)
    try:
        pw.__exit__(None, None, None)
    except (OSError, RuntimeError, AttributeError) as _exc:
        logger.debug("Suppressed error: %s", _exc)

_SORT_MAP = {
    "price_asc": "Price+Ascending",
    "price_desc": "Price+Descending",
    "newest": "Most+Recent",
}

def _slug(s: str) -> str:
    """URL-safe slug for make/model."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

# ─── BoatTrader Market Value Scrape ─────────────────────────────
# KBB/JDPower block scrapers and dropped PWC support.
# BoatTrader listing search is the real-world book value for PWCs.

@dataclass(frozen=True)
class _PageFetchSpec:
    """Parameter bundle for `_fetch_text_with_scroll`."""
    nav_timeout_ms: int
    settle_seconds: float
    scrolls: int
    snippet_chars: int


async def _fetch_text_with_scroll(page: Any, url: str, spec: _PageFetchSpec) -> str:
    """Goto + settle + optional scroll + return innerText slice."""
    await page.goto(url, wait_until="domcontentloaded", timeout=spec.nav_timeout_ms)
    await asyncio.sleep(spec.settle_seconds)
    for _ in range(spec.scrolls):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(0.5)
    return await page.evaluate(
        f"() => document.body.innerText.substring(0, {spec.snippet_chars})"
    )


async def _safe_fetch_text(
    page: Any, url: str, spec: _PageFetchSpec, result: PWCValueResult, source_label: str,
) -> Optional[str]:
    """Wrap `_fetch_text_with_scroll`; on browser error, mark result and return None."""
    try:
        return await _fetch_text_with_scroll(page, url, spec)
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        result.error = str(e)
        logger.warning("%s scrape failed: %s", source_label, e)
        # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; caller short-circuits
        return None


def _populate_boattrader_prices(text: str, year: int, result: PWCValueResult) -> None:
    """Parse $ prices from BoatTrader inner text and write derived stats."""
    import re as _re
    raw_prices = _re.findall(r"\$(\d[\d,]+)", text)
    prices = sorted(
        int(x.replace(",", "")) for x in raw_prices
        if 1500 < int(x.replace(",", "")) < 55000
    )
    if not prices:
        result.raw_text = text[:500]
        result.error = "No prices extracted"
        return
    median = prices[len(prices) // 2]
    low, high = prices[0], prices[-1]
    avg = int(sum(prices) / len(prices))
    result.private_party = f"${low:,} - ${high:,}"
    result.retail = (
        f"${avg:,} avg | ${median:,} median ({len(prices)} data points)"
    )
    result.raw_text = (
        f"{year} used PWC market: {len(prices)} prices from BoatTrader. "
        f"Range ${low:,}-${high:,}"
    )


async def _scrape_boattrader_value(page: Any, year: int, make: str, model: str) -> PWCValueResult:
    """Pull market comp pricing from BoatTrader listings for a year+make."""
    url = (
        f"https://www.boattrader.com/boats/type-pwc/condition-used/"
        f"?sort=price-asc&year={year}%2C{year}"
    )
    result = PWCValueResult(source="BoatTrader-Market", url=url)
    spec = _PageFetchSpec(
        nav_timeout_ms=25000, settle_seconds=5, scrolls=6, snippet_chars=15000,
    )
    text = await _safe_fetch_text(page, url, spec, result, "BoatTrader value")
    if text is None:
        return result
    _populate_boattrader_prices(text, year, result)
    return result


# ─── BRP MSRP Scrape ────────────────────────────────────────────

def _find_model_lines(text: str, model: str) -> list[str]:
    """Search innerText for lines mentioning the model and snip surrounding context."""
    model_clean = model.lower().replace("-", " ").replace("_", " ")
    tokens = model_clean.split()
    lines = text.split("\n")
    found: list[str] = []
    for i, line in enumerate(lines):
        lowered = line.lower()
        first_hit = tokens and tokens[0] in lowered
        second_hit = len(tokens) > 1 and tokens[1] in lowered
        if first_hit or second_hit:
            found.append(" | ".join(lines[max(0, i - 1):i + 4]))
    return found


async def _scrape_brp_msrp(page: Any, year: int, model: str) -> PWCValueResult:
    """Pull original MSRP from BRP (Sea-Doo) or Yamaha WaveRunner sites."""
    url = "https://sea-doo.brp.com/en-us/personal-watercraft.html"
    result = PWCValueResult(source="BRP-SeaDoo", url=url)
    spec = _PageFetchSpec(
        nav_timeout_ms=20000, settle_seconds=3, scrolls=0, snippet_chars=5000,
    )
    text = await _safe_fetch_text(page, url, spec, result, "BRP MSRP")
    if text is None:
        return result
    found_lines = _find_model_lines(text, model)
    if found_lines:
        result.msrp = " || ".join(found_lines[:3])
    result.raw_text = text[:500]
    return result


# ─── PWCTrader Listings Scrape ───────────────────────────────────

_PWCTRADER_JS = """() => {
    const results = [];
    const cards = document.querySelectorAll(
        '[class*="card"], [class*="listing"], article, [class*="result"]'
    );
    cards.forEach(card => {
        const link = card.querySelector('a[href*="/pwc/"]');
        if (!link) return;
        const text = card.innerText || '';
        const price = (text.match(/\\$[\\d,]+/) || [''])[0];
        const year = (text.match(/\\b(19|20)\\d{2}\\b/) || [''])[0];
        const hours = (text.match(/(\\d+)\\s*(?:hrs?|hours)/i) || [''])[0];
        results.push({
            title: (link.getAttribute('title') || link.innerText || '').trim().substring(0,100),
            price, year, hours, url: link.href, location: ''
        });
    });
    return results;
}"""


async def _scroll_for_lazyload(page, scrolls: int = 5) -> None:
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(0.5)


def _items_to_listings(items: List[dict], source: str) -> List[PWCListing]:
    return [
        PWCListing(
            title=item.get("title", ""),
            price=item.get("price") or None,
            year=item.get("year") or None,
            hours=item.get("hours") or None,
            location=item.get("location") or None,
            url=item.get("url") or None,
            source=source,
        )
        for item in items
    ]


async def _scrape_listings_via(
    page,
    url: str,
    js: str,
    source_label: str,
) -> List[PWCListing]:
    """Open ``url`` in ``page``, scroll for lazyload, and parse listings via ``js``."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(4)
        await _scroll_for_lazyload(page)
        items = await page.evaluate(js)
        return _items_to_listings(items, source_label)
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("%s scrape failed: %s", source_label, e)
        return []


async def _scrape_pwctrader(page, req: PWCListingsRequest) -> List[PWCListing]:
    sort_param = _SORT_MAP.get(req.sort, "Price+Ascending")
    cond_param = "" if req.condition == "all" else f"&condition={req.condition}"
    url = (
        f"https://www.pwctrader.com/pwcs-for-sale/"
        f"?zip={req.zip}&radius={req.radius}"
        f"&yearMin={req.year_min}&yearMax={req.year_max}"
        f"{cond_param}&sort={sort_param}"
    )
    return await _scrape_listings_via(page, url, _PWCTRADER_JS, "PWCTrader")


# ─── BoatTrader Listings Scrape ──────────────────────────────────

_BOATTRADER_JS = """() => {
    const results = [];
    const cards = document.querySelectorAll(
        '[class*="card"], [class*="listing"], article, [class*="result"], [data-testid*="listing"]'
    );
    cards.forEach(card => {
        const link = card.querySelector('a[href*="listing"], a[href*="/boats/"]');
        if (!link) return;
        const text = card.innerText || '';
        const price = (text.match(/\\$[\\d,]+/) || [''])[0];
        const year  = (text.match(/\\b(19|20)\\d{2}\\b/) || [''])[0];
        const hours = (text.match(/(\\d+)\\s*(?:hrs?|hours)/i) || [''])[0];
        const loc   = (text.match(/\\b[A-Z][a-zA-Z\\s]+,\\s*[A-Z]{2}\\b/) || [''])[0];
        results.push({
            title: (link.getAttribute('title') || link.innerText || '').trim().substring(0,100),
            price, year, hours, location: loc, url: link.href
        });
    });
    return results;
}"""


_BOATTRADER_SORT = {"price_asc": "price-asc", "price_desc": "price-desc", "newest": "year-desc"}


async def _scrape_boattrader(page, req: PWCListingsRequest) -> List[PWCListing]:
    sort_param = _BOATTRADER_SORT.get(req.sort, "price-asc")
    cond_seg = "" if req.condition == "all" else f"condition-{req.condition}/"
    url = (
        f"https://www.boattrader.com/boats/type-pwc/{cond_seg}"
        f"?zip={req.zip}&radius={req.radius}"
        f"&year={req.year_min}%2C{req.year_max}"
        f"&sort={sort_param}"
    )
    return await _scrape_listings_via(page, url, _BOATTRADER_JS, "BoatTrader")


# ─── Routes ─────────────────────────────────────────────────────

@router.post("/value", response_model=PWCValueResponse, summary="PWC book value from KBB + J.D. Power")
async def pwc_value(req: PWCValueRequest) -> object:
    """
    Scrape book value for a specific PWC/jet ski from KBB and J.D. Power.
    Returns trade-in, private party, retail, and MSRP ranges where available.
    """
    cache_key = f"pwc_value:{req.year}:{_slug(req.make)}:{_slug(req.model)}:{req.condition}"
    cache = get_cache()
    cached = await asyncio.to_thread(cache.get, cache_key)
    if cached:
        return cached

    t0 = time.time()
    results = []

    pw, browser, ctx, page = _cdp_page()
    try:
        results.append(_scrape_boattrader_value(page, req.year, req.make, req.model))
        results.append(_scrape_brp_msrp(page, req.year, req.model))
    finally:
        await cdp_close(pw, browser, ctx, page)

    resp = PWCValueResponse(
        success=True,
        year=req.year,
        make=req.make,
        model=req.model,
        condition=req.condition,
        results=results,
        elapsed_ms=int((time.time() - t0) * 1000),
    )
    cache.set(cache_key, resp.model_dump(), ttl_override=3600)
    return resp


@router.post("/listings", response_model=PWCListingsResponse, summary="Market listings from PWCTrader + BoatTrader")
async def pwc_listings(req: PWCListingsRequest) -> object:
    """
    Search PWCTrader and BoatTrader for used/new jet ski listings near a ZIP code.
    Returns unified listing list sorted by price ascending by default.
    """
    cache_key = f"pwc_listings:{req.zip}:{req.radius}:{req.year_min}:{req.year_max}:{req.condition}:{req.sort}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached:
        return cached

    t0 = time.time()
    all_listings: List[PWCListing] = []

    browser, slot = await get_browser()
    try:
        ctx = await browser.new_context()
        try:
            # PWCTrader
            pg1 = await ctx.new_page()
            try:
                all_listings.extend(_scrape_pwctrader(pg1, req))
            finally:
                await pg1.close()

            await asyncio.sleep(1)

            # BoatTrader
            pg2 = await ctx.new_page()
            try:
                all_listings.extend(_scrape_boattrader(pg2, req))
            finally:
                await pg2.close()
        finally:
            await ctx.close()
    finally:
        await release_browser(slot)

    resp = PWCListingsResponse(
        success=True,
        count=len(all_listings),
        listings=all_listings,
        elapsed_ms=int((time.time() - t0) * 1000),
    )
    cache.set(cache_key, resp.model_dump(), ttl_override=1800)
    return resp


_PRIVATE_HELPERS = (_slug,)
