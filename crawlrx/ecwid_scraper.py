"""Ecwid scraper for CrawlRX -- detect and scrape Ecwid store products."""

import logging
import re
from typing import Any, Dict, List, Optional

import requests

from ._cms_helpers import (
    CMS_HEADERS,
    HTTP_SCRAPE_EXCEPTIONS,
    REQUESTS_EXCEPTIONS,
    text_contains_any,
)

logger = logging.getLogger(__name__)

TIMEOUT = 8
CDP_PORT = 9231
PLAYWRIGHT_TIMEOUT = 15000

_ECWID_HEADERS: Dict[str, str] = {"User-Agent": CMS_HEADERS["User-Agent"]}
_ECWID_API_HEADERS: Dict[str, str] = {**_ECWID_HEADERS, "Accept": "application/json"}

_DETECTION_MARKERS: tuple[str, ...] = (
    "app.ecwid.com", "Ecwid.init", "ecwid-script", "data-ecwid",
)
_STORE_ID_PATTERNS: tuple[str, ...] = (
    r'Ecwid\.init\(\{[^}]*storeId[:\s]*(\d+)',
    r'data-ecwid[^>]*store-id=["\'](\d+)["\']',
)


def detect_ecwid(url: str, timeout: int = TIMEOUT) -> bool:
    """Detect Ecwid-hosted store via page-source markers."""
    if not url:
        return False
    try:
        resp = requests.get(url, headers=_ECWID_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning("Ecwid detection failed for %s: %s: %s", url, type(exc).__name__, exc)
        return False
    if text_contains_any(resp.text, _DETECTION_MARKERS):
        return True
    return False


def _normalize_price(price_val: Any) -> str:
    """Normalize price to string format."""
    if price_val is None:
        return ""
    if isinstance(price_val, (int, float)):
        return "$%.2f" % price_val
    price_str = str(price_val).strip()
    digits_only = price_str.replace(".", "").replace(",", "")
    if not price_str.startswith("$") and digits_only.isdigit():
        return f"${price_str}"
    return price_str


def _extract_store_id(html: str) -> Optional[str]:
    """Find first matching Ecwid store ID pattern in HTML."""
    for pattern in _STORE_ID_PATTERNS:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _fetch_store_id(base_url: str) -> Optional[str]:
    """GET base_url + parse Ecwid store ID, or None on any failure."""
    try:
        resp = requests.get(base_url, headers=_ECWID_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except REQUESTS_EXCEPTIONS as exc:
        logger.warning(
            "Failed to extract Ecwid store ID from %s: %s: %s",
            base_url, type(exc).__name__, exc,
        )
        return None
    store_id = _extract_store_id(resp.text)
    if store_id:
        logger.info("Ecwid store ID found: %s", store_id)
    return store_id


def _first_image_url(item: dict) -> str:
    """Pull a single image URL out of an Ecwid item.media block."""
    media = item.get("media")
    if not isinstance(media, dict):
        return ""
    images = media.get("images")
    if not (isinstance(images, list) and images):
        return ""
    first = images[0]
    if not isinstance(first, dict):
        return ""
    return first.get("imageUrl", first.get("url", ""))


def _build_product(item: dict, base_url: str) -> Dict[str, Any]:
    """Map one Ecwid item dict to standard product dict."""
    product_url = ""
    if item.get("seoLink"):
        product_url = f"{base_url}#!/~/product/id={item.get('id', '')}"
    return {
        "title": item.get("name", ""),
        "price": _normalize_price(item.get("defaultDisplayedPrice")),
        "url": product_url,
        "image": _first_image_url(item),
        "in_stock": item.get("inStock", True),
        "sku": item.get("sku", ""),
    }


def _fetch_ecwid_items(store_id: str, query: Optional[str], limit: int) -> List[dict]:
    """Hit Ecwid public API; return raw items (or [] on any failure)."""
    api_url = f"https://app.ecwid.com/api/v3/{store_id}/products"
    params: Dict[str, Any] = {"limit": limit, "token": "public"}
    if query:
        params["keyword"] = query
    try:
        resp = requests.get(api_url, headers=_ECWID_API_HEADERS, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except HTTP_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Ecwid API scrape failed for store %s: %s: %s", store_id, type(exc).__name__, exc)
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items", [])
    return [item for item in items if isinstance(item, dict)][:limit]


def scrape_ecwid_products(
    url: str,
    query: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Scrape Ecwid store products via public API."""
    if not url:
        return []
    base_url = url.rstrip("/")
    store_id = _fetch_store_id(base_url)
    if not store_id:
        logger.warning("Could not find Ecwid store ID for %s", url)
        return []
    items = _fetch_ecwid_items(store_id, query, limit)
    results = [_build_product(item, base_url) for item in items]
    logger.info("Ecwid API scrape: %d products from store %s", len(results), store_id)
    return results
