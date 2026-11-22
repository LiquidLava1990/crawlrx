"""HTML / JSON-LD parsers for the Walmart scraper.

Split out of ``walmart.py`` for readability: removes the giant
``_parse_search_results_html`` / ``_parse_product_html`` god functions
from the routing module. Imports from ``walmart`` are deferred behind
``TYPE_CHECKING`` to keep this file free of cyclic imports.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._scraper_helpers import PARSE_EXCEPTIONS, parse_price, parse_rating_value

if TYPE_CHECKING:  # pragma: no cover  (typing only)
    from .walmart import WalmartProduct, WalmartProductDetail

    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright
logger = logging.getLogger("crawlrx.walmart._parsers")

WALMART_BASE_URL = "https://www.walmart.com"


# ---------------------------------------------------------------------------
# Item-id helpers (shared between callers)
# ---------------------------------------------------------------------------

_ITEM_ID_PATTERNS = (
    r"/ip/(?:[^/]+/)*(\d+)$",
    r"/ip/(\d+)$",
    r"item/(\d+)",
    r"skuId=(\d+)",
)

# Compile-once: `_parse_search_links_fallback` calls `find_all` per scrape;
# the inline `re.compile(r"/ip/")` re-paid the cost on every call.
_IP_HREF_RE = re.compile(r"/ip/")


def extract_item_id_from_url(url: str) -> Optional[str]:
    """Pull the numeric item id encoded in a Walmart product URL."""
    for pattern in _ITEM_ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def build_product_url(item_id: str) -> str:
    """Canonical ``/ip/{item_id}`` Walmart URL."""
    return f"{WALMART_BASE_URL}/ip/{item_id}"


def absolute_walmart_url(href: str) -> str:
    """Make a Walmart URL absolute, preserving already-absolute hrefs."""
    return href if href.startswith("http") else f"{WALMART_BASE_URL}{href}"


# ---------------------------------------------------------------------------
# Search-results parsing
# ---------------------------------------------------------------------------

def _availability_text(offers: Any) -> str:
    """Strip the ``http://schema.org/`` prefix from an availability tag."""
    if not isinstance(offers, dict):
        return ""
    return (offers.get("availability") or "").replace("http://schema.org/", "")


def _build_product_from_jsonld(
    product_cls: Any,
    product: Dict[str, Any],
) -> Any:
    """Map one ``Product`` block from a JSON-LD ``ItemList`` to ``WalmartProduct``."""
    item_id = product.get("sku", "")
    offers = product.get("offers", {}) or {}
    price = offers.get("price") if isinstance(offers, dict) else None
    aggregate = product.get("aggregateRating", {}) or {}
    return product_cls(
        title=product.get("name", ""),
        price=f"${price}" if price else None,
        original_price=None,
        rating=aggregate.get("ratingValue"),
        review_count=aggregate.get("reviewCount"),
        availability=_availability_text(offers),
        product_url=product.get("url", ""),
        image_url=product.get("image"),
        item_id=str(item_id),
        seller="Walmart",
    )


