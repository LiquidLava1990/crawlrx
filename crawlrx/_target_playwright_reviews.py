"""Review extraction for the Target Playwright path."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)
from ._target_models import TargetProductDetail, TargetReview

logger = logging.getLogger("crawlrx.target.playwright.reviews")


async def _await_inner_text(card: Any, selector: str) -> Optional[str]:
    """Return the inner text of a sub-element, or ``None``."""
    el = await card.query_selector(selector)
    if el is None:
        return None
    text = await el.inner_text()
    return text.strip() if text else None


async def _first_int_for(card: Any, selector: str) -> Optional[int]:
    """Resolve the first integer in the inner text of ``selector``."""
    text = await _await_inner_text(card, selector)
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _review_rating(card: Any) -> Optional[int]:
    """Pull a 1-5 star rating off a review card."""
    return await _first_int_for(card, "[data-test='review-rating']")


async def _review_verified(card: Any) -> bool:
    """Probe the review card for the verified-purchase badge."""
    try:
        el = await card.query_selector("text=/verified purchase/i")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("verified probe suppressed: %s: %s", type(exc).__name__, exc)
        return False
    return el is not None


async def _review_helpful(card: Any) -> Optional[int]:
    """Pull the helpful-count integer off a review card."""
    return await _first_int_for(card, "[data-test='helpful-count']")


async def _build_review(card: Any) -> Optional[TargetReview]:
    """Build a single :class:`TargetReview` from a Playwright review card."""
    try:
        title = await _await_inner_text(card, "[data-test='review-title']")
        rating = await _review_rating(card)
        text = await _await_inner_text(card, "[data-test='review-text']")
        author = await _await_inner_text(card, "[data-test='review-author']")
        date = await _await_inner_text(card, "[data-test='review-date']")
        verified = await _review_verified(card)
        helpful = await _review_helpful(card)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("review build skipped: %s: %s", type(exc).__name__, exc)
        return None
    return TargetReview(
        title=title, rating=rating, text=text, author=author, date=date,
        verified_purchase=verified, helpful_count=helpful,
    )


async def populate_product_reviews(page: "Page", product: TargetProductDetail) -> None:
    """Populate ``product.top_reviews`` from up to five review cards."""
    try:
        cards = await page.query_selector_all("[data-test='review-card']")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("reviews suppressed: %s: %s", type(exc).__name__, exc)
        return
    for card in cards[:5]:
        review = await _build_review(card)
        if review is not None:
            product.top_reviews.append(review)


__all__ = ("populate_product_reviews",)
