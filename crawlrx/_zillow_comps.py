"""Zillow comparable-sales orchestrator (split out of ``zillow.py``).

Owns the multi-step navigation that produces ``ZillowCompsResponse``:

1. Land on Zillow homepage (session warmup).
2. Navigate to the recently-sold ZIP search page.
3. Random-scroll, evaluate the comps payload, build typed comps.
4. Best-effort lookup of the subject property's zestimate.

Each numbered step is its own helper; the orchestrator just composes
them under a single try / timeout boundary.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from . import _zillow_helpers as zh
from ._realestate_helpers import (
    is_captcha_blocked,
    scroll_page_randomly,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
)
from ._zillow_models import (
    ZillowComp,
    ZillowCompsRequest,
    ZillowCompsResponse,
)


logger = logging.getLogger("crawlrx.zillow.comps")


# ---------------------------------------------------------------------------
# Block-detection helper (one path, both pages share)
# ---------------------------------------------------------------------------

async def _blocked_response(
    page: Any, message: str, start_time: float,
) -> Optional[ZillowCompsResponse]:
    """Return a captcha-block response if ``page`` is being blocked."""
    if not await is_captcha_blocked(page):
        return None
    return ZillowCompsResponse(
        success=False,
        error=message,
        elapsed_ms=int((time.time() - start_time) * 1000),
    )


# ---------------------------------------------------------------------------
# Comp building
# ---------------------------------------------------------------------------

def _build_comps(
    listings_data: List[Dict[str, Any]], limit: int,
) -> List[ZillowComp]:
    """Map JS-comps rows to validated :class:`ZillowComp` objects."""
    comps: List[ZillowComp] = []
    for data in listings_data[:limit]:
        sqft_val = data.get("sqft")
        price_str = data.get("sale_price", "")
        ppsf_label = zh.price_per_sqft_label(price_str, sqft_val)
        try:
            comps.append(ZillowComp(
                address=data.get("address", ""),
                sale_price=price_str,
                sale_date=None,
                beds=data.get("beds"),
                baths=data.get("baths"),
                sqft=sqft_val,
                price_per_sqft=ppsf_label,
                distance_miles=None,
                url=data.get("url", ""),
            ))
        except PARSE_EXCEPTIONS:
            continue
    return comps


# ---------------------------------------------------------------------------
# Subject-property zestimate (best-effort)
# ---------------------------------------------------------------------------

async def _subject_zestimate(
    page: Any, address: str, zip_code: str,
) -> Optional[str]:
    """Best-effort subject-property zestimate lookup."""
    address_slug = address.replace(" ", "-")
    subject_url = f"{zh.ZILLOW_BASE}/homes/{address_slug}_{zip_code}_rb/"
    try:
        await page.goto(
            subject_url, wait_until="domcontentloaded", timeout=20000,
        )
        await asyncio.sleep(zh.random_uniform(2, 3))
        body_text = await page.inner_text("body", timeout=5000)
    except (PlaywrightTimeoutError,) + BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug(
            "Could not get zestimate for subject: %s", type(exc).__name__,
        )
        return None
    return zh.parse_zestimate(body_text or "")


# ---------------------------------------------------------------------------
# Orchestrator (composed of three small steps)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _NavStep:
    """Bundle for a navigate-then-block-check step (caps positional args)."""

    url: str
    block_message: str
    pause_low: float
    pause_high: float


async def _navigate_then_block_check(
    page: Any, step: _NavStep, start_time: float,
) -> Optional[ZillowCompsResponse]:
    """Navigate, settle, then return a block-response if captcha shows."""
    await page.goto(step.url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(zh.random_uniform(step.pause_low, step.pause_high))
    return await _blocked_response(page, step.block_message, start_time)


async def _land_homepage(
    page: Any, start_time: float,
) -> Optional[ZillowCompsResponse]:
    """Step 1: visit Zillow homepage; return a block-response on failure."""
    step = _NavStep(
        url=f"{zh.ZILLOW_BASE}/",
        block_message="blocked: challenge or environment-check on homepage",
        pause_low=2.0,
        pause_high=3.0,
    )
    return await _navigate_then_block_check(page, step, start_time)


async def _land_sold_page(
    page: Any, zip_code: str, start_time: float,
) -> Optional[ZillowCompsResponse]:
    """Step 2: visit the recently-sold ZIP page; return block-response on bail."""
    sold_url = f"{zh.ZILLOW_BASE}/homes/recently_sold/{zip_code}_rb/"
    logger.info("Searching sold listings: %s", sold_url)
    step = _NavStep(
        url=sold_url,
        block_message="blocked: challenge or environment-check on sold listings",
        pause_low=3.0,
        pause_high=5.0,
    )
    return await _navigate_then_block_check(page, step, start_time)


async def gather_comps(
    page: "Page", request: ZillowCompsRequest, start_time: float,
) -> ZillowCompsResponse:
    """Run the comps lookup against a stealth-rendered page."""
    early = await _land_homepage(page, start_time)
    if early is not None:
        return early
    blocked = await _land_sold_page(page, request.zip_code, start_time)
    if blocked is not None:
        return blocked
    await scroll_page_randomly(page)
    listings_data = await page.evaluate(zh.COMPS_LISTINGS_JS)
    comps = _build_comps(listings_data or [], request.limit)
    subject_zestimate = await _subject_zestimate(
        page, request.address, request.zip_code,
    )
    elapsed = int((time.time() - start_time) * 1000)
    logger.info("Found %d comps in %dms", len(comps), elapsed)
    return ZillowCompsResponse(
        success=True,
        subject_zestimate=subject_zestimate,
        comps=comps,
        count=len(comps),
        elapsed_ms=elapsed,
    )
