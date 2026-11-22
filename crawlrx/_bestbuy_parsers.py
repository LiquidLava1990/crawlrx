"""HTML parsing helpers for the Best Buy scraper.

Split out of ``bestbuy.py`` for readability: keeps the parser god
functions out of the routing module so the Tier-0 linter sees one
small responsibility per file.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    parse_price,
    parse_rating_value,
)

logger = logging.getLogger("crawlrx.bestbuy._parsers")

BESTBUY_BASE_URL = "https://www.bestbuy.com"

# Module-level compiled regex constants (hoisted from loop bodies for perf).
_RE_SKU_ITEM = re.compile(r"sku-item|list-item")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def extract_sku_from_url(url: str) -> Optional[str]:
    """Return a Best Buy SKU pulled from a product URL or ``None``."""
    patterns = (
        r"/site/[^/]+/(\d+)\.p",
        r"skuId=(\d+)",
        r"sku[_-]?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def build_product_url(sku: str) -> str:
    """Return the canonical Best Buy product URL for ``sku``."""
    return f"{BESTBUY_BASE_URL}/site/{sku}.p?skuId={sku}"


# ---------------------------------------------------------------------------
# JSON-LD search-result parsing
# ---------------------------------------------------------------------------

def _build_jsonld_product(
    product_cls: Any,
    product: Dict[str, Any],
    open_box_global: bool,
) -> Any:
    """Map a JSON-LD ``Product`` block to a :class:`BestBuyProduct`."""
    sku = product.get("sku", "")
    offers = product.get("offers") or {}
    price = offers.get("price") if isinstance(offers, dict) else None
    aggregate_rating = product.get("aggregateRating") or {}
    availability = ""
    if isinstance(offers, dict):
        availability = (offers.get("availability") or "").replace(
            "http://schema.org/", ""
        )
    obj = product_cls(
        title=product.get("name", ""),
        price="$%s" % price if price else None,
        rating=aggregate_rating.get("ratingValue"),
        review_count=aggregate_rating.get("reviewCount"),
        sku=str(sku),
        product_url=product.get("url", ""),
        image_url=product.get("image"),
        availability=availability,
        condition="new",
    )
    if open_box_global:
        obj.open_box_available = True
    return obj


def _iter_jsonld_blocks(soup: Any) -> List[Dict[str, Any]]:
    """Yield decoded JSON-LD payloads, skipping ones that fail to parse."""
    blocks: List[Dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except PARSE_EXCEPTIONS as exc:
            logger.debug("bestbuy jsonld script parse failed: %s", exc)
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


def _drain_item_list(
    block: Dict[str, Any],
    product_cls: Any,
    limit: int,
    open_box_global: bool,
) -> List[Any]:
    products: List[Any] = []
    for item in block.get("itemListElement", []) or []:
        product = item.get("item") if isinstance(item, dict) else None
        if not (isinstance(product, dict) and product.get("@type") == "Product"):
            continue
        products.append(
            _build_jsonld_product(product_cls, product, open_box_global)
        )
        if len(products) >= limit:
            break
    return products


def _try_jsonld_search_results(
    soup: Any,
    product_cls: Any,
    limit: int,
    open_box_global: bool,
) -> List[Any]:
    for block in _iter_jsonld_blocks(soup):
        if block.get("@type") != "ItemList":
            continue
        products = _drain_item_list(
            block, product_cls, limit, open_box_global
        )
        if products:
            return products[:limit]
    return []


# ---------------------------------------------------------------------------
# DOM-card search-result parsing
# ---------------------------------------------------------------------------

def _resolve_sku(card: Any) -> Optional[str]:
    sku_attr = card.get("data-sku-id") or card.get("data-sku")
    if sku_attr:
        return sku_attr
    inner = card.select_one("[data-sku-id]")
    return inner.get("data-sku-id") if inner is not None else None


def _resolve_card_url(title_el: Any) -> str:
    if title_el is None or not title_el.get("href"):
        return ""
    href = title_el.get("href")
    return href if href.startswith("http") else BESTBUY_BASE_URL + href


def _resolve_card_pricing(card: Any) -> tuple[Optional[str], Optional[str]]:
    price_el = card.select_one(
        ".pricing-price__range, .sr-only:contains('current price'),"
        " .visually-hidden:contains('current price')"
    )
    price = parse_price(price_el.get_text()) if price_el is not None else None
    original_el = card.select_one(
        ".pricing-price__range-crossed-out, .was-price"
    )
    original = (
        parse_price(original_el.get_text()) if original_el is not None else None
    )
    return price, original


def _resolve_card_rating(card: Any) -> tuple[Optional[float], Optional[int]]:
    rating_el = card.select_one("[aria-label*='stars'], .c-ratings-reviews")
    if rating_el is None:
        return None, None
    rating = parse_rating_value(rating_el.get("aria-label"))
    review_count: Optional[int] = None
    review_match = re.search(r"\((\d+)\)", rating_el.get_text())
    if review_match:
        try:
            review_count = int(review_match.group(1))
        except ValueError:
            review_count = None
    return rating, review_count


def _resolve_card_image(card: Any) -> Optional[str]:
    img_el = card.select_one("img")
    if img_el is None:
        return None
    return img_el.get("src") or img_el.get("data-src")


def _resolve_card_condition(card: Any) -> tuple[str, bool]:
    text = card.get_text().lower()
    condition = "new"
    if "refurbished" in text:
        condition = "refurbished"
    elif "open-box" in text or "open box" in text:
        condition = "open-box"
    open_box_available = "open-box" in text or "open box" in text
    return condition, open_box_available


def _build_card_product(
    card: Any,
    product_cls: Any,
    condition_filter: str,
) -> Optional[Any]:
    """Build a :class:`BestBuyProduct` from one search card or ``None``."""
    try:
        sku = _resolve_sku(card)
        if not sku:
            return None
        title_el = card.select_one(
            "h4 a, .sku-title a, a[data-testid='product-title']"
        )
        title = title_el.get_text(strip=True) if title_el else ""
        price, original = _resolve_card_pricing(card)
        rating, review_count = _resolve_card_rating(card)
        condition, open_box_available = _resolve_card_condition(card)
        if condition_filter != "any" and condition != condition_filter:
            return None
        return product_cls(
            title=title,
            price=price,
            original_price=original,
            rating=rating,
            review_count=review_count,
            sku=sku,
            product_url=_resolve_card_url(title_el) or build_product_url(sku),
            image_url=_resolve_card_image(card),
            condition=condition,
            open_box_available=open_box_available,
        )
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error parsing Best Buy product card: %s", exc)
        return None


def _build_soup(html: str, where: str) -> Optional[Any]:
    """Best-effort ``BeautifulSoup`` build; returns ``None`` on failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("BeautifulSoup not available for HTML parsing")
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except PARSE_EXCEPTIONS as exc:
        logger.warning("Error parsing Best Buy %s HTML: %s", where, exc)
        return None