def _try_parse_jsonld_block(
    product_cls: Any,
    script_text: Optional[str],
    limit: int,
    found: List[Any],
) -> bool:
    """Append products from a single ``<script>`` JSON-LD block.

    Returns True when a complete ``ItemList`` block was processed (the
    caller can stop). Per-block parse failures are downgraded to DEBUG.
    """
    if not script_text:
        return False
    try:
        data = json.loads(script_text)
    except PARSE_EXCEPTIONS as exc:
        logger.debug("walmart jsonld decode failed (%s): %s",
                     type(exc).__name__, exc)
        return False
    if not isinstance(data, dict) or data.get("@type") != "ItemList":
        return False
    for item in data.get("itemListElement", []) or []:
        product = (item or {}).get("item", {}) or {}
        if product.get("@type") != "Product":
            continue
        try:
            found.append(_build_product_from_jsonld(product_cls, product))
        except PARSE_EXCEPTIONS as exc:
            logger.debug("walmart jsonld product map failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        if len(found) >= limit:
            return True
    return bool(found)


def _parse_jsonld_search_results(
    product_cls: Any,
    soup: Any,
    limit: int,
) -> List[Any]:
    """Pull ``WalmartProduct`` items out of all JSON-LD ``<script>`` tags."""
    found: List[Any] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if _try_parse_jsonld_block(product_cls, script.string, limit, found):
            return found[:limit]
    return found[:limit] if found else []


def _price_near_link(link: Any) -> Optional[str]:
    """Heuristic: look at the link's parent + its next sibling for a $price."""
    parent = link.find_parent()
    if parent is None:
        return None
    candidate = parent.find_next_sibling() or parent
    if candidate is None:
        return None
    match = re.search(r"\$[\d,]+\.\d{2}", candidate.get_text())
    return match.group(0) if match else None


def _build_search_link_product(
    product_cls: Any,
    link: Any,
    seen_ids: set[str],
) -> Optional[Any]:
    """Build a ``WalmartProduct`` from a fallback ``<a href="/ip/...">`` link."""
    href = link.get("href", "") or ""
    item_id = extract_item_id_from_url(href)
    if not item_id or item_id in seen_ids:
        return None
    title = link.get_text(strip=True)
    if not title:
        return None
    seen_ids.add(item_id)
    return product_cls(
        title=title,
        price=_price_near_link(link),
        product_url=absolute_walmart_url(href),
        item_id=item_id,
    )


def _parse_search_links_fallback(
    product_cls: Any,
    soup: Any,
    limit: int,
) -> List[Any]:
    """Scan ``<a href="/ip/...">`` links when JSON-LD is missing."""
    found: List[Any] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=_IP_HREF_RE):
        try:
            product = _build_search_link_product(product_cls, link, seen)
        except PARSE_EXCEPTIONS as exc:
            logger.debug("walmart link parse failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        if product is None:
            continue
        found.append(product)
        if len(found) >= limit:
            break
    return found


def parse_search_results_html(
    product_cls: "type[Any]",
    html: str,
    limit: int,
) -> List[Any]:
    """Parse Walmart search HTML into a list of ``WalmartProduct``."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("BeautifulSoup not available for HTML parsing")
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except PARSE_EXCEPTIONS as exc:
        logger.warning("walmart HTML soup construction failed (%s): %s",
                       type(exc).__name__, exc)
        return []
    products = _parse_jsonld_search_results(product_cls, soup, limit)
    if products:
        return products
    return _parse_search_links_fallback(product_cls, soup, limit)


# ---------------------------------------------------------------------------
# Product-detail parsing
# ---------------------------------------------------------------------------

def _ingest_jsonld_offers(detail: Any, offers: Any) -> None:
    """Copy price + availability from a JSON-LD ``offers`` mapping."""
    if not isinstance(offers, dict):
        return
    price = offers.get("price")
    if price:
        detail.price = f"${price}"
    detail.availability = _availability_text(offers)


def _ingest_jsonld_images(detail: Any, image_field: Any) -> None:
    """Copy image array from a JSON-LD product's ``image`` field."""
    if not image_field:
        return
    detail.images = image_field if isinstance(image_field, list) else [image_field]


def _ingest_jsonld_product(detail: Any, data: Dict[str, Any]) -> None:
    """Apply a JSON-LD ``Product`` block onto ``detail``."""
    detail.title = data.get("name", "") or detail.title
    detail.description = data.get("description")
    brand = data.get("brand", {})
    if isinstance(brand, dict):
        detail.brand = brand.get("name")
    _ingest_jsonld_offers(detail, data.get("offers", {}) or {})
    aggregate = data.get("aggregateRating", {}) or {}
    if aggregate:
        detail.rating = aggregate.get("ratingValue")
    _ingest_jsonld_images(detail, data.get("image"))


def _try_apply_jsonld_detail(detail: Any, soup: Any) -> bool:
    """Walk all JSON-LD blocks; apply the first ``Product`` block, return True."""
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string
        if not text:
            continue
        try:
            data = json.loads(text)
        except PARSE_EXCEPTIONS as exc:
            logger.debug("walmart product jsonld decode failed (%s): %s",
                         type(exc).__name__, exc)
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            _ingest_jsonld_product(detail, data)
            return True
    return False


def _apply_html_title_and_price(detail: Any, soup: Any) -> None:
    """Backfill ``title`` / ``price`` / ``original_price`` from HTML selectors."""
    if not detail.title:
        title_elem = soup.select_one(
            "h1[data-automation-id='product-title'], h1"
        )
        if title_elem:
            detail.title = title_elem.get_text(strip=True)
    if not detail.price:
        price_elem = soup.select_one(
            "[data-automation-id='product-price'] span, .price-current"
        )
        if price_elem:
            detail.price = parse_price(price_elem.get_text())
    original_elem = soup.select_one(
        "[data-automation-id='product-price'] .price-old, .price-was"
    )
    if original_elem:
        detail.original_price = parse_price(original_elem.get_text())


def _apply_specs(detail: Any, soup: Any) -> None:
    """Fill ``specs`` from the product specification table rows."""
    specs: Dict[str, str] = {}
    rows = soup.select(
        "[data-automation-id='product-specification-table'] tr, "
        ".specification-table tr"
    )
    for row in rows:
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = cells[0].get_text(strip=True)
        value = cells[1].get_text(strip=True)
        if key and value:
            specs[key] = value
    detail.specs = specs


def _apply_seller(detail: Any, soup: Any) -> None:
    """Resolve the ``seller`` field, defaulting to ``Walmart``."""
    seller_elem = soup.select_one(
        "[data-automation-id='sold-by-section'], .seller-name"
    )
    if not seller_elem:
        detail.seller = "Walmart"
        return
    seller_text = seller_elem.get_text(strip=True)
    detail.seller = "Walmart" if "walmart" in seller_text.lower() else seller_text


def _apply_fulfillment(detail: Any, soup: Any) -> None:
    """Collect available fulfillment-option labels."""
    elems = soup.select(
        "[data-automation-id='fulfillment-badge'], .fulfillment-option"
    )
    detail.fulfillment_options = [el.get_text(strip=True) for el in elems]


def parse_product_html(
    detail_cls: "type[Any]",
    html: str,
    item_id: str,
) -> Optional[Any]:
    """Parse a Walmart product detail page into ``WalmartProductDetail``."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("BeautifulSoup not available for HTML parsing")
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except PARSE_EXCEPTIONS as exc:
        logger.warning("walmart product soup construction failed (%s): %s",
                       type(exc).__name__, exc)
        return None
    detail = detail_cls(title="", item_id=item_id)
    _try_apply_jsonld_detail(detail, soup)
    _apply_html_title_and_price(detail, soup)
    _apply_specs(detail, soup)
    _apply_seller(detail, soup)
    _apply_fulfillment(detail, soup)
    return detail if detail.title else None


# ---------------------------------------------------------------------------
# Playwright probe helpers
# ---------------------------------------------------------------------------

# Element-probe failure tuple kept narrow on purpose: callers want
# extraction misses ignored without swallowing real Playwright errors.
PROBE_EXCEPTIONS = (AttributeError, TypeError, ValueError, KeyError, IndexError)


async def safe_inner_text(element: "ElementHandle | None") -> Optional[str]:
    """``inner_text()`` with a narrow except + ``.strip()``."""
    if element is None:
        return None
    try:
        text = await element.inner_text()
    except PROBE_EXCEPTIONS as exc:
        logger.debug("walmart inner_text probe failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    return text.strip() if isinstance(text, str) else None


async def safe_attr(element: "ElementHandle | None", name: str) -> Optional[str]:
    """``get_attribute(name)`` with a narrow except."""
    if element is None:
        return None
    try:
        return await element.get_attribute(name)
    except PROBE_EXCEPTIONS as exc:
        logger.debug("walmart get_attribute(%s) failed (%s): %s",
                     name, type(exc).__name__, exc)
        return None


async def probe_text(page: "Page", selector: str) -> Optional[str]:
    """Query one selector and return its inner text (None on miss/error)."""
    try:
        el = await page.query_selector(selector)
    except PROBE_EXCEPTIONS as exc:
        logger.debug("walmart probe selector(%s) failed (%s): %s",
                     selector, type(exc).__name__, exc)
        return None
    return await safe_inner_text(el)


async def probe_rating(page: "Page", selector: str) -> Optional[float]:
    """Query one selector and parse a numeric rating (None on miss/error)."""
    text = await probe_text(page, selector)
    return parse_rating_value(text)
