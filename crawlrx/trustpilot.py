"""trustpilot.py -- Trustpilot scraper.

Endpoints:
  POST /scrape/trustpilot/company -- scrape a company on Trustpilot
  POST /scrape/trustpilot/reviews -- get company reviews

Uses direct HTTP + BeautifulSoup as the primary path (Trustpilot is
largely server-rendered) with a Playwright fallback for blocked /
challenge-protected pages.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

import requests
from fastapi import APIRouter
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field

from ._trustpilot_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    HTTP_HEADERS,
    PARSE_EXCEPTIONS,
    TRUSTPILOT_BASE_URL,  # noqa: F401  (re-exported for back-compat callers)
    collect_http_reviews,
    collect_pw_reviews,
    extract_company_name,
    extract_response_rate,
    extract_star_percentages,
    extract_total_reviews,
    extract_trustscore,
    extract_website,
    get_company_url,
    random_delay,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.trustpilot")
router = APIRouter(prefix="/scrape/trustpilot", tags=["trustpilot"])


# Compile-once: HTML parsing patterns are constant; re-compiling per
# `_build_company` call paid the same O(pattern-length) cost on every request.
_RATING_LABEL_CLASS_RE = re.compile("ratingLabel")
_REVIEW_CARD_CLASS_RE = re.compile("reviewCard|review-card")
_CLAIMED_TEXT_RE = re.compile(r"claimed", re.I)


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class RatingBreakdown(BaseModel):
    """Rating breakdown by star level."""
    five_star: Optional[float] = Field(default=None)
    four_star: Optional[float] = Field(default=None)
    three_star: Optional[float] = Field(default=None)
    two_star: Optional[float] = Field(default=None)
    one_star: Optional[float] = Field(default=None)


class TrustpilotReview(BaseModel):
    """Single Trustpilot review."""
    title: Optional[str] = Field(default=None)
    rating: Optional[int] = Field(default=None)
    text: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    date: Optional[str] = Field(default=None)
    verified_purchase: bool = Field(default=False)
    company_reply: Optional[str] = Field(default=None)
    helpful_count: Optional[int] = Field(default=None)


class TrustpilotCompany(BaseModel):
    """Trustpilot company data."""
    name: str = Field(...)
    trustscore: Optional[float] = Field(default=None)
    rating_label: Optional[str] = Field(default=None)
    total_reviews: Optional[int] = Field(default=None)
    rating_breakdown: RatingBreakdown = Field(default_factory=RatingBreakdown)
    website: Optional[str] = Field(default=None)
    claimed: bool = Field(default=False)
    response_rate: Optional[str] = Field(default=None)
    recent_reviews: List[TrustpilotReview] = Field(default_factory=list)


class TrustpilotCompanyRequest(BaseModel):
    """Request model for POST /scrape/trustpilot/company."""
    company_name: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)


class TrustpilotReviewsRequest(BaseModel):
    """Request model for POST /scrape/trustpilot/reviews."""
    company_name: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    limit: int = Field(default=10, ge=1, le=100)
    sort: str = Field(default="recency", description="recency|relevance")


class TrustpilotCompanyResponse(BaseModel):
    """Response model for POST /scrape/trustpilot/company."""
    success: bool
    company: Optional[TrustpilotCompany] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


class TrustpilotReviewsResponse(BaseModel):
    """Response model for POST /scrape/trustpilot/reviews."""
    success: bool
    reviews: List[TrustpilotReview] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# HTTP scrape (primary path)
# ─────────────────────────────────────────────

def _import_bs4() -> Optional[Any]:
    """Return the BeautifulSoup class or None when bs4 is missing."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("BeautifulSoup not available")
        return None
    return BeautifulSoup


def _http_get(url: str) -> Optional[str]:
    """Fetch ``url`` with the canonical Trustpilot headers; None on HTTP error."""
    try:
        response = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.warning("HTTP request failed for Trustpilot: %s", exc)
        return None


