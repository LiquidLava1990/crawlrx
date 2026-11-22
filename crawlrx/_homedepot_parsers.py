"""HomeDepot HTML / JSON-LD parsing helpers (split from homedepot.py).

Pulled out so the parent module stays small
and so the search-results parser and product-page parser stop sharing
duplicated body shingles.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    parse_price,
    parse_rating_value,
    parse_review_count,
    safe_extract,
)

logger = logging.getLogger("crawlrx.homedepot.parsers")

HOMEDEPOT_BASE_URL = "https://www.homedepot.com"

# Narrow exception family for HTML parsing:
# JSON-LD decode + soup attribute / lookup failures.
HD_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *PARSE_EXCEPTIONS,
    *ELEMENT_PROBE_EXCEPTIONS,
)

_MODEL_RE = re.compile(r"Model[#\s]+([A-Za-z0-9-]+)")


# ---------------------------------------------------------------------------
# Search-results parsing
# ---------------------------------------------------------------------------

def _make_card_class_matcher(token: str):
    """Return a class= predicate matching any class containing ``token``."""
    needle = token.lower()

    def _matcher(value: Optional[str]) -> bool:
        return bool(value) and needle in value.lower()

    return _matcher


def _jsonld_search_offers_list(data: Any) -> Optional[list]:
    """Return the ``itemListElement`` array from a JSON-LD ``ItemList``."""
    if not isinstance(data, dict) or data.get("@type") != "ItemList":
        return None
    return data.get("itemListElement", []) or []


def _build_search_product_from_jsonld(
    raw: Dict[str, Any],
    product_factory: Any,
) -> Optional[Any]:
    """Convert a JSON-LD ``Product`` block into a ``HomeDepotProduct``."""
    if raw.get("@type") != "Product":
        return None
    offers = raw.get("offers") or {}
    rating = raw.get("aggregateRating") or {}
    price = offers.get("price")
    return product_factory(
        title=raw.get("name", ""),
        price=f"${price}" if price else None,
        rating=rating.get("ratingValue"),
        review_count=rating.get("reviewCount"),
        model_number=raw.get("sku"),
        brand=(raw.get("brand") or {}).get("name"),
        product_url=raw.get("url", ""),
        image_url=raw.get("image"),
        availability=offers.get("availability", "").replace(
            "http://schema.org/", ""
        ),
    )


def _decode_jsonld_script(script: Any) -> Any:
    """Decode one ``<script>`` body, returning ``None`` on parse failure."""
    try:
        return json.loads(script.string or "")
    except HD_PARSE_EXCEPTIONS:
        return None


def _products_from_itemlist_entries(
    entries: list,
    limit: int,
    product_factory: Any,
) -> List[Any]:
    """Convert each ``itemListElement`` entry into a product row."""
    products: List[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item", {})
        product = safe_extract(
            lambda inner=inner: _build_search_product_from_jsonld(
                inner, product_factory
            ),
            field="hd_jsonld_product",
            log=logger,
        )
        if product:
            products.append(product)
        if len(products) >= limit:
            break
    return products


def _parse_jsonld_search(
    soup: BeautifulSoup,
    limit: int,
    product_factory: Any,
) -> List[Any]:
    """Parse the JSON-LD ``ItemList`` into HomeDepotProduct rows."""
    for script in soup.find_all("script", type="application/ld+json"):
        data = _decode_jsonld_script(script)
        elements = _jsonld_search_offers_list(data) if data is not None else None
        if not elements:
            continue
        products = _products_from_itemlist_entries(elements, limit, product_factory)
        if products:
            return products[:limit]
    return []


def _card_title(card: Any) -> Optional[str]:
    """Find a card's product title across HD's selector variants."""
    title_el = (
        card.find("span", class_=_make_card_class_matcher("product-title"))
        or card.find("h3")
        or card.find("a", {"data-testid": "product-title"})
    )
    if title_el:
        text = title_el.get_text(strip=True)
        return text or None
    return None


def _card_product_url(card: Any) -> Optional[str]:
    """Resolve a card's product URL, prefixing the HD base URL when relative."""
    link = card.find("a", href=True)
    if not link:
        return None
    href = link["href"]
    return href if href.startswith("http") else f"{HOMEDEPOT_BASE_URL}{href}"


def _card_model_number(card: Any) -> Optional[str]:
    """Pull the model number out of a card."""
    sku_el = card.find("div", class_=_make_card_class_matcher("model"))
    if not sku_el:
        return None
    match = _MODEL_RE.search(sku_el.get_text())
    return match.group(1) if match else None


