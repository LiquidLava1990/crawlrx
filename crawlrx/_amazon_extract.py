"""_amazon_extract.py -- Amazon product-detail page DOM extraction helpers.

Sibling module for ``amazon.py``. Houses the per-field probes that previously
lived inside one 160-LOC ``_scrape_once`` function. Each probe is wrapped in a
narrow exception handler and is independently testable.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._ecommerce_product_helpers import (
    parse_amazon_rating,
    parse_asin_from_url,
    parse_review_count_int,
)
from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS, log_and_swallow

logger = logging.getLogger("crawlrx._amazon_extract")

_THUMB_RE = re.compile(r"\._[A-Z0-9_]+_\.")

_PRICE_SELECTORS: tuple[str, ...] = (
    "#priceblock_ourprice",
    ".a-price .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_dealprice",
    ".a-price-range .a-offscreen",
    "#priceblock_saleprice",
    "#apex_offerDisplay_desktop .a-price .a-offscreen",
)

_DESCRIPTION_SELECTORS: tuple[str, ...] = (
    "#productDescription p",
    "#productDescription",
    "#aplus p",
)


@dataclass
class AmazonExtraction:
    """Bag of per-field extraction results for one product page."""

    title: Optional[str] = None
    price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    asin: Optional[str] = None
    availability: Optional[str] = None
    brand: Optional[str] = None
    description: Optional[str] = None
    images: List[str] = field(default_factory=list)
    features: List[str] = field(default_factory=list)
    variants: List[dict] = field(default_factory=list)


async def _safe_inner_text(element: Any) -> Optional[str]:
    """Safely read ``element.inner_text()``; None on missing / probe failure."""
    if element is None:
        return None
    try:
        text = await element.inner_text()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, "amazon inner_text", exc)
        return None
    return (text or "").strip() or None


async def _query_text(page: Any, selector: str) -> Optional[str]:
    """Run a single query_selector and return the inner-text strip, or None."""
    try:
        element = await page.query_selector(selector)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"amazon q({selector})", exc)
        return None
    return await _safe_inner_text(element)


async def _query_attr(page: Any, selector: str, attr: str) -> Optional[str]:
    """Run a single query_selector and return ``attr`` or None."""
    try:
        element = await page.query_selector(selector)
        if element is None:
            return None
        value = await element.get_attribute(attr)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"amazon attr({selector})", exc)
        return None
    return value


async def _extract_title(page: Any) -> Optional[str]:
    """Read the product title."""
    return await _query_text(page, "#productTitle")


async def _extract_price(page: Any) -> Optional[str]:
    """Try each Amazon price selector, returning the first non-empty match."""
    for selector in _PRICE_SELECTORS:
        value = await _query_text(page, selector)
        if value:
            return value
    return None


async def _extract_rating(page: Any) -> Optional[float]:
    """Read the product star-rating."""
    title_text = await _query_attr(page, "#acrPopover", "title")
    rating = parse_amazon_rating(title_text)
    if rating is not None:
        return rating
    fallback_text = await _query_text(page, ".a-icon-alt")
    return parse_amazon_rating(fallback_text)


async def _extract_review_count(page: Any) -> Optional[int]:
    """Read the integer review count."""
    text = await _query_text(page, "#acrCustomerReviewText")
    return parse_review_count_int(text)


async def _resolve_asin(page: Any, url: str) -> Optional[str]:
    """Resolve ASIN via URL first, then the hidden input."""
    asin = parse_asin_from_url(url)
    if asin:
        return asin
    return await _query_attr(page, "#ASIN", "value")


async def _extract_availability(page: Any) -> Optional[str]:
    """Read the buybox availability message."""
    return await _query_text(page, "#availability span")


async def _extract_brand(page: Any) -> Optional[str]:
    """Read the byline / brand string."""
    return await _query_text(page, "#bylineInfo")


async def _extract_description(page: Any) -> Optional[str]:
    """Try each description selector; return the first non-empty text."""
    for selector in _DESCRIPTION_SELECTORS:
        value = await _query_text(page, selector)
        if value:
            return value
    return None


def _to_full_image(src: str) -> str:
    """Convert an Amazon thumbnail URL to its full-size variant."""
    return _THUMB_RE.sub(".", src)


async def _safe_query_all(page: Any, selector: str) -> List[Any]:
    """Run query_selector_all under a narrow exception guard."""
    try:
        return await page.query_selector_all(selector) or []
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"amazon qa({selector})", exc)
        return []


async def _collect_thumbnail_images(page: Any) -> List[str]:
    """Gather full-size image URLs from the main thumbnail strip."""
    elements = await _safe_query_all(page, "#altImages img, #imageBlock img")
    images: List[str] = []
    seen: set[str] = set()
    for img in elements:
        try:
            src = await img.get_attribute("src") or ""
        except PLAYWRIGHT_EXCEPTIONS as exc:
            log_and_swallow(logger, logging.DEBUG, "amazon image attr", exc)
            continue
        full = _to_full_image(src)
        if full and full not in seen and "transparent-pixel" not in full:
            seen.add(full)
            images.append(full)
    return images


async def _landing_image_fallback(page: Any) -> List[str]:
    """Single-element fallback when the thumbnail strip yields nothing."""
    src = await _query_attr(page, "#landingImage", "src") or await _query_attr(
        page, "#landingImage", "data-old-hires"
    )
    return [src] if src else []


async def _extract_images(page: Any) -> List[str]:
    """Collect all product images, preferring the thumbnail strip."""
    images = await _collect_thumbnail_images(page)
    if images:
        return images
    return await _landing_image_fallback(page)


async def _collect_text_list(page: Any, selector: str) -> List[str]:
    """Read inner-text from each element matching ``selector``."""
    elements = await _safe_query_all(page, selector)
    out: List[str] = []
    for element in elements:
        text = await _safe_inner_text(element)
        if text:
            out.append(text)
    return out


async def _extract_features(page: Any) -> List[str]:
    """Read the bullet-list features."""
    return await _collect_text_list(page, "#feature-bullets li span.a-list-item")


async def _extract_variants(page: Any) -> List[dict]:
    """Read the size / color twister variants as ``{label}`` dicts."""
    labels = await _collect_text_list(page, "#twister .a-button-text, #twister li")
    return [{"label": label} for label in labels]


async def extract_all_fields(page: "Page", url: str) -> AmazonExtraction:
    """Drive every per-field probe and return the populated extraction."""
    return AmazonExtraction(
        title=await _extract_title(page),
        price=await _extract_price(page),
        rating=await _extract_rating(page),
        review_count=await _extract_review_count(page),
        asin=await _resolve_asin(page, url),
        availability=await _extract_availability(page),
        brand=await _extract_brand(page),
        description=await _extract_description(page),
        images=await _extract_images(page),
        features=await _extract_features(page),
        variants=await _extract_variants(page),
    )
