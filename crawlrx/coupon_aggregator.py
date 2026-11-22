"""Coupon aggregator for CrawlRX -- scrape coupon codes from 4 sites in parallel.

Module split: per-site bodies live as :class:`CouponSiteConfig`
configs that drive the shared scraper in :mod:`._coupon_helpers`.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from ._coupon_helpers import (
    CouponSiteConfig,
    aggregate_via_pool,
    extract_domain,
    extract_store_name,
    scrape_coupon_site,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class CouponAggregatorRequest(BaseModel):
    """Request body for coupon aggregation."""

    merchant: str


class CouponAggregatorResponse(BaseModel):
    """Response model for coupon aggregation."""

    success: bool
    codes: list[dict]
    total: int
    elapsed_ms: int


# ─────────────────────────────────────────────
# Per-site configs (replace duplicated scrape_* bodies)
# ─────────────────────────────────────────────

_COUPONFOLLOW = CouponSiteConfig(
    source="couponfollow",
    url_builder=lambda merchant: f"https://couponfollow.com/site/{extract_domain(merchant)}",
    code_selectors=(".coupon-code", ".code-text", ".promo-code", ".code"),
    discount_selectors=(".discount", ".offer", ".savings", ".deal", "[class*='save']"),
)


_WETHRIFT = CouponSiteConfig(
    source="wethrift",
    url_builder=lambda merchant: f"https://www.wethrift.com/{extract_store_name(merchant)}",
    code_selectors=(".code", ".coupon-code", ".promo-code", ".discount-code"),
    discount_selectors=(".discount", ".offer", ".savings", ".description", "[class*='save']"),
)


_COUPONCABIN = CouponSiteConfig(
    source="couponcabin",
    url_builder=lambda merchant: f"https://www.couponcabin.com/coupons/{extract_store_name(merchant)}/",
    code_selectors=(".coupon-code", ".code", ".promo-code", ".discount-code"),
    discount_selectors=(".discount", ".offer", ".savings", ".title", "[class*='save']"),
)


_DONTPAYFULL = CouponSiteConfig(
    source="dontpayfull",
    url_builder=lambda merchant: f"https://www.dontpayfull.com/at/{extract_domain(merchant)}",
    code_selectors=(".code", ".coupon-code", ".promo-code", ".discount-code"),
    discount_selectors=(".discount", ".offer", ".savings", ".description", "[class*='save']", "[class*='off']"),
)


# ─────────────────────────────────────────────
# Public per-site scrapers (preserved for callers + tests)
# ─────────────────────────────────────────────

def scrape_couponfollow(merchant: str) -> List[dict]:
    """Scrape coupon codes from CouponFollow."""
    return scrape_coupon_site(merchant, _COUPONFOLLOW)


def scrape_wethrift(merchant: str) -> List[dict]:
    """Scrape coupon codes from Wethrift."""
    return scrape_coupon_site(merchant, _WETHRIFT)


def scrape_couponcabin(merchant: str) -> List[dict]:
    """Scrape coupon codes from CouponCabin."""
    return scrape_coupon_site(merchant, _COUPONCABIN)


def scrape_dontpayfull(merchant: str) -> List[dict]:
    """Scrape coupon codes from DontPayFull."""
    return scrape_coupon_site(merchant, _DONTPAYFULL)


# ─────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────

_SCRAPERS = (
    ("couponfollow", scrape_couponfollow),
    ("wethrift", scrape_wethrift),
    ("couponcabin", scrape_couponcabin),
    ("dontpayfull", scrape_dontpayfull),
)


def aggregate_coupons(merchant: str) -> tuple[list[dict], int]:
    """Aggregate coupon codes from 4 sites in parallel."""
    return aggregate_via_pool(merchant, _SCRAPERS)


@router.post("/scrape/coupons")
def api_aggregate_coupons(request: CouponAggregatorRequest) -> dict:
    """POST /scrape/coupons -- aggregate coupon codes from 4 sites in parallel."""
    logger.info("POST /scrape/coupons - merchant: %s", request.merchant)
    codes, elapsed_ms = aggregate_coupons(request.merchant)
    return {
        "success": True,
        "codes": codes,
        "total": len(codes),
        "elapsed_ms": elapsed_ms,
    }
