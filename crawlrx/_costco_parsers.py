"""HTML parsing helpers for the Costco scraper.

Split out of ``costco.py`` for readability: keeps the parser god
functions out of the routing module and lets the Tier-0 linter see one
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

logger = logging.getLogger("crawlrx.costco._parsers")

COSTCO_BASE_URL = "https://www.costco.com"

# Module-level compiled regex constants (hoisted from loop bodies for perf).
_RE_PRODUCT_TILE = re.compile(r"product-tile|item|product")


# ---------------------------------------------------------------------------
# URL / text micro-parsers
# ---------------------------------------------------------------------------

def extract_item_number_from_url(url: str) -> Optional[str]:
    """Return the numeric item id encoded in a Costco product URL."""
    patterns = (
        r"/(\d+)\.html",
        r"product[_/]?(\d+)",
        r"item[_-]?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_unit_price(text: Optional[str]) -> Optional[str]:
    """Return ``"$0.50/oz"``-style unit price substring or ``None``."""
    if not text:
        return None
    match = re.search(r"\$[\d.]+\s*/\s*\w+", text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# JSON-LD search-result parsing
# ---------------------------------------------------------------------------

def _build_jsonld_product(
    product_cls: Any,
    product: Dict[str, Any],
) -> Any:
    """Map a JSON-LD ``Product`` block to a :class:`CostcoProduct`.

    ``product_cls`` is injected to avoid an import cycle with ``costco.py``.
    """
    item_number = product.get("sku", "")
    offers = product.get("offers") or {}
    price = offers.get("price") if isinstance(offers, dict) else None
    aggregate_rating = product.get("aggregateRating") or {}
    availability = ""
    if isinstance(offers, dict):
        availability = (offers.get("availability") or "").replace(
            "http://schema.org/", ""
        )
    return product_cls(
        title=product.get("name", ""),
        price="$%s" % price if price else None,
        item_number=str(item_number),
        rating=aggregate_rating.get("ratingValue"),
        review_count=aggregate_rating.get("reviewCount"),
        product_url=product.get("url", ""),
        image_url=product.get("image"),
        availability=availability,
    )


def _iter_jsonld_blocks(soup: Any) -> List[Dict[str, Any]]:
    """Yield decoded JSON-LD payloads, skipping ones that fail to parse."""
    blocks: List[Dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except PARSE_EXCEPTIONS as exc:
            logger.debug("costco jsonld script parse failed: %s", exc)
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


def _drain_item_list(
    block: Dict[str, Any],
    product_cls: Any,
    limit: int,
) -> List[Any]:
    """Map one JSON-LD ``ItemList`` to a list of ``product_cls`` objects."""
    products: List[Any] = []
    for item in block.get("itemListElement", []) or []:
        product = item.get("item") if isinstance(item, dict) else None
        if not (isinstance(product, dict) and product.get("@type") == "Product"):
            continue
        products.append(_build_jsonld_product(product_cls, product))
        if len(products) >= limit:
            break
    return products


def _try_jsonld_search_results(
    soup: Any,
    product_cls: Any,
    limit: int,
) -> List[Any]:
    """Best-effort JSON-LD pass; returns ``[]`` if nothing matches."""
    for block in _iter_jsonld_blocks(soup):
        if block.get("@type") != "ItemList":
            continue
        products = _drain_item_list(block, product_cls, limit)
        if products:
            return products[:limit]
    return []


# ---------------------------------------------------------------------------
# DOM-card search-result parsing
# ---------------------------------------------------------------------------

def _resolve_item_number(card: Any) -> Optional[str]:
    """Pull a Costco item id out of a search-card element."""
    item_number = card.get("data-item-number") or card.get("data-itemid")
    if item_number:
        return item_number
    inner = card.select_one("[data-item-number]")
    if inner is not None:
        item_number = inner.get("data-item-number")
        if item_number:
            return item_number
    link_el = card.select_one("a[href*='.html']")
    if link_el is None:
        return None
    href = link_el.get("href", "")
    match = re.search(r"/(\d+)\.html", href)
    return match.group(1) if match else None


def _resolve_card_url(card: Any) -> str:
    """Return absolute product URL pulled from a search card."""
    link_el = card.select_one("a")
    if link_el is None:
        return ""
    href = link_el.get("href", "") or ""
    if not href:
        return ""
    return href if href.startswith("http") else COSTCO_BASE_URL + href


def _resolve_card_price(card: Any) -> Optional[str]:
    price_el = card.select_one(
        ".price, .sr-only:contains('current price'), .cost, .value"
    )
    if price_el is None:
        return None
    return parse_price(price_el.get_text())


def _resolve_card_unit_price(card: Any) -> Optional[str]:
    unit_el = card.select_one(".unit-price, .comparison-price, .per-unit")
    if unit_el is None:
        return None
    return parse_unit_price(unit_el.get_text())


def _resolve_card_rating(card: Any) -> tuple[Optional[float], Optional[int]]:
    rating_el = card.select_one(
        "[aria-label*='star'], .rating, [class*='rating']"
    )
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


def _resolve_card_flags(card: Any) -> tuple[bool, Optional[str]]:
    text = card.get_text().lower()
    warehouse_only = "warehouse" in text and "only" in text
    availability: Optional[str] = None
    if "out of stock" in text:
        availability = "Out of stock"
    elif "in stock" in text:
        availability = "In stock"
    return warehouse_only, availability


def _build_card_product(
    card: Any,
    product_cls: Any,
) -> Optional[Any]:
    """Build a :class:`CostcoProduct` from one search card or ``None``."""
    try:
        item_number = _resolve_item_number(card)
        if not item_number:
            return None
        title_el = card.select_one(
            ".description, .product-title, "
            "a[data-testid='product-title'], h3 a"
        )
        title = title_el.get_text(strip=True) if title_el else ""
        product_url = _resolve_card_url(card)
        rating, review_count = _resolve_card_rating(card)
        warehouse_only, availability = _resolve_card_flags(card)
        return product_cls(
            title=title,
            price=_resolve_card_price(card),
            item_number=item_number,
            rating=rating,
            review_count=review_count,
            product_url=product_url,
            image_url=_resolve_card_image(card),
            availability=availability,
            unit_price=_resolve_card_unit_price(card),
            warehouse_only=warehouse_only,
        )
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error parsing Costco product card: %s", exc)
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
        logger.warning("Error parsing Costco %s HTML: %s", where, exc)
        return None


def parse_search_results_html(
    html: str,
    limit: int,
    product_cls: "type[Any]",
) -> List[Any]:
    """Parse Costco search-results HTML into a list of ``product_cls``."""
    soup = _build_soup(html, "search")
    if soup is None:
        return []

    products: List[Any] = _try_jsonld_search_results(soup, product_cls, limit)
    if products:
        return products[:limit]

    for card in soup.find_all(["li", "div"], class_=_RE_PRODUCT_TILE):
        built = _build_card_product(card, product_cls)
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
    detail.online_availability = (offers.get("availability") or "").replace(
        "http://schema.org/", ""
    )


def _apply_jsonld_rating(detail: Any, aggregate_rating: Dict[str, Any]) -> None:
    if not aggregate_rating:
        return
    detail.rating = aggregate_rating.get("ratingValue")
    detail.rating_breakdown = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}


def _apply_jsonld_image(detail: Any, image: Any) -> None:
    if not image:
        return
    detail.images = list(image) if isinstance(image, list) else [image]


def _apply_jsonld_product(detail: Any, data: Dict[str, Any]) -> bool:
    """Populate ``detail`` from a single JSON-LD ``Product`` block."""
    if not (isinstance(data, dict) and data.get("@type") == "Product"):
        return False
    detail.title = data.get("name", "")
    detail.description = data.get("description")
    brand = data.get("brand") or {}
    detail.brand = brand.get("name") if isinstance(brand, dict) else None
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
            "h1, .product-title, [data-testid='product-title'], "
            "#product-details-item"
        )
        if title_elem:
            detail.title = title_elem.get_text(strip=True)
    if not detail.price:
        price_elem = soup.select_one(
            ".price, .sr-only:contains('current price'), .value, "
            "[class*='price']"
        )
        if price_elem:
            detail.price = parse_price(price_elem.get_text())


def _fill_unit_and_description(soup: Any, detail: Any) -> None:
    unit_price_elem = soup.select_one(
        ".unit-price, .comparison-price, .per-unit"
    )
    if unit_price_elem:
        detail.unit_price = parse_unit_price(unit_price_elem.get_text())
    desc_elem = soup.select_one(
        ".product-description, .description, .product-info, "
        "[data-testid='product-description']"
    )
    if desc_elem:
        detail.description = desc_elem.get_text(strip=True)


def _fill_features(soup: Any, detail: Any) -> None:
    for feat in soup.select(".product-features li, .product-info li, ul li"):
        text = feat.get_text(strip=True)
        if text and len(text) > 10:
            detail.features.append(text)


def _fill_specs(soup: Any, detail: Any) -> None:
    specs: Dict[str, str] = {}
    for row in soup.select(".specifications tr, .product-data tr, .spec-row"):
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
        ".product-image img, .thumbnail-image img, .product-gallery img"
    ):
        src = img.get("src") or img.get("data-src") or img.get("data-zoom-image")
        if src and src not in detail.images:
            detail.images.append(src)


def _fill_category_and_warehouse(soup: Any, detail: Any) -> None:
    category_elem = soup.select_one(
        ".breadcrumb a, [data-testid='breadcrumb'] a"
    )
    if category_elem:
        detail.category = category_elem.get_text(strip=True)
    warehouse_section = soup.select_one(
        ".warehouse-availability, .store-availability"
    )
    if warehouse_section:
        detail.warehouse_availability.append(
            {"status": warehouse_section.get_text(strip=True)}
        )


def parse_product_html(
    html: str,
    url: str,
    detail_cls: "type[Any]",
) -> Optional[Any]:
    """Parse Costco product-page HTML into a ``detail_cls`` instance."""
    soup = _build_soup(html, "product")
    if soup is None:
        return None

    item_number = extract_item_number_from_url(url) or ""
    detail = detail_cls(title="", item_number=item_number)
    try:
        _try_jsonld_product_detail(soup, detail)
        _fill_title_and_price(soup, detail)
        _fill_unit_and_description(soup, detail)
        _fill_features(soup, detail)
        _fill_specs(soup, detail)
        _fill_extra_images(soup, detail)
        _fill_category_and_warehouse(soup, detail)
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.warning("Costco product DOM scrape error: %s", exc)
        return None
    return detail if detail.title else None
