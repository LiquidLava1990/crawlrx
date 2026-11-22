"""HTTP-path scraper for Target (split out of target.py)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

import httpx

from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    parse_price as _hp_parse_price,
)
from ._target_models import (
    TargetProduct,
    TargetProductDetail,
    TargetSearchRequest,
    TargetSearchResponse,
)
from .utils import timer_ms

logger = logging.getLogger("crawlrx.target.http")

TARGET_BASE_URL = "https://www.target.com"
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT_S: float = 30.0


def extract_tcin_from_url(url: str) -> Optional[str]:
    """Extract a TCIN from a Target URL."""
    patterns = (r"/A-(\d+)", r"/p/A-(\d+)", r"tcin=(\d+)", r"/(\d{8,})")
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def build_product_url(tcin: str) -> str:
    """Build the Target product URL from a TCIN."""
    return f"{TARGET_BASE_URL}/p/A-{tcin}"


def _http_headers() -> Dict[str, str]:
    """Default request headers for Target HTTP fetches."""
    return {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def fetch_text(url: str, *, params: Optional[dict] = None) -> str:
    """GET ``url`` returning response text or raising :class:`httpx.HTTPError`."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT_S) as client:
        resp = await client.get(
            url, params=params, headers=_http_headers(), timeout=_HTTP_TIMEOUT_S
        )
        resp.raise_for_status()
        return resp.text


def _safe_jsonld_load(payload: Optional[str]) -> Any:
    """Parse a JSON-LD blob; return ``None`` on malformed JSON."""
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _jsonld_rating(data: Dict[str, Any]) -> tuple[Optional[Any], Optional[Any]]:
    """Pull (rating, review_count) from a JSON-LD ``aggregateRating`` block."""
    aggregate = data.get("aggregateRating", {})
    if not isinstance(aggregate, dict) or not aggregate:
        return None, None
    return aggregate.get("ratingValue"), aggregate.get("reviewCount")


