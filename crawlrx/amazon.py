"""amazon.py -- POST /scrape/amazon.

Amazon product scraper using Playwright CDP.
Extracts product details from any Amazon product URL.
Handles: captcha detection, 3-retry with fresh context, networkidle wait.
"""

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from ._amazon_extract import (
    AmazonExtraction,
    extract_all_fields,
    parse_asin_from_url,
)
from ._ecommerce_product_helpers import (
    BROWSER_OR_HTTP_EXCEPTIONS,
    DEFAULT_CAPTCHA_SIGNALS,
    text_has_captcha,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow
from .models import AmazonScrapeRequest, AmazonScrapeResponse

logger = logging.getLogger("crawlrx.amazon")
router = APIRouter()

MAX_RETRIES = 3

_CAPTCHA_SIGNALS: tuple[str, ...] = DEFAULT_CAPTCHA_SIGNALS + (
    "robot check",
    "type the characters you see in this image",
)


async def _is_captcha_page(page: Any) -> bool:
    """Return True if the page is a captcha/bot-check page."""
    try:
        body = await page.inner_text("body") or ""
        title = await page.title() or ""
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "amazon captcha probe", exc)
        return False
    return text_has_captcha(body + " " + title, _CAPTCHA_SIGNALS)


async def _wait_for_idle(page: Any, timeout_ms: int) -> None:
    """Wait for ``networkidle``, swallowing the expected timeout."""
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
    except PlaywrightTimeoutError:
        pass


def _captcha_response(url: str, start_time: float) -> AmazonScrapeResponse:
    """Build the captcha-partial response."""
    elapsed_ms = int((time.time() - start_time) * 1000)
    return AmazonScrapeResponse(
        success=False,
        url=url,
        asin=parse_asin_from_url(url),
        error="Captcha detected",
        elapsed_ms=elapsed_ms,
    )


def _success_response(
    url: str, extraction: AmazonExtraction, start_time: float
) -> AmazonScrapeResponse:
    """Build the success response from a populated extraction bag."""
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "[Amazon] Scraped %s: title=%s price=%s rating=%s images=%d features=%d",
        extraction.asin or "unknown",
        (extraction.title or "")[:30],
        extraction.price or "None",
        extraction.rating or "None",
        len(extraction.images),
        len(extraction.features),
    )
    return AmazonScrapeResponse(
        success=True,
        url=url,
        asin=extraction.asin,
        title=extraction.title,
        price=extraction.price,
        rating=extraction.rating,
        review_count=extraction.review_count,
        availability=extraction.availability,
        brand=extraction.brand,
        images=extraction.images,
        description=extraction.description,
        features=extraction.features,
        variants=extraction.variants,
        elapsed_ms=elapsed_ms,
    )


async def _scrape_once(
    page: Any, url: str, timeout_ms: int, start_time: float
) -> AmazonScrapeResponse:
    """Navigate and extract from one page attempt. Returns AmazonScrapeResponse."""
    logger.info("[Amazon] Navigating to %s", url)
    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    await _wait_for_idle(page, timeout_ms)
    await page.wait_for_timeout(1500)

    if await _is_captcha_page(page):
        logger.warning("[Amazon] Captcha detected for %s -- returning partial", url)
        return _captcha_response(url, start_time)

    extraction = await extract_all_fields(page, url)
    return _success_response(url, extraction, start_time)


async def close_silently(obj: "Page | BrowserContext | Browser | None") -> None:
    """Close a Playwright page / context, swallowing narrow shutdown failures."""
    if not obj:
        return
    try:
        await obj.close()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "amazon cleanup", exc)


async def _try_attempt(
    browser: Any, url: str, timeout_ms: int, start_time: float
) -> tuple[Optional[AmazonScrapeResponse], str]:
    """Run a single attempt; return (response_or_None, last_error)."""
    context = None
    page = None
    last_error = ""
    try:
        context = await browser.new_context()
        page = await context.new_page()
        result = await _scrape_once(page, url, timeout_ms, start_time)
        if not result.success and result.error and "Captcha" in result.error:
            return result, ""
        if result.success:
            return result, ""
        last_error = result.error or "Scrape returned no data"
    except PlaywrightTimeoutError as exc:
        last_error = f"Timeout: {exc}"
    except BROWSER_OR_HTTP_EXCEPTIONS as exc:
        last_error = str(exc)
    finally:
        await close_silently(page)
        await close_silently(context)
    return None, last_error


async def _do_scrape_amazon(url: str, timeout_ms: int) -> AmazonScrapeResponse:
    """Sync Amazon product scrape using CDP. Retries up to MAX_RETRIES with fresh context."""
    start_time = time.time()
    last_error = "Unknown error"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        for attempt in range(1, MAX_RETRIES + 1):
            response, attempt_error = await _try_attempt(
                browser, url, timeout_ms, start_time
            )
            if response is not None:
                return response
            last_error = attempt_error or last_error
            logger.warning(
                "[Amazon] Attempt %d/%d failed: %s", attempt, MAX_RETRIES, last_error
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.error("[Amazon] All %d attempts failed for %s: %s", MAX_RETRIES, url, last_error)
    return AmazonScrapeResponse(
        success=False,
        url=url,
        asin=parse_asin_from_url(url),
        error=f"All retries failed: {last_error}",
        elapsed_ms=elapsed_ms,
    )


@router.post(
    "/scrape/amazon",
    response_model=AmazonScrapeResponse,
    summary="Scrape Amazon product",
)
async def scrape_amazon(request: AmazonScrapeRequest) -> AmazonScrapeResponse:
    """Scrape an Amazon product page and extract product details.

    - **url**: Amazon product URL (e.g., "https://www.amazon.com/dp/B08N5WRWNW")
    - **session_id**: Optional session ID (unused).

    Extracts: title, price, rating, review_count, ASIN, availability, brand,
    images (list), description, features (bullets), variants (size/color).
    Detects captcha and returns partial result with error flag.
    Retries up to 3 times with a fresh context on failure.
    """
    timeout_seconds = 90  # 3 retries * 30s each
    try:
        return await asyncio.wait_for(
            _do_scrape_amazon(request.url, 30000),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, BROWSER_OR_HTTP_EXCEPTIONS) as exc:
        logger.error("[Amazon] Request failed: %s", exc)
        return AmazonScrapeResponse(
            success=False,
            url=request.url,
            error=f"Request failed: {exc}",
            elapsed_ms=timeout_seconds * 1000,
        )