def _build_company(soup: Any) -> TrustpilotCompany:
    """Map a soup of the company page into a typed ``TrustpilotCompany``."""
    label_el = soup.find("p", class_=_RATING_LABEL_CLASS_RE)
    rating_label = label_el.get_text(strip=True) if label_el else None
    review_cards = soup.find_all("div", class_=_REVIEW_CARD_CLASS_RE)
    recent = collect_http_reviews(review_cards[:5], TrustpilotReview)
    return TrustpilotCompany(
        name=extract_company_name(soup),
        trustscore=extract_trustscore(soup),
        rating_label=rating_label,
        total_reviews=extract_total_reviews(soup),
        rating_breakdown=extract_star_percentages(soup, RatingBreakdown),
        website=extract_website(soup),
        claimed=bool(soup.find(text=_CLAIMED_TEXT_RE)),
        response_rate=extract_response_rate(soup),
        recent_reviews=recent,
    )


def _scrape_company_http(company_url: str) -> Optional[TrustpilotCompany]:
    """Scrape Trustpilot company using HTTP + BeautifulSoup."""
    BeautifulSoup = _import_bs4()
    if BeautifulSoup is None:
        return None
    logger.info("[Trustpilot] HTTP scraping: %s", company_url)
    body = _http_get(company_url)
    if body is None:
        return None
    try:
        soup = BeautifulSoup(body, "html.parser")
        return _build_company(soup)
    except PARSE_EXCEPTIONS as exc:
        logger.error(
            "Error scraping Trustpilot company via HTTP (%s): %s",
            type(exc).__name__, exc,
        )
        return None


def _build_reviews_url(company_url: str, sort: str) -> str:
    """Append the sort param to the company URL."""
    sort_param = "recency" if sort == "recency" else "relevance"
    return f"{company_url}?sort={sort_param}"


def _scrape_reviews_http(
    company_url: str, limit: int, sort: str,
) -> List[TrustpilotReview]:
    """Scrape Trustpilot reviews using HTTP + BeautifulSoup."""
    BeautifulSoup = _import_bs4()
    if BeautifulSoup is None:
        return []
    reviews_url = _build_reviews_url(company_url, sort)
    logger.info("[Trustpilot] HTTP scraping reviews: %s", reviews_url)
    body = _http_get(reviews_url)
    if body is None:
        return []
    try:
        soup = BeautifulSoup(body, "html.parser")
    except PARSE_EXCEPTIONS as exc:
        logger.error(
            "Error scraping Trustpilot reviews via HTTP (%s): %s",
            type(exc).__name__, exc,
        )
        return []
    cards = soup.find_all("div", class_=_REVIEW_CARD_CLASS_RE)
    return collect_http_reviews(cards, TrustpilotReview, limit=limit)


# ─────────────────────────────────────────────
# Playwright fallback (browser path)
# ─────────────────────────────────────────────

async def _wait_for_h1(page: Any) -> None:
    """Best-effort wait for an ``<h1>`` -- only Playwright timeouts swallowed."""
    try:
        await page.wait_for_selector("h1", timeout=15000)
    except PlaywrightTimeoutError:
        logger.warning("Timeout waiting for Trustpilot company page")
        return None  # explicit recovery: best-effort wait; PlaywrightTimeoutError is non-fatal


async def _pw_inner_text(page: Any, selector: str) -> Optional[str]:
    """``page.query_selector(selector).inner_text()`` -- None on miss."""
    el = await page.query_selector(selector)
    if not el:
        return None
    text = await el.inner_text()
    return text.strip() if isinstance(text, str) else None


async def _pw_company_name(page: Any) -> str:
    """Pick the first ``<h1>`` text or ``"Unknown"``."""
    text = await _pw_inner_text(page, "h1")
    return text or "Unknown"


