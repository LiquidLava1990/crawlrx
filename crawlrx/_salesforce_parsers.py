"""Salesforce Commerce Cloud product parsers (split from salesforce_scraper.py).

Pure functions that turn JSON-API hits or rendered HTML cards into the
common ``{title, price, currency, url, image, in_stock, sku}`` shape.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    safe_extract,
)

logger = logging.getLogger("crawlrx.salesforce.parsers")

HTML_PARSE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    *PARSE_EXCEPTIONS,
    *ELEMENT_PROBE_EXCEPTIONS,
)

_HTML_TITLE_SELECTORS = (
    ".pdp-link a, .product-name a, .product-tile-name, .product-title, h3, h2"
)
_HTML_PRICE_SELECTORS = (
    ".price .value, .price-sales, .product-price, .sales .value, "
    ".price-container, .price"
)

_PRICE_CURRENCY_SYMBOLS: Tuple[Tuple[str, str], ...] = (
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
)

HTML_CARD_SELECTORS: Tuple[str, ...] = (
    ".product-tile",
    ".product",
    "[data-pid]",
    ".product-grid-item",
    ".product-card",
    ".grid-tile",
)


def extract_site_id(url: str) -> str:
    """Try to extract the Demandware site ID from the URL."""
    parsed = urlparse(url)
    match = re.search(r"Sites-([^-]+)-Site", parsed.path)
    return match.group(1) if match else "default"


def _coerce_price(price_raw: Any) -> str:
    """Coerce a numeric or string price into ``"%.2f"`` or stringified."""
    if not price_raw:
        return ""
    try:
        return f"{float(price_raw):.2f}"
    except (TypeError, ValueError):
        return str(price_raw)


def _extract_jsonld_price_data(item: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(price, currency)`` from a JSON product's ``price`` field."""
    price_data = item.get("price", {})
    if isinstance(price_data, dict):
        price_raw = price_data.get(
            "sales", price_data.get("list", price_data.get("value", 0))
        )
    else:
        price_raw = price_data
    currency = item.get("currency", "")
    if isinstance(currency, dict):
        currency = currency.get("value", "")
    return _coerce_price(price_raw), str(currency or "")


def _extract_jsonld_product_url(item: Dict[str, Any], base_url: str) -> str:
    """Return the absolute product URL for a JSON product."""
    for key in ("link", "url"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return urljoin(base_url, value)
    if "id" in item:
        return f"{base_url}/s/{item['id']}"
    return ""


def _extract_jsonld_image(item: Dict[str, Any]) -> str:
    """Return a single image URL from a JSON product's ``image`` field."""
    images = item.get("image", item.get("images", {}))
    if isinstance(images, dict):
        return images.get(
            "url",
            images.get("src", images.get("large", images.get("medium", ""))),
        )
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get("url", first.get("src", ""))
    return ""


def _extract_jsonld_in_stock(item: Dict[str, Any]) -> bool:
    """Map the JSON ``availability`` field to a bool."""
    availability = item.get("availability", {})
    if isinstance(availability, dict):
        return bool(availability.get("available", True))
    if isinstance(availability, str):
        return availability.lower() not in ("out of stock", "unavailable", "oos")
    return True


def parse_salesforce_product(
    item: Dict[str, Any], base_url: str
) -> Optional[Dict[str, Any]]:
    """Normalise a Salesforce JSON product into the common product shape."""
    title = item.get("name", item.get("product_name", ""))
    if not title:
        return None
    price, currency = _extract_jsonld_price_data(item)
    return {
        "title": title,
        "price": price,
        "currency": currency,
        "url": _extract_jsonld_product_url(item, base_url),
        "image": _extract_jsonld_image(item),
        "in_stock": _extract_jsonld_in_stock(item),
        "sku": str(item.get("sku", item.get("id", item.get("product_id", "")))),
    }


def parse_api_products(
    data: object, base: str, container_keys: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """Pull products out of the first matching key in a response dict."""
    if not isinstance(data, dict):
        return []
    products: List[Any] = []
    for key in container_keys:
        candidate = data.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("products", [])
        if isinstance(candidate, list) and candidate:
            products = candidate
            break
    out: List[Dict[str, Any]] = []
    for item in products:
        parsed = parse_salesforce_product(item, base)
        if parsed:
            out.append(parsed)
    return out


def _detect_currency_symbol(text: str) -> str:
    """Return the ISO currency for the first known symbol in ``text``."""
    for symbol, code in _PRICE_CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return ""


def _normalise_html_price(raw: str) -> str:
    """Extract a numeric price token from the rendered price text."""
    match = re.search(r"[\d,]+\.?\d*", raw.replace(",", ""))
    if not match:
        return raw
    try:
        return f"{float(match.group().replace(',', '')):.2f}"
    except ValueError:
        return raw


def _parse_html_product(card: Any, base_url: str) -> Optional[Dict[str, Any]]:
    """Parse a product card from rendered Salesforce HTML."""
    title_el = card.select_one(_HTML_TITLE_SELECTORS)
    if title_el is None:
        return None
    title = title_el.get_text(strip=True)
    if not title:
        return None
    price_el = card.select_one(_HTML_PRICE_SELECTORS)
    raw_price = price_el.get_text(strip=True) if price_el else ""
    price = _normalise_html_price(raw_price)
    currency = _detect_currency_symbol(raw_price)
    link_el = card.select_one("a[href]")
    product_url = urljoin(base_url, link_el["href"]) if link_el else ""
    img_el = card.select_one("img[src]")
    image = ""
    if img_el is not None:
        image = img_el.get("src") or img_el.get("data-src", "")
    sku = card.get("data-pid", card.get("data-product-id", ""))
    in_stock = card.select_one(".out-of-stock, .unavailable, .oos") is None
    return {
        "title": title,
        "price": price,
        "currency": currency,
        "url": product_url,
        "image": image,
        "in_stock": in_stock,
        "sku": str(sku or ""),
    }


def _select_first_cards(soup: BeautifulSoup) -> List[Any]:
    """Try each known card selector, return the first non-empty match."""
    for selector in HTML_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            return cards
    return []


def parse_html_cards(html: str, url: str) -> List[Dict[str, Any]]:
    """Parse all product cards from a rendered storefront page."""
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_first_cards(soup)
    products: List[Dict[str, Any]] = []
    for card in cards:
        parsed = safe_extract(
            lambda card=card: _parse_html_product(card, url),
            field="sf_html_card",
            log=logger,
        )
        if parsed:
            products.append(parsed)
    return products
