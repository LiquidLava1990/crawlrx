"""
Cross-retailer price comparison endpoint for CrawlRX.
Checks Newegg, CDW, B&H Photo, Adorama, and antonline for product prices.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel

from ._scraper_helpers import PARSE_EXCEPTIONS

logger = logging.getLogger(__name__)
router = APIRouter()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 15

# Narrow exception families for outbound retailer requests / parses.
_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    OSError,
    UnicodeDecodeError,
) + PARSE_EXCEPTIONS
_POOL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RuntimeError,
    OSError,
)


class PriceCompareRequest(BaseModel):
    query: str
    asin: Optional[str] = None


class RetailerResult(BaseModel):
    retailer: str
    price: Optional[float] = None
    url: Optional[str] = None
    in_stock: Optional[bool] = None
    notes: str = ""


class LowestPrice(BaseModel):
    retailer: str
    price: float
    url: Optional[str] = None


class PriceCompareResponse(BaseModel):
    success: bool
    query: str
    results: List[RetailerResult]
    lowest: Optional[LowestPrice] = None
    elapsed_ms: int


@dataclass(frozen=True)
class _RetailerSpec:
    """Per-retailer scraper config: search-URL template + CSS selectors."""

    name: str
    base_host: str
    search_url_template: str  # has one ``%s`` for the encoded query
    item_selector: str
    price_selector: str


_RETAILERS: tuple[_RetailerSpec, ...] = (
    _RetailerSpec(
        name="Newegg",
        base_host="https://www.newegg.com",
        search_url_template="https://www.newegg.com/p/pl?d=%s",
        item_selector=".item-cell",
        price_selector=".price-current",
    ),
    _RetailerSpec(
        name="CDW",
        base_host="https://www.cdw.com",
        search_url_template="https://www.cdw.com/search/?key=%s",
        item_selector=".search-result",
        price_selector=".product-price",
    ),
    _RetailerSpec(
        name="B&H Photo",
        base_host="https://www.bhphotovideo.com",
        search_url_template="https://www.bhphotovideo.com/c/search?q=%s",
        item_selector='[data-selenium="miniProductPage"]',
        price_selector='[data-selenium="miniProductPrice"]',
    ),
    _RetailerSpec(
        name="Adorama",
        base_host="https://www.adorama.com",
        search_url_template="https://www.adorama.com/l/?searchinfo=%s",
        item_selector=".item",
        price_selector=".price",
    ),
    _RetailerSpec(
        name="antonline",
        base_host="https://www.antonline.com",
        search_url_template="https://www.antonline.com/search?search=%s",
        item_selector=".product-item",
        price_selector=".price",
    ),
)


def _parse_price(price_text: Optional[str]) -> Optional[float]:
    """Parse price text to float, stripping $ and commas."""
    if not price_text:
        return None
    try:
        cleaned = price_text.replace("$", "").replace(",", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _check_stock_indicators(text: str, html: str) -> Optional[bool]:
    """Check for in-stock signals in text and HTML."""
    text_lower = text.lower()
    html_lower = html.lower()

    out_of_stock_phrases = (
        "out of stock", "sold out", "unavailable", "not available",
        "backorder", "coming soon", "notify me", "discontinued",
        "temporarily unavailable", "no longer available",
    )
    in_stock_phrases = (
        "in stock", "add to cart", "available", "ships today",
        "ships tomorrow", "ready to ship", "instock",
    )

    for phrase in out_of_stock_phrases:
        if phrase in text_lower or phrase in html_lower:
            return False
    for phrase in in_stock_phrases:
        if phrase in text_lower or phrase in html_lower:
            return True
    return None


def _absolute_item_url(href: Optional[str], base_host: str, fallback: str) -> str:
    """Promote a relative ``href`` to an absolute URL using ``base_host``."""
    if not href:
        return fallback
    if href.startswith("http"):
        return href
    return f"{base_host}{href}"


def _build_retailer_result(spec: _RetailerSpec, item, search_url: str) -> RetailerResult:
    """Translate a parsed search-card into a :class:`RetailerResult`."""
    price_elem = item.select_one(spec.price_selector)
    price = _parse_price(price_elem.get_text(strip=True) if price_elem else None)
    link_elem = item.select_one("a[href]")
    item_url = _absolute_item_url(
        link_elem.get("href") if link_elem else None,
        spec.base_host,
        search_url,
    )
    return RetailerResult(
        retailer=spec.name,
        price=price,
        url=item_url,
        in_stock=_check_stock_indicators(item.get_text(), str(item)),
        notes="" if price else "parse failed",
    )


def _fetch_retailer(spec: _RetailerSpec, query: str) -> RetailerResult:
    """Fetch + parse one retailer; never raises (returns ``parse failed``)."""
    search_url = spec.search_url_template % quote(query)
    try:
        resp = requests.get(
            search_url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except _FETCH_EXCEPTIONS as exc:
        logger.error(
            "%s fetch failed (%s): %s", spec.name, type(exc).__name__, exc
        )
        return RetailerResult(retailer=spec.name, notes="parse failed")
    item = soup.select_one(spec.item_selector)
    if not item:
        return RetailerResult(retailer=spec.name, notes="parse failed")
    return _build_retailer_result(spec, item, search_url)


def _fetcher_for(spec: _RetailerSpec) -> Callable[[str], RetailerResult]:
    """Return a query-only callable bound to ``spec`` (used by ThreadPoolExecutor)."""
    def fetch(query: str) -> RetailerResult:
        return _fetch_retailer(spec, query)
    fetch.__name__ = f"_fetch_{spec.name}"
    return fetch


def _find_lowest(results: List[RetailerResult]) -> Optional[LowestPrice]:
    """Find the lowest price among all retailers."""
    valid_results = [r for r in results if r.price is not None]
    if not valid_results:
        return None
    lowest = min(valid_results, key=lambda r: r.price)
    return LowestPrice(
        retailer=lowest.retailer,
        price=lowest.price,
        url=lowest.url,
    )


def _gather_one(future, spec: _RetailerSpec) -> RetailerResult:
    """Resolve one ``Future``; map known failures to a parse-failed row."""
    try:
        return future.result()
    except _FETCH_EXCEPTIONS as exc:
        logger.error(
            "%s raised exception (%s): %s", spec.name, type(exc).__name__, exc
        )
        return RetailerResult(retailer=spec.name, notes="parse failed")


def _gather_results(query: str) -> List[RetailerResult]:
    """Run all retailer fetchers in parallel; never raises."""
    results: List[RetailerResult] = []
    try:
        with ThreadPoolExecutor(max_workers=len(_RETAILERS)) as executor:
            futures = {
                executor.submit(_fetcher_for(spec), query): spec
                for spec in _RETAILERS
            }
            for future in as_completed(futures):
                results.append(_gather_one(future, futures[future]))
    except _POOL_EXCEPTIONS as exc:
        logger.error(
            "ThreadPoolExecutor failed (%s): %s", type(exc).__name__, exc
        )
        return None  # explicit recovery: non-fatal: _POOL_EXCEPTIONS is recorded but does not abort the caller
    return results


@router.post("/search/price-compare", response_model=PriceCompareResponse)
def price_compare(request: PriceCompareRequest) -> PriceCompareResponse:
    """Compare prices across multiple retailers."""
    start_time = time.time()
    query = request.query
    logger.info("Starting price comparison for query: %s", query)
    results = _gather_results(query)
    elapsed_ms = int((time.time() - start_time) * 1000)
    lowest = _find_lowest(results)
    success = any(r.price is not None for r in results)
    logger.info(
        "Price comparison completed in %d ms, success=%s", elapsed_ms, success
    )
    return PriceCompareResponse(
        success=success,
        query=query,
        results=results,
        lowest=lowest,
        elapsed_ms=elapsed_ms,
    )
