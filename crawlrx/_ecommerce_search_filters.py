"""Pure helpers for the ecommerce-search pipeline.

Price parsing, store-URL resolution, discount enrichment, price filter,
result sort key. No I/O, no sibling-module imports -- safe to use from
both the runner and the source adapters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


def _parse_price(value: Any) -> Optional[float]:
    """Return ``value`` as a float price, ``None`` on parse failure."""
    if value is None:
        return None
    try:
        cleaned = str(value).replace('$', '').replace(',', '').strip()
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _get_store_url(url: str) -> str:
    """Return scheme + netloc for a URL."""
    parsed = urlparse(url)
    return '%s://%s' % (parsed.scheme or 'https', parsed.netloc)


def _coerce_product_url(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Pull ``(title, url)`` from ``item``; ``None`` if either is empty."""
    title = item.get("title", "").strip()
    if not title:
        return None
    url = item.get("url", "").strip()
    if not url:
        return None
    return title, url


def _store_for(url: str, default: str = "") -> str:
    """Return ``store_url`` for ``url`` (scheme+netloc) or ``default``."""
    return _get_store_url(url) if url.startswith("http") else default


def _apply_discount_pct(products: List[Any]) -> None:
    """Compute ``discount_pct`` in place when both prices known."""
    for p in products:
        if p.price_original and p.price and p.price_original > p.price:
            p.discount_pct = round(
                (p.price_original - p.price) / p.price_original * 100, 1,
            )


def _filter_by_price(
    products: List[Any], min_price: Optional[float], max_price: Optional[float],
) -> List[Any]:
    """Apply min/max price filters (price=None excluded when filter set)."""
    if min_price is not None:
        products = [
            p for p in products
            if p.price is not None and p.price >= min_price
        ]
    if max_price is not None:
        products = [
            p for p in products
            if p.price is not None and p.price <= max_price
        ]
    return products


def _ecommerce_sort_key(p: Any) -> tuple:
    """Sort: google_shopping first, then ddgs, then by ascending price."""
    platform_rank = (
        0 if p.platform == "google_shopping"
        else (1 if p.platform == "ddgs_shopping" else 2)
    )
    return (platform_rank, p.price is None, p.price or 0.0)