async def _pw_company_trustscore(page: Any) -> Optional[float]:
    """Parse a numeric trust-score from the canonical Playwright selectors."""
    text = await _pw_inner_text(page, "[data-testid='trust-score'], .trustScore")
    if not text:
        return None
    match = re.search(r"(\d+\.?\d*)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def _pw_company_total_reviews(page: Any) -> Optional[int]:
    """Parse total-reviews integer from the Playwright DOM."""
    text = await _pw_inner_text(page, "[data-testid='total-reviews']")
    if not text:
        return None
    match = re.search(r"([\d,]+)", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


async def _pw_company_website(page: Any) -> Optional[str]:
    """Resolve the company website href if present in the DOM."""
    el = await page.query_selector("[data-testid='company-website']")
    if not el:
        return None
    return await el.get_attribute("href")


async def _scrape_company_playwright(
    company_url: str,
) -> Optional[TrustpilotCompany]:
    """Scrape a Trustpilot company page using Playwright (fallback path)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            logger.info("[Trustpilot] Playwright scraping: %s", company_url)
            await page.goto(company_url, wait_until="domcontentloaded", timeout=45000)
            await random_delay(1000, 2000)
            await _wait_for_h1(page)
            review_cards = await page.query_selector_all(
                "[data-testid='review-card']",
            )
            recent = await collect_pw_reviews(review_cards[:5], TrustpilotReview, 5)
            return TrustpilotCompany(
                name=await _pw_company_name(page),
                trustscore=await _pw_company_trustscore(page),
                total_reviews=await _pw_company_total_reviews(page),
                website=await _pw_company_website(page),
                recent_reviews=recent,
            )
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "Playwright scraping failed for Trustpilot (%s): %s",
            type(exc).__name__, exc,
        )
        return None
    finally:
        await close_stealth_context(browser, ctx, page)


async def _scrape_reviews_playwright(
    company_url: str, limit: int, sort: str,
) -> List[TrustpilotReview]:
    """Scrape Trustpilot reviews via Playwright (fallback path)."""
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            reviews_url = _build_reviews_url(company_url, sort)
            logger.info("[Trustpilot] Playwright scraping reviews: %s", reviews_url)
            await page.goto(reviews_url, wait_until="domcontentloaded", timeout=45000)
            await random_delay(1000, 2000)
            cards = await page.query_selector_all("[data-testid='review-card']")
            return await collect_pw_reviews(cards, TrustpilotReview, limit)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "Playwright scraping failed for Trustpilot reviews (%s): %s",
            type(exc).__name__, exc,
        )
        return []
    finally:
        await close_stealth_context(browser, ctx, page)


# ─────────────────────────────────────────────
# Outer orchestration
# ─────────────────────────────────────────────

async def _scrape_company(
    request: TrustpilotCompanyRequest,
) -> TrustpilotCompanyResponse:
    """Try HTTP first; fall back to Playwright on miss."""
    start = timer_ms()
    try:
        company_url = get_company_url(request.company_name, request.url)
    except ValueError as exc:
        elapsed = int(timer_ms() - start)
        return TrustpilotCompanyResponse(
            success=False, error=str(exc), elapsed_ms=elapsed,
        )
    company = _scrape_company_http(company_url)
    if company is None:
        logger.info("HTTP scraping failed, trying Playwright fallback")
        company = await _scrape_company_playwright(company_url)
    elapsed = int(timer_ms() - start)
    if company is not None:
        return TrustpilotCompanyResponse(
            success=True, company=company, elapsed_ms=elapsed,
        )
    return TrustpilotCompanyResponse(
        success=False, error="Failed to scrape company data", elapsed_ms=elapsed,
    )


async def _scrape_reviews(
    request: TrustpilotReviewsRequest,
) -> TrustpilotReviewsResponse:
    """Try HTTP first; fall back to Playwright on empty result."""
    start = timer_ms()
    try:
        company_url = get_company_url(request.company_name, request.url)
    except ValueError as exc:
        elapsed = int(timer_ms() - start)
        return TrustpilotReviewsResponse(
            success=False, error=str(exc), elapsed_ms=elapsed,
        )
    reviews = _scrape_reviews_http(company_url, request.limit, request.sort)
    if not reviews:
        logger.info("HTTP scraping returned no reviews, trying Playwright fallback")
        reviews = await _scrape_reviews_playwright(
            company_url, request.limit, request.sort,
        )
    elapsed = int(timer_ms() - start)
    return TrustpilotReviewsResponse(
        success=True, reviews=reviews, count=len(reviews), elapsed_ms=elapsed,
    )


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/company", response_model=TrustpilotCompanyResponse)
async def trustpilot_company(
    request: TrustpilotCompanyRequest,
) -> TrustpilotCompanyResponse:
    """Scrape a company on Trustpilot (HTTP-first, Playwright fallback)."""
    return await _scrape_company(request)


@router.post("/reviews", response_model=TrustpilotReviewsResponse)
async def trustpilot_reviews(
    request: TrustpilotReviewsRequest,
) -> TrustpilotReviewsResponse:
    """Get company reviews from Trustpilot (HTTP-first, Playwright fallback)."""
    return await _scrape_reviews(request)
