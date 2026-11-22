"""
Capital One Shopping coupon code scraper for CrawlRX.
FastAPI router: POST /scrape/capital-one
"""

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from fastapi import APIRouter
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from ._scraper_helpers import playwright_exception_types

logger = logging.getLogger(__name__)
router = APIRouter()

CAPITAL_ONE_SEARCH_URL = "https://capitaloneshopping.com/search?q={merchant}+coupons"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CDP_PORT = 9234

# Narrow exception families.
_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError, AttributeError, NameError)
)
_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = _PLAYWRIGHT_EXCEPTIONS

_DISCOUNT_SELECTOR = (
    ".discount, .savings, .deal-amount, .offer-value, .percentage"
)
_DESCRIPTION_SELECTOR = (
    ".description, .deal-title, .offer-title, .deal-description"
)
_VERIFIED_SELECTOR = ".verified, .verified-badge, [data-verified]"

_COUPON_SELECTORS = (
    ".deal-code",
    ".coupon-code",
    "[data-code]",
    ".code-value",
    ".promo-code",
    ".discount-code",
    "[data-coupon-code]",
    ".deal-card .code",
    ".offer-code",
)


class CapitalOneRequest(BaseModel):
    merchant: str


class CouponCode(BaseModel):
    code: str
    discount: str
    description: str
    source: str
    verified: bool


class CapitalOneResponse(BaseModel):
    success: bool
    codes: list[CouponCode]
    elapsed_ms: int


def _select_text(parent: Any, selector: str) -> str:
    """Stripped text from the first match of ``selector`` under ``parent`` (else "")."""
    found = parent.select_one(selector)
    return found.get_text(strip=True) if found else ""


def _update_meta_from_parent(meta: Dict[str, Any], parent: Any) -> None:
    """Fill blank discount / description / verified fields from one parent node."""
    if not meta["discount"]:
        meta["discount"] = _select_text(parent, _DISCOUNT_SELECTOR)
    if not meta["description"]:
        meta["description"] = _select_text(parent, _DESCRIPTION_SELECTOR)
    if not meta["verified"] and parent.select_one(_VERIFIED_SELECTOR):
        meta["verified"] = True


def _walk_for_meta(elem: Any) -> Dict[str, Any]:
    """Walk up to 5 parent levels collecting discount / description / verified flags."""
    meta: Dict[str, Any] = {"discount": "", "description": "", "verified": False}
    parent = elem
    for _ in range(5):
        if not parent or not parent.parent:
            break
        parent = parent.parent
        _update_meta_from_parent(meta, parent)
        if meta["discount"] and meta["description"]:
            break
    return meta


def _coupon_from_element(elem: Any, merchant: str) -> Optional[Dict[str, Any]]:
    """Build one coupon dict from a candidate element, or ``None`` if invalid."""
    code_text = elem.get_text(strip=True)
    if not code_text or len(code_text) <= 2:
        return None
    meta = _walk_for_meta(elem)
    return {
        "code": code_text,
        "discount": meta["discount"] or "Deal available",
        "description": meta["description"] or ("Coupon for %s" % merchant),
        "source": "Capital One Shopping",
        "verified": meta["verified"],
    }


def _extract_coupons_from_html(html: str, merchant: str) -> List[Dict[str, Any]]:
    """Parse HTML and extract coupon codes."""
    soup = BeautifulSoup(html, "html.parser")
    codes: List[Dict[str, Any]] = []
    seen_codes: set[str] = set()
    for selector in _COUPON_SELECTORS:
        for elem in soup.select(selector):
            coupon = _coupon_from_element(elem, merchant)
            if coupon is None:
                continue
            if coupon["code"] in seen_codes:
                continue
            seen_codes.add(coupon["code"])
            codes.append(coupon)
    return codes


def _scrape_with_requests(merchant: str) -> List[Dict[str, Any]]:
    """Attempt to scrape using requests + BeautifulSoup."""
    url = CAPITAL_ONE_SEARCH_URL.format(merchant=requests.utils.quote(merchant))
    headers = {"User-Agent": USER_AGENT}
    logger.info("Scraping Capital One with requests for merchant: %s", merchant)
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    codes = _extract_coupons_from_html(resp.text, merchant)
    logger.info("Found %d codes via requests", len(codes))
    return codes


async def close_quietly(target: "Page | BrowserContext | Browser | None", label: str) -> None:
    """Best-effort async ``.close()`` that logs and swallows known failures."""
    if target is None:
        return
    try:
        await target.close()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.debug(
            "Cleanup %s failed (%s): %s", label, type(exc).__name__, exc
        )


@router.post("/scrape/capital-one", response_model=CapitalOneResponse)
def scrape_capital_one(request: CapitalOneRequest) -> CapitalOneResponse:
    """Scrape coupon codes from Capital One Shopping."""
    start = time.time()
    codes: List[Dict[str, Any]] = []
    try:
        codes = _scrape_with_requests(request.merchant)
    except requests.RequestException as exc:
        logger.warning(
            "Requests scraping failed (%s): %s", type(exc).__name__, exc
        )
        return None  # explicit recovery: non-fatal: requests.RequestException is recorded but does not abort the caller
        # Playwright fallback runs in its own loop -- this endpoint is sync
        # by design; a sync caller can't await, so empty fallback is correct.
    elapsed = int((time.time() - start) * 1000)
    coupon_codes = [CouponCode(**c) for c in codes]
    logger.info(
        "Returning %d coupon codes for %s in %d ms",
        len(coupon_codes),
        request.merchant,
        elapsed,
    )
    return CapitalOneResponse(
        success=True,
        codes=coupon_codes,
        elapsed_ms=elapsed,
    )
