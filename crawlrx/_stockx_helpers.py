"""Internal helpers for :mod:`stockx` (module split).

Holds the JS extraction payloads + product/detail builders so the parent
module's ``_parse_*`` and ``_do_*`` functions stay under the cognitive +
nesting + length ceilings.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, TypeVar

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.stockx")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# JS payloads (kept here so parent module stays small)
# ---------------------------------------------------------------------------

SEARCH_TILES_JS = """
() => {
    const results = [];
    const tiles = document.querySelectorAll('[data-testid="product-tile"], .tile, .product-card, [data-testid="product-card"]');
    tiles.forEach(tile => {
        const nameEl = tile.querySelector('[data-testid="product-name"], .name, .product-name, h3');
        const name = nameEl ? nameEl.textContent.trim() : '';
        const tickerEl = tile.querySelector('[data-testid="product-ticker"], .ticker, .sku');
        const ticker = tickerEl ? tickerEl.textContent.trim() : '';
        const lastSaleEl = tile.querySelector('[data-testid="last-sale"], .last-sale, [data-testid="price"]');
        const last_sale = lastSaleEl ? lastSaleEl.textContent.trim() : '';
        const retailEl = tile.querySelector('[data-testid="retail-price"], .retail');
        const retail_price = retailEl ? retailEl.textContent.trim() : '';
        const asksEl = tile.querySelector('[data-testid="asks-count"], .asks');
        let asks_count = null;
        if (asksEl) { const m = asksEl.textContent.match(/\\d+/); if (m) asks_count = parseInt(m[0]); }
        const bidsEl = tile.querySelector('[data-testid="bids-count"], .bids');
        let bids_count = null;
        if (bidsEl) { const m = bidsEl.textContent.match(/\\d+/); if (m) bids_count = parseInt(m[0]); }
        const linkEl = tile.querySelector('a[href^="/"]');
        let product_url = '';
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            product_url = href.startsWith('http') ? href : 'https://stockx.com' + href;
        }
        const imgEl = tile.querySelector('img');
        const image_url = imgEl ? imgEl.getAttribute('src') : '';
        let category = '';
        if (product_url.includes('/sneakers/')) category = 'sneakers';
        else if (product_url.includes('/apparel/')) category = 'apparel';
        else if (product_url.includes('/electronics/')) category = 'electronics';
        else if (product_url.includes('/collectibles/')) category = 'collectibles';
        if (name && product_url) {
            results.push({ name, ticker, last_sale, retail_price, asks_count, bids_count, product_url, image_url, category });
        }
    });
    return results;
}
"""


PRODUCT_DETAIL_JS = """
() => {
    const result = {};
    const nameEl = document.querySelector('h1, [data-testid="product-name"], .product-name');
    result.name = nameEl ? nameEl.textContent.trim() : '';
    const descEl = document.querySelector('[data-testid="product-description"], .description, [data-component="product-description"]');
    result.description = descEl ? descEl.textContent.trim() : '';
    const colorEl = document.querySelector('[data-testid="colorway"], .colorway, .product-colorway');
    result.colorway = colorEl ? colorEl.textContent.trim() : '';
    const lastSaleEl = document.querySelector('[data-testid="last-sale"], .last-sale-value, [data-testid="market-last-sale"]');
    result.last_sale = lastSaleEl ? lastSaleEl.textContent.trim() : '';
    const retailEl = document.querySelector('[data-testid="retail-price"], .retail-value');
    result.retail_price = retailEl ? retailEl.textContent.trim() : '';
    const weekHighEl = document.querySelector('[data-testid="52-week-high"], .week-high, [data-testid="range-high"]');
    result.week_52_high = weekHighEl ? weekHighEl.textContent.trim() : '';
    const weekLowEl = document.querySelector('[data-testid="52-week-low"], .week-low, [data-testid="range-low"]');
    result.week_52_low = weekLowEl ? weekLowEl.textContent.trim() : '';
    const athEl = document.querySelector('[data-testid="all-time-high"], .ath-value');
    result.all_time_high = athEl ? athEl.textContent.trim() : '';
    const atlEl = document.querySelector('[data-testid="all-time-low"], .atl-value');
    result.all_time_low = atlEl ? atlEl.textContent.trim() : '';
    const tradesEl = document.querySelector('[data-testid="total-trades"], .trade-count');
    if (tradesEl) {
        const match = tradesEl.textContent.match(/[\\d,]+/);
        if (match) result.total_trades = parseInt(match[0].replace(/,/g, ''));
    }
    result.price_history = [];
    const chartPoints = document.querySelectorAll('[data-testid="chart-point"], .chart-point');
    chartPoints.forEach(point => {
        const date = point.getAttribute('data-date') || '';
        const price = point.getAttribute('data-price') || '';
        if (date && price) {
            result.price_history.push({ date, price });
        }
    });
    result.size_breakdown = [];
    const sizeRows = document.querySelectorAll('[data-testid="size-row"], .size-row, .size-table tr');
    sizeRows.forEach(row => {
        const sizeEl = row.querySelector('[data-testid="size"], .size-label, td:first-child');
        const asksEl = row.querySelector('[data-testid="size-asks"], .size-asks, td:nth-child(2)');
        const bidsEl = row.querySelector('[data-testid="size-bids"], .size-bids, td:nth-child(3)');
        const saleEl = row.querySelector('[data-testid="size-last-sale"], .size-last-sale');
        if (sizeEl) {
            const size = sizeEl.textContent.trim();
            let asks = null;
            let bids = null;
            let last_sale = null;
            if (asksEl) { const m = asksEl.textContent.match(/\\d+/); if (m) asks = parseInt(m[0]); }
            if (bidsEl) { const m = bidsEl.textContent.match(/\\d+/); if (m) bids = parseInt(m[0]); }
            if (saleEl) { last_sale = saleEl.textContent.trim(); }
            result.size_breakdown.push({ size, asks, bids, last_sale });
        }
    });
    return result;
}
"""


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def calc_premium_pct(last_sale: str, retail: str) -> Optional[float]:
    """Return premium % between ``last_sale`` and ``retail``, or None."""
    try:
        last_num = float(last_sale.replace("$", "").replace(",", ""))
        retail_num = float(retail.replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        return None
    if retail_num <= 0:
        return None
    return round(((last_num - retail_num) / retail_num) * 100, 1)


def extract_price(text: str) -> Optional[str]:
    """Extract a ``$N`` / ``$N.NN`` substring from text."""
    if not text:
        return None
    match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    return match.group(0) if match else None


def extract_number(text: str) -> Optional[int]:
    """Extract the first integer from text (ignoring commas)."""
    if not text:
        return None
    match = re.search(r"[\d,]+", text)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Wait helpers + scrape pipeline
# ---------------------------------------------------------------------------

async def wait_for_search_tiles(page: "Page") -> bool:
    """Wait for product tiles to render. Return True if found."""
    try:
        await page.wait_for_selector(
            "[data-testid='product-tile'], .tile, .product-card",
            timeout=15000,
        )
        return True
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for products to load")
        return False


def build_product_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Translate JS-side tile data into a stable schema for typed model build."""
    last_sale_val = extract_price(data.get("last_sale", ""))
    retail_val = extract_price(data.get("retail_price", ""))
    premium_pct = (
        calc_premium_pct(last_sale_val, retail_val)
        if last_sale_val and retail_val else None
    )
    return {
        "name": data.get("name", ""),
        "ticker": data.get("ticker") or None,
        "last_sale": last_sale_val,
        "retail_price": retail_val,
        "premium_pct": premium_pct,
        "asks_count": data.get("asks_count"),
        "bids_count": data.get("bids_count"),
        "product_url": data.get("product_url", ""),
        "image_url": data.get("image_url") or None,
        "category": data.get("category") or None,
    }