def _card_price_and_was_price(
    card: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(price, original_price)`` strings for a card."""
    price_el = card.find("span", class_=_make_card_class_matcher("price"))
    was_el = card.find("span", class_=_make_card_class_matcher("was-price"))
    return (
        parse_price(price_el.get_text()) if price_el else None,
        parse_price(was_el.get_text()) if was_el else None,
    )


def _card_rating_and_reviews(
    card: Any,
) -> tuple[Optional[float], Optional[int]]:
    """Return ``(rating, review_count)`` for a card."""
    rating_el = card.find("span", class_=_make_card_class_matcher("rating"))
    review_el = card.find("span", class_=_make_card_class_matcher("review"))
    return (
        parse_rating_value(rating_el.get_text()) if rating_el else None,
        parse_review_count(review_el.get_text()) if review_el else None,
    )


def _card_image_and_brand(
    card: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(image_url, brand)`` for a card."""
    img_el = card.find("img")
    image_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
    brand_el = card.find("span", class_=_make_card_class_matcher("brand"))
    brand = brand_el.get_text(strip=True) if brand_el else None
    return image_url, brand


def _build_search_product_from_card(
    card: Any,
    product_factory: Any,
) -> Optional[Any]:
    """Construct a ``HomeDepotProduct`` from a search-results card."""
    title = _card_title(card)
    if not title:
        return None
    price, original_price = _card_price_and_was_price(card)
    rating, review_count = _card_rating_and_reviews(card)
    image_url, brand = _card_image_and_brand(card)
    product_url = _card_product_url(card) or ""
    model_number = _card_model_number(card)

    return product_factory(
        title=title,
        price=price,
        original_price=original_price,
        rating=rating,
        review_count=review_count,
        model_number=model_number,
        brand=brand,
        product_url=product_url,
        image_url=image_url,
        availability=None,
    )


def _parse_search_cards(
    soup: BeautifulSoup,
    limit: int,
    product_factory: Any,
) -> List[Any]:
    """Walk product-card divs and convert to ``HomeDepotProduct`` rows."""
    products: List[Any] = []
    cards = soup.find_all(
        "div",
        class_=lambda x: bool(x)
        and ("product" in x.lower() or "browse-search-result" in x.lower()),
    )
    for card in cards:
        product = safe_extract(
            lambda card=card: _build_search_product_from_card(card, product_factory),
            field="hd_search_card",
            log=logger,
        )
        if product:
            products.append(product)
        if len(products) >= limit:
            break
    return products


def parse_search_results_html(
    html: str,
    limit: int,
    product_factory: "Callable[..., Any]",
) -> List[Any]:
    """Parse Home Depot search-results HTML into product rows."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except HD_PARSE_EXCEPTIONS as exc:
        logger.warning("HD search soup error: %s", exc)
        return []

    try:
        jsonld_products = _parse_jsonld_search(soup, limit, product_factory)
    except HD_PARSE_EXCEPTIONS as exc:
        logger.debug("HD JSON-LD search parse failed: %s", exc)
        jsonld_products = []
    if jsonld_products:
        return jsonld_products[:limit]

    return _parse_search_cards(soup, limit, product_factory)[:limit]


# ---------------------------------------------------------------------------
# Product-detail parsing
# ---------------------------------------------------------------------------

def _apply_jsonld_offers(detail: Any, offers: Any) -> None:
    """Copy offer fields from JSON-LD into a ``HomeDepotProductDetail``."""
    if not isinstance(offers, dict):
        return
    price = offers.get("price")
    if price:
        detail.price = f"${price}"
    detail.availability = offers.get("availability", "").replace(
        "http://schema.org/", ""
    )


def _apply_jsonld_rating(detail: Any, rating: Dict[str, Any]) -> None:
    """Copy ``aggregateRating`` into ``rating_breakdown`` if present."""
    if not rating:
        return
    detail.rating_breakdown = {
        "rating": rating.get("ratingValue"),
        "review_count": rating.get("reviewCount"),
    }


def _apply_jsonld_images(detail: Any, image_field: Any) -> None:
    """Normalise the JSON-LD image field into a list."""
    if not image_field:
        return
    detail.images = image_field if isinstance(image_field, list) else [image_field]


def _apply_jsonld_product(detail: Any, data: Dict[str, Any]) -> bool:
    """Copy every JSON-LD ``Product`` field into ``detail``. Returns True if applied."""
    if data.get("@type") != "Product":
        return False
    detail.title = data.get("name", "")
    detail.description = data.get("description")
    detail.brand = (data.get("brand") or {}).get("name")
    detail.sku = data.get("sku")
    _apply_jsonld_offers(detail, data.get("offers"))
    _apply_jsonld_rating(detail, data.get("aggregateRating") or {})
    _apply_jsonld_images(detail, data.get("image"))
    return True


def _hydrate_detail_from_jsonld(soup: BeautifulSoup, detail: Any) -> None:
    """Fill ``detail`` from the first matching JSON-LD ``Product`` block."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except HD_PARSE_EXCEPTIONS:
            continue
        if isinstance(data, dict) and _apply_jsonld_product(detail, data):
            break


def _hydrate_title_and_price(soup: BeautifulSoup, detail: Any) -> None:
    """Populate title / price from rendered HTML if JSON-LD missed them."""
    if not detail.title:
        elem = soup.select_one(
            "h1[data-testid='product-title'], h1.product-title, h1"
        )
        if elem:
            detail.title = elem.get_text(strip=True)
    if not detail.price:
        elem = soup.select_one(
            "span[data-testid='product-price'], .price-current, "
            ".product-prices__price"
        )
        if elem:
            detail.price = parse_price(elem.get_text())


def _extract_specs_from_table(soup: BeautifulSoup) -> Dict[str, str]:
    """Pull specs out of HD's ``<table>``-shaped specs block."""
    specs: Dict[str, str] = {}
    rows = soup.select(
        "table.specifications-table tr, [data-testid='product-specs'] tr, "
        ".product-details__specs tr"
    )
    for row in rows:
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            key = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if key and value:
                specs[key] = value
    return specs


def _extract_specs_from_items(soup: BeautifulSoup) -> Dict[str, str]:
    """Pull specs out of HD's ``<div>``-shaped spec-item layout."""
    specs: Dict[str, str] = {}
    for item in soup.select("[data-testid='spec-item'], .specification-item"):
        label = item.select_one(".spec-label, [data-testid='spec-label']")
        value = item.select_one(".spec-value, [data-testid='spec-value']")
        if label and value:
            specs[label.get_text(strip=True)] = value.get_text(strip=True)
    return specs


def _hydrate_specs(soup: BeautifulSoup, detail: Any) -> None:
    """Populate ``detail.specs`` using the table layout, then fall back to items."""
    specs = _extract_specs_from_table(soup)
    if not specs:
        specs = _extract_specs_from_items(soup)
    detail.specs = specs


def _hydrate_model_and_description(soup: BeautifulSoup, detail: Any) -> None:
    """Populate model number + description from rendered HTML if missing."""
    if not detail.model_number:
        elem = soup.select_one(
            "[data-testid='model-number'], .product-identifiers__model"
        )
        if elem:
            match = _MODEL_RE.search(elem.get_text())
            if match:
                detail.model_number = match.group(1)
    if not detail.description:
        elem = soup.select_one(
            "[data-testid='product-description'], .product-description, "
            "#product_description"
        )
        if elem:
            detail.description = elem.get_text(strip=True)


def _hydrate_related(soup: BeautifulSoup, detail: Any) -> None:
    """Capture up to 5 related-product entries."""
    related: List[Dict[str, str]] = []
    for item in soup.select("[data-testid='related-product'], .related-product")[:5]:
        title = item.select_one(".product-title, [data-testid='product-title']")
        link = item.find("a", href=True)
        if not (title and link):
            continue
        href = link["href"]
        related.append(
            {
                "title": title.get_text(strip=True),
                "url": href if href.startswith("http") else f"{HOMEDEPOT_BASE_URL}{href}",
            }
        )
    detail.related_items = related


def _hydrate_top_reviews(soup: BeautifulSoup, detail: Any) -> None:
    """Capture up to 5 top reviews from the rendered HTML."""
    reviews: List[Dict[str, Any]] = []
    for review in soup.select("[data-testid='review-item'], .review-item")[:5]:
        title_el = review.select_one(".review-title, [data-testid='review-title']")
        body_el = review.select_one(".review-body, [data-testid='review-body']")
        rating_el = review.select_one(".review-rating, [data-testid='review-rating']")
        author_el = review.select_one(".review-author, [data-testid='review-author']")
        if not (title_el and body_el):
            continue
        reviews.append(
            {
                "title": title_el.get_text(strip=True),
                "body": body_el.get_text(strip=True),
                "rating": parse_rating_value(rating_el.get_text()) if rating_el else None,
                "author": author_el.get_text(strip=True) if author_el else None,
            }
        )
    detail.top_reviews = reviews


def parse_product_html(html: str, url: str, detail_factory: "Callable[..., Any]") -> Optional[Any]:
    """Parse a Home Depot product page into a ``HomeDepotProductDetail``."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except HD_PARSE_EXCEPTIONS as exc:
        logger.warning("HD product soup error for %s: %s", url, exc)
        return None

    detail = detail_factory(title="")
    try:
        _hydrate_detail_from_jsonld(soup, detail)
        _hydrate_title_and_price(soup, detail)
        _hydrate_specs(soup, detail)
        _hydrate_model_and_description(soup, detail)
        _hydrate_related(soup, detail)
        _hydrate_top_reviews(soup, detail)
    except HD_PARSE_EXCEPTIONS as exc:
        logger.warning("Error parsing Home Depot product HTML: %s", exc)
        return None

    return detail if detail.title else None