def parse_search_results_html(
    html: str,
    limit: int,
    condition_filter: str,
    product_cls: "type[Any]",
) -> List[Any]:
    """Parse Best Buy search-results HTML into ``product_cls`` instances."""
    soup = _build_soup(html, "search")
    if soup is None:
        return []

    open_box_global = (
        "open box" in html.lower() or "open-box" in html.lower()
    )
    products: List[Any] = _try_jsonld_search_results(
        soup, product_cls, limit, open_box_global
    )
    if products:
        return products[:limit]

    for card in soup.find_all("li", class_=_RE_SKU_ITEM):
        built = _build_card_product(card, product_cls, condition_filter)
        if built is None:
            continue
        products.append(built)
        if len(products) >= limit:
            break
    return products[:limit]


# ---------------------------------------------------------------------------
# Product-detail JSON-LD parsing
# ---------------------------------------------------------------------------

def _apply_jsonld_offers(detail: Any, offers: Dict[str, Any]) -> None:
    if not isinstance(offers, dict):
        return
    price = offers.get("price")
    if price:
        detail.price = "$%s" % price
    detail.availability = (offers.get("availability") or "").replace(
        "http://schema.org/", ""
    )


def _apply_jsonld_rating(detail: Any, aggregate_rating: Dict[str, Any]) -> None:
    if not aggregate_rating:
        return
    detail.rating = aggregate_rating.get("ratingValue")
    if "reviewCount" in aggregate_rating:
        detail.rating_breakdown = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}


