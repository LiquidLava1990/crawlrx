"""Promo/coupon scraper for CrawlRX -- RetailMeNot + Coupons.com.

Routes / public API:
    POST /scrape/promo                  -- HTTP-only path
    search_promo_codes(merchant)        -- aggregate + dedupe across two sites
    _scrape_retailmenot_playwright/...  -- JS fallback for blocked sites

DOM parsing + duplicated HTTP shape live in :mod:`_promo_scraper_helpers`.
Browser-only Playwright scraping lives in :mod:`_promo_scraper_playwright`.
"""

import logging
import time as _time
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

from ._promo_scraper_helpers import (
    CouponSiteSpec,
    deduplicate_promos,
    safe_aggregate_promos,
    scrape_coupon_site,
)
from ._promo_scraper_playwright import (
    scrape_coupons_com_playwright as _scrape_coupons_com_playwright_impl,
    scrape_retailmenot_playwright as _scrape_retailmenot_playwright_impl,
)
from ._scraper_helpers import SCRAPE_FAILURE_EXCEPTIONS

logger = logging.getLogger(__name__)

TIMEOUT = 15
CDP_PORT = 9232
PLAYWRIGHT_TIMEOUT = 15000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter()


class PromoRequest(BaseModel):
    merchant: str


# ---------------------------------------------------------------------------
# Site specs -- shared scrape pipeline drives both
# ---------------------------------------------------------------------------

_RETAILMENOT_SPEC = CouponSiteSpec(
    source="retailmenot",
    url_template="https://www.retailmenot.com/view/%s.com",
    card_selectors=[
        '[data-testid="coupon-card"]',
        ".offer-card",
        ".coupon-card",
        '[class*="coupon"]',
        '[class*="offer"]',
    ],
    timeout_s=TIMEOUT,
    user_agent=UA,
)

_COUPONS_COM_SPEC = CouponSiteSpec(
    source="coupons.com",
    url_template="https://www.coupons.com/coupon-codes/%s/",
    card_selectors=[
        ".coupon-card",
        ".promo-card",
        '[data-testid="coupon"]',
        ".offer-item",
        '[class*="coupon"]',
        '[class*="promo"]',
    ],
    timeout_s=TIMEOUT,
    user_agent=UA,
)


# ---------------------------------------------------------------------------
# Public scrapers -- thin wrappers around the shared site pipeline
# ---------------------------------------------------------------------------

def scrape_retailmenot(merchant: str) -> List[Dict[str, Any]]:
    """Scrape RetailMeNot for coupon codes."""
    return scrape_coupon_site(_RETAILMENOT_SPEC, merchant)


def scrape_coupons_com(merchant: str) -> List[Dict[str, Any]]:
    """Scrape Coupons.com for coupon codes."""
    return scrape_coupon_site(_COUPONS_COM_SPEC, merchant)


def _verified_then_source_key(coupon: Dict[str, Any]) -> tuple:
    """Sort key: verified first, then alphabetical by source."""
    return (not coupon.get("verified", False), coupon.get("source", ""))


def search_promo_codes(merchant: str) -> List[Dict[str, Any]]:
    """Aggregate + deduplicate promos from RetailMeNot and Coupons.com."""
    if not merchant:
        return []
    raw = safe_aggregate_promos(
        merchant,
        [scrape_retailmenot, scrape_coupons_com],
    )
    results = deduplicate_promos(raw, _verified_then_source_key)
    logger.info("search_promo_codes: %d unique for %s", len(results), merchant)
    return results


# ---------------------------------------------------------------------------
# FastAPI endpoint
# ---------------------------------------------------------------------------

def _scrub_codes_payload(codes: Any) -> List[Dict[str, Any]]:
    """Defensive: ensure the response carries a list of dicts only."""
    if not isinstance(codes, list):
        return []
    return [c for c in codes if isinstance(c, dict)]


@router.post("/scrape/promo")
def scrape_promo_endpoint(request: PromoRequest) -> Dict[str, Any]:
    """POST /scrape/promo -- scrape promo codes for a merchant."""
    t0 = _time.time()
    logger.info("Promo scrape endpoint: merchant=%s", request.merchant)
    if not request.merchant or not request.merchant.strip():
        return {"success": True, "codes": [], "elapsed_ms": 0}
    try:
        codes = _scrub_codes_payload(search_promo_codes(request.merchant))
        elapsed = int((_time.time() - t0) * 1000)
        return {"success": True, "codes": codes, "elapsed_ms": elapsed}
    except SCRAPE_FAILURE_EXCEPTIONS as exc:
        logger.error("Promo endpoint error (%s): %s", type(exc).__name__, exc)
        elapsed = int((_time.time() - t0) * 1000)
        return {
            "success": False,
            "codes": [],
            "elapsed_ms": elapsed,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Playwright fallbacks (re-exported from sibling module)
# ---------------------------------------------------------------------------