# ---------------------------------------------------------------------------
# Run-with-error-response (shared by search + product scrapes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StockXRequestSpec:
    """Bundle of metadata for the StockX error-response wrapper."""

    factory: Callable[..., Any]
    log_label: str
    start_time: float
    cleanup: Optional[Callable[[], Awaitable[None]]] = None


async def run_stockx_request(
    op: Callable[[], Awaitable[T]],
    spec: StockXRequestSpec,
) -> T:
    """Drive ``op`` with the standard StockX error response shape."""
    try:
        return await op()
    except PlaywrightTimeoutError as e:
        elapsed_ms = int((time.time() - spec.start_time) * 1000)
        logger.error("%s timeout: %s", spec.log_label, e)
        return spec.factory(
            success=False, error="Timeout: %s" % e, elapsed_ms=elapsed_ms,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        elapsed_ms = int((time.time() - spec.start_time) * 1000)
        logger.error("Error in %s: %s", spec.log_label, e)
        return spec.factory(success=False, error=str(e), elapsed_ms=elapsed_ms)
    finally:
        if spec.cleanup is not None:
            await spec.cleanup()


__all__ = [
    "BROWSER_SCRAPE_EXCEPTIONS",
    "PRODUCT_DETAIL_JS",
    "SEARCH_TILES_JS",
    "StockXRequestSpec",
    "build_product_dict",
    "calc_premium_pct",
    "extract_number",
    "extract_price",
    "run_stockx_request",
    "wait_for_search_tiles",
]