def _apply_jsonld_image(detail: Any, image: Any) -> None:
    if not image:
        return
    detail.images = list(image) if isinstance(image, list) else [image]


def _apply_jsonld_product(detail: Any, data: Dict[str, Any]) -> bool:
    """Populate ``detail`` from one JSON-LD ``Product`` block."""
    if not (isinstance(data, dict) and data.get("@type") == "Product"):
        return False
    detail.title = data.get("name", "")
    detail.description = data.get("description")
    brand = data.get("brand") or {}
    detail.brand = brand.get("name") if isinstance(brand, dict) else None
    detail.model_number = data.get("mpn")
    _apply_jsonld_offers(detail, data.get("offers") or {})
    _apply_jsonld_rating(detail, data.get("aggregateRating") or {})
    _apply_jsonld_image(detail, data.get("image"))
    return True


def _try_jsonld_product_detail(soup: Any, detail: Any) -> None:
    for block in _iter_jsonld_blocks(soup):
        if _apply_jsonld_product(detail, block):
            break


# ---------------------------------------------------------------------------
# Product-detail HTML fallbacks
# ---------------------------------------------------------------------------

def _fill_title_and_price(soup: Any, detail: Any) -> None:
    if not detail.title:
        title_elem = soup.select_one(
            "h1, .product-title, [data-testid='product-title']"
        )
        if title_elem:
            detail.title = title_elem.get_text(strip=True)
    if not detail.price:
        price_elem = soup.select_one(
            ".pricing-price__range, .sr-only:contains('current price'),"
            " .current-price"
        )
        if price_elem:
            detail.price = parse_price(price_elem.get_text())


def _fill_original_and_description(soup: Any, detail: Any) -> None:
    original_elem = soup.select_one(
        ".pricing-price__range-crossed-out, .was-price, .original-price"
    )
    if original_elem:
        detail.original_price = parse_price(original_elem.get_text())
    desc_elem = soup.select_one(
        ".product-description, .overview-content, "
        "[data-testid='product-description']"
    )
    if desc_elem:
        detail.description = desc_elem.get_text(strip=True)


def _fill_specs(soup: Any, detail: Any) -> None:
    specs: Dict[str, str] = {}
    for row in soup.select(
        ".specification-row, .product-data-value, tr"
    ):
        cells = row.find_all(["th", "td", "dt", "dd"])
        if len(cells) < 2:
            continue
        key = cells[0].get_text(strip=True)
        value = cells[1].get_text(strip=True)
        if key and value and key not in specs:
            specs[key] = value
    detail.specs = specs


def _fill_extra_images(soup: Any, detail: Any) -> None:
    for img in soup.select(
        ".thumbnail-image img, .product-image img, [data-testid='product-image']"
    ):
        src = img.get("src") or img.get("data-src")
        if src and src not in detail.images:
            detail.images.append(src)


def _fill_category(soup: Any, detail: Any) -> None:
    category_elem = soup.select_one(".category, .breadcrumb a")
    if category_elem:
        detail.category = category_elem.get_text(strip=True)


def parse_product_html(
    html: str,
    sku: str,
    detail_cls: "type[Any]",
) -> Optional[Any]:
    """Parse Best Buy product-page HTML into a ``detail_cls`` instance."""
    soup = _build_soup(html, "product")
    if soup is None:
        return None

    detail = detail_cls(title="", sku=sku)
    try:
        _try_jsonld_product_detail(soup, detail)
        _fill_title_and_price(soup, detail)
        _fill_original_and_description(soup, detail)
        _fill_specs(soup, detail)
        _fill_extra_images(soup, detail)
        _fill_category(soup, detail)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("Best Buy product DOM scrape error: %s", exc)
        return None
    return detail if detail.title else None