def _parse_jsonld_offers(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the ``offers`` block to a dict (first if a list)."""
    offers = data.get("offers", {})
    if isinstance(offers, list) and offers:
        return offers[0] if isinstance(offers[0], dict) else {}
    return offers if isinstance(offers, dict) else {}


def _parse_jsonld_product(data: Dict[str, Any]) -> Optional[TargetProduct]:
    """Parse a Target product from a JSON-LD record."""
    name = data.get("name", "")
    if not name:
        return None
    tcin = data.get("sku", "") or extract_tcin_from_url(data.get("url", "")) or ""
    if not tcin:
        return None
    offers = _parse_jsonld_offers(data)
    price = f"${offers['price']}" if offers.get("price") else None
    rating, review_count = _jsonld_rating(data)
    brand_data = data.get("brand", {})
    brand = brand_data.get("name") if isinstance(brand_data, dict) else None
    availability = (offers.get("availability") or "").replace(
        "http://schema.org/InStock", "In stock"
    )
    return TargetProduct(
        title=name,
        price=price,
        rating=float(rating) if rating else None,
        review_count=int(review_count) if review_count else None,
        product_url=data.get("url") or build_product_url(tcin),
        image_url=data.get("image"),
        tcin=tcin,
        availability=availability,
        brand=brand,
    )


def _jsonld_list_to_products(items: list) -> List[TargetProduct]:
    """Walk a top-level JSON-LD ``list`` into products."""
    out: List[TargetProduct] = []
    for item in items:
        if not isinstance(item, dict) or item.get("@type") != "Product":
            continue
        product = _parse_jsonld_product(item)
        if product:
            out.append(product)
    return out


def _itemlist_to_products(itemlist: Dict[str, Any]) -> List[TargetProduct]:
    """Walk a JSON-LD ItemList record into products."""
    out: List[TargetProduct] = []
    for item in itemlist.get("itemListElement", []) or []:
        product = _itemlist_entry_to_product(item)
        if product is not None:
            out.append(product)
    return out


def _itemlist_entry_to_product(item: Any) -> Optional[TargetProduct]:
    """Convert one ItemList element to a product (skip on shape miss)."""
    if not isinstance(item, dict):
        return None
    product_data = item.get("item", {})
    if not isinstance(product_data, dict) or product_data.get("@type") != "Product":
        return None
    return _parse_jsonld_product(product_data)


def _jsonld_record_to_products(data: Any) -> List[TargetProduct]:
    """Walk a JSON-LD record (list or ItemList) into products."""
    if isinstance(data, list):
        return _jsonld_list_to_products(data)
    if isinstance(data, dict) and data.get("@type") == "ItemList":
        return _itemlist_to_products(data)
    return []


def _iter_jsonld_products(soup: Any) -> List[TargetProduct]:
    """Extract products from a soup's JSON-LD ``<script>`` blocks."""
    products: List[TargetProduct] = []
    for script in soup.find_all("script", type="application/ld+json"):
        data = _safe_jsonld_load(script.string)
        if data is None:
            continue
        products.extend(_jsonld_record_to_products(data))
    return products


def _card_product_url(card: Any) -> Optional[str]:
    """Build the absolute product URL from a card's anchor tag."""
    link_el = card.find("a", href=True)
    if not link_el:
        return None
    href = link_el["href"]
    return href if href.startswith("http") else f"{TARGET_BASE_URL}{href}"


def _card_to_product(card: Any) -> Optional[TargetProduct]:
    """Convert a single product-card BS4 node into a :class:`TargetProduct`."""
    try:
        tcin = card.get("data-tcin") or card.get("data-testid", "").replace(
            "product-card-", ""
        )
        if not tcin:
            return None
        title_el = card.find("[data-test='product-title']") or card.find("a")
        title = title_el.get_text(strip=True) if title_el else "Unknown Product"
        price_el = card.find("[data-test='product-price']")
        price = _hp_parse_price(price_el.get_text()) if price_el else None
        img_el = card.find("img")
        image_url = img_el.get("src") if img_el else None
        product_url = _card_product_url(card)
        return TargetProduct(
            title=title,
            price=price,
            product_url=product_url or build_product_url(tcin),
            image_url=image_url,
            tcin=tcin,
        )
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Card parse skipped: %s: %s", type(exc).__name__, exc)
        return None


def _iter_card_products(soup: Any) -> List[TargetProduct]:
    """Extract products from ``[data-test='product-card']`` blocks."""
    products: List[TargetProduct] = []
    for card in soup.find_all("[data-test='product-card']"):
        product = _card_to_product(card)
        if product is not None:
            products.append(product)
    return products


def parse_search_results_html(
    html: str,
    request: TargetSearchRequest,
    finalise: "Callable[..., Any]",
) -> List[TargetProduct]:
    """Parse product data from a Target search-results HTML page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("BeautifulSoup not available for HTML parsing")
        return []
    soup = BeautifulSoup(html, "html.parser")
    products = _iter_jsonld_products(soup)
    if products:
        return finalise(products, request)
    return finalise(_iter_card_products(soup), request)


def _apply_jsonld_to_product(detail: TargetProductDetail, data: Dict[str, Any]) -> bool:
    """Populate ``detail`` from a JSON-LD ``Product`` record."""
    if not isinstance(data, dict) or data.get("@type") != "Product":
        return False
    detail.title = data.get("name", "")
    detail.description = data.get("description")
    brand_data = data.get("brand", {})
    if isinstance(brand_data, dict):
        detail.brand = brand_data.get("name")
    _apply_offers(detail, data)
    rating, review_count = _jsonld_rating(data)
    if rating is not None:
        detail.rating = rating
    if review_count is not None:
        detail.review_count = review_count
    detail.images = _jsonld_images(data)
    detail.model_number = data.get("mpn")
    gtin = data.get("gtin13") or data.get("gtin")
    if gtin:
        detail.upc = str(gtin)
    return True


def _apply_offers(detail: TargetProductDetail, data: Dict[str, Any]) -> None:
    """Apply price + availability from a JSON-LD ``offers`` field."""
    offers = data.get("offers", {})
    if not isinstance(offers, dict):
        return
    price = offers.get("price")
    if price:
        detail.price = f"${price}"
    detail.availability = (offers.get("availability") or "").replace(
        "http://schema.org/", ""
    )


def _jsonld_images(data: Dict[str, Any]) -> List[str]:
    """Normalise the JSON-LD ``image`` field to a list."""
    image = data.get("image")
    if not image:
        return []
    if isinstance(image, list):
        return [str(i) for i in image]
    return [str(image)]


def _populate_from_jsonld_blocks(detail: TargetProductDetail, soup: Any) -> None:
    """Walk JSON-LD blocks and apply the first ``Product`` record found."""
    for script in soup.find_all("script", type="application/ld+json"):
        data = _safe_jsonld_load(script.string)
        if data is None:
            continue
        if _apply_jsonld_to_product(detail, data):
            return


def _populate_html_fallbacks(detail: TargetProductDetail, soup: Any) -> None:
    """Fill ``title`` / ``price`` / ``original_price`` when JSON-LD missed them."""
    if not detail.title:
        title_elem = soup.select_one("[data-test='product-title'], h1")
        if title_elem:
            detail.title = title_elem.get_text(strip=True)
    if not detail.price:
        price_elem = soup.select_one("[data-test='product-price']")
        if price_elem:
            detail.price = _hp_parse_price(price_elem.get_text())
    original_price_elem = soup.select_one("[data-test='product-regular-price']")
    if original_price_elem:
        detail.original_price = _hp_parse_price(original_price_elem.get_text())


def _populate_specs(detail: TargetProductDetail, soup: Any) -> None:
    """Read the spec table into ``detail.specs``."""
    specs: Dict[str, str] = {}
    for row in soup.select("[data-test='item-details-specification'] tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = cells[0].get_text(strip=True)
        value = cells[1].get_text(strip=True)
        if key and value:
            specs[key] = value
    detail.specs = specs


def _populate_breadcrumbs(detail: TargetProductDetail, soup: Any) -> None:
    """Stitch breadcrumbs into ``detail.category``."""
    breadcrumbs = soup.select("[data-test='breadcrumb-item']")
    if breadcrumbs:
        detail.category = " > ".join(bc.get_text(strip=True) for bc in breadcrumbs)


def parse_product_html(html: str, tcin: str) -> Optional[TargetProductDetail]:
    """Parse Target product-page HTML into a :class:`TargetProductDetail`."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("BeautifulSoup not available for HTML parsing")
        return None
    soup = BeautifulSoup(html, "html.parser")
    detail = TargetProductDetail(title="", tcin=tcin)
    _populate_from_jsonld_blocks(detail, soup)
    _populate_html_fallbacks(detail, soup)
    _populate_specs(detail, soup)
    _populate_breadcrumbs(detail, soup)
    return detail if detail.title else None


def _http_error_response(
    request: TargetSearchRequest, exc: httpx.HTTPError, start: float
) -> TargetSearchResponse:
    """Build the error response for an httpx-level failure."""
    elapsed = int(timer_ms() - start)
    logger.error("Target HTTP search error for '%s': %s", request.query, exc)
    return TargetSearchResponse(
        success=False, error=f"HTTP error: {exc}", elapsed_ms=elapsed
    )


def _parse_error_response(
    request: TargetSearchRequest, exc: BaseException, start: float
) -> TargetSearchResponse:
    """Build the error response for a parse-level failure."""
    elapsed = int(timer_ms() - start)
    logger.error(
        "Target parse error for '%s': %s: %s",
        request.query, type(exc).__name__, exc,
    )
    return TargetSearchResponse(success=False, error=str(exc), elapsed_ms=elapsed)


async def search_target_http(
    request: TargetSearchRequest, finalise: "Callable[..., Any]"
) -> TargetSearchResponse:
    """Search Target using direct HTTP."""
    start = timer_ms()
    try:
        html = await fetch_text(
            f"{TARGET_BASE_URL}/s", params={"searchTerm": request.query}
        )
    except httpx.HTTPError as exc:
        return _http_error_response(request, exc, start)
    try:
        products = parse_search_results_html(html, request, finalise)
    except PARSE_EXCEPTIONS as exc:
        return _parse_error_response(request, exc, start)
    elapsed = int(timer_ms() - start)
    logger.info(
        "Target HTTP search returned %d products for '%s' in %dms",
        len(products), request.query, elapsed,
    )
    return TargetSearchResponse(
        success=True, products=products, count=len(products), elapsed_ms=elapsed
    )


async def get_product_http(tcin: str) -> Optional[TargetProductDetail]:
    """Get product details using direct HTTP."""
    url = build_product_url(tcin)
    try:
        html = await fetch_text(url)
    except httpx.HTTPError as exc:
        logger.warning("Target HTTP product fetch failed for %s: %s", tcin, exc)
        return None
    try:
        return parse_product_html(html, tcin)
    except PARSE_EXCEPTIONS as exc:
        logger.warning("Target HTTP product parse failed for %s: %s", tcin, exc)
        return None


__all__ = (
    "TARGET_BASE_URL",
    "build_product_url",
    "extract_tcin_from_url",
    "fetch_text",
    "get_product_http",
    "parse_product_html",
    "parse_search_results_html",
    "search_target_http",
)
