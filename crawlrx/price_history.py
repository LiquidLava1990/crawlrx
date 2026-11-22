"""
Price history endpoint for CrawlRX webforge.
Combines CamelCamelCamel and Keepa data for comprehensive price tracking.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from ._scraper_helpers import PARSE_EXCEPTIONS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_HTTP_TIMEOUT = 8

# Compile-once regexes shared by hot helpers below. CCC and Keepa pages get
# parsed for every price-history request, so paying the `re.compile` cost
# per call (or per div in a `find_all` loop) is wasteful.
_CCC_TABLE_CLASS_RE = re.compile(r"price|history", re.I)
_CCC_DIV_DATE_CLASS_RE = re.compile(r"date", re.I)
_CCC_DIV_ROW_CLASS_RE = re.compile(r"price-row|history-item", re.I)
_CCC_DIV_PRICE_CLASS_RE = re.compile(r"price", re.I)
_KEEPA_PRICE_TEXT_RE = re.compile(r"\$[\d,]+\.?\d*")

# Narrow exception family for one scrape attempt. Anything beyond network +
# parse errors is a real bug and propagates so callers can decide.
_SCRAPE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    OSError,
    UnicodeDecodeError,
) + PARSE_EXCEPTIONS


class PriceHistoryRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="Product URL (Amazon or other)")
    asin: Optional[str] = Field(default=None, description="Amazon ASIN")

    @model_validator(mode="after")
    def check_url_or_asin(self) -> "PriceHistoryRequest":
        if not self.url and not self.asin:
            raise ValueError("Either url or asin is required")
        return self


class PriceHistoryEntry(BaseModel):
    date: str
    price: float
    source: str


class PriceHistoryResponse(BaseModel):
    success: bool
    product: Optional[str] = None
    current_price: Optional[float] = None
    history: List[PriceHistoryEntry]
    lowest_ever: Optional[float] = None
    highest_ever: Optional[float] = None
    sources: List[str]
    elapsed_ms: int
    note: Optional[str] = None


def _empty_scrape_result() -> Dict[str, Any]:
    """Default zero-state result shared by both backend scrapers."""
    return {"history": [], "current": None, "product": None}


def _extract_asin_from_url(url: str) -> Optional[str]:
    """Extract ASIN from Amazon URL."""
    if not url:
        return None
    patterns = (
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
    )
    for pattern in patterns:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).upper()
    return None


def _clean_price(price_str: str) -> Optional[float]:
    """Extract numeric price from string."""
    if not price_str:
        return None
    match = re.search(r"[\d,]+\.?\d*", price_str.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


# ─────────────────────────────────────────────
# CamelCamelCamel
# ─────────────────────────────────────────────


def _ccc_current_price(soup: BeautifulSoup) -> Optional[float]:
    """First non-empty current-price (green or red badge) on the page."""
    for cls in ("green", "red"):
        elem = soup.find("span", class_=cls)
        if not elem:
            continue
        price = _clean_price(elem.get_text(strip=True))
        if price is not None:
            return price
    return None


def _ccc_history_from_table(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Pull price-history rows from a ``<table class~="price|history">``."""
    table = soup.find("table", class_=_CCC_TABLE_CLASS_RE)
    if not table:
        return []
    history: List[Dict[str, Any]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date_text = cells[0].get_text(strip=True)
        price = _clean_price(cells[1].get_text(strip=True))
        if not (price and date_text):
            continue
        try:
            iso_date = datetime.strptime(date_text, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            iso_date = datetime.now().strftime("%Y-%m-%d")
        history.append({"date": iso_date, "price": price, "source": "camelcamelcamel"})
    return history


def _resolve_div_date(div: Any) -> str:
    """Pick a date string from a price-row div (today's date if absent)."""
    date_elem = div.find(class_=_CCC_DIV_DATE_CLASS_RE)
    if not date_elem:
        return datetime.now().strftime("%Y-%m-%d")
    raw_date = date_elem.get_text(strip=True)
    try:
        return datetime.strptime(raw_date, "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return raw_date


def _ccc_history_from_divs(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Pull price-history items from generic price-row / history-item divs."""
    history: List[Dict[str, Any]] = []
    for div in soup.find_all("div", class_=_CCC_DIV_ROW_CLASS_RE):
        price_elem = div.find(class_=_CCC_DIV_PRICE_CLASS_RE)
        if not price_elem:
            continue
        price = _clean_price(price_elem.get_text(strip=True))
        if not price:
            continue
        history.append({
            "date": _resolve_div_date(div),
            "price": price,
            "source": "camelcamelcamel",
        })
    return history


def _ccc_alltime_extremes(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """All-time high/low entries from ``<div class="stat">`` blocks."""
    history: List[Dict[str, Any]] = []
    for stat in soup.find_all("div", class_="stat"):
        label = stat.find("div", class_="label")
        value = stat.find("div", class_="value")
        if not (label and value):
            continue
        label_text = label.get_text(strip=True).lower()
        price = _clean_price(value.get_text(strip=True))
        if not price:
            continue
        if "all" in label_text and ("high" in label_text or "low" in label_text):
            history.append({
                "date": "all-time",
                "price": price,
                "source": "camelcamelcamel",
            })
    return history


def _scrape_camelcamelcamel_history_sync(asin: str) -> Dict[str, Any]:
    """Blocking CCC scrape body (runs inside ``asyncio.to_thread``)."""
    asin = asin.strip().upper()
    url = "https://camelcamelcamel.com/product/%s" % asin
    try:
        response = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("CCC request failed for ASIN %s: %s", asin, exc)
        return _empty_scrape_result()
    soup = BeautifulSoup(response.text, "html.parser")
    title_elem = soup.find("h2") or soup.find("h1")
    product_name = title_elem.get_text(strip=True) if title_elem else None
    history: List[Dict[str, Any]] = []
    history.extend(_ccc_history_from_table(soup))
    history.extend(_ccc_history_from_divs(soup))
    history.extend(_ccc_alltime_extremes(soup))
    current_price = _ccc_current_price(soup)
    logger.info(
        "CCC scrape for ASIN %s: %d history entries, current=%s",
        asin, len(history), current_price,
    )
    return {"history": history, "current": current_price, "product": product_name}


async def _scrape_camelcamelcamel_history(asin: str) -> Dict[str, Any]:
    """Scrape price history from CamelCamelCamel (off the event loop)."""
    if not asin or not isinstance(asin, str):
        logger.error("Invalid ASIN provided to CCC: %s", asin)
        return _empty_scrape_result()
    return await asyncio.to_thread(_scrape_camelcamelcamel_history_sync, asin)


# ─────────────────────────────────────────────
# Keepa
# ─────────────────────────────────────────────


def _keepa_history_from_csv(price_history: List[Any]) -> List[Dict[str, Any]]:
    """Parse Keepa's ``[ts, price, ts, price, ...]`` arrays into history rows."""
    history: List[Dict[str, Any]] = []
    for i in range(0, len(price_history) - 1, 2):
        try:
            timestamp = price_history[i]
            price = price_history[i + 1]
        except IndexError:
            continue
        if not (price and price > 0):
            continue
        try:
            date = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            continue
        history.append({
            "date": date,
            "price": round(price / 100.0, 2),
            "source": "keepa",
        })
    return history


def _keepa_current_amazon_price(current_data: List[Any]) -> Optional[float]:
    """Pull the Amazon (price_type==1) current price from the ``current`` array."""
    for i in range(0, len(current_data) - 1, 2):
        try:
            price_type = current_data[i]
            price = current_data[i + 1]
        except IndexError:
            continue
        if price and price > 0 and price_type == 1:
            return price / 100.0
    return None


def _parse_keepa_api_payload(asin: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate a Keepa API payload into the common scrape-result shape."""
    products = data.get("products") or []
    if not products:
        return None
    product = products[0]
    csv_data = product.get("csv") or []
    history: List[Dict[str, Any]] = []
    if csv_data and csv_data[0]:
        history = _keepa_history_from_csv(csv_data[0])
    current_price = _keepa_current_amazon_price(product.get("current") or [])
    logger.info(
        "Keepa API for ASIN %s: found %d history entries, current=%s",
        asin,
        len(history),
        current_price,
    )
    return {
        "history": history,
        "current": current_price,
        "product": product.get("title"),
    }


def _try_keepa_api(asin: str) -> Optional[Dict[str, Any]]:
    """Hit the Keepa API; return the canonical payload on success, ``None`` else."""
    api_url = "https://api.keepa.com/product?key=demo&domain=1&asin=%s" % asin
    try:
        response = requests.get(api_url, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Keepa API failed for ASIN %s: %s", asin, exc)
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except _SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "Keepa API parse error for ASIN %s (%s): %s",
            asin,
            type(exc).__name__,
            exc,
        )
        return None
    return _parse_keepa_api_payload(asin, data or {})


def _try_keepa_page(asin: str) -> Dict[str, Any]:
    """Fallback: scrape current price from the Keepa product page."""
    url = "https://keepa.com/#!product/1-%s" % asin
    try:
        response = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Keepa page scrape failed for ASIN %s: %s", asin, exc)
        return _empty_scrape_result()
    if response.status_code != 200:
        return _empty_scrape_result()
    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except _SCRAPE_EXCEPTIONS as exc:
        logger.warning(
            "Keepa page parse error for ASIN %s (%s): %s",
            asin,
            type(exc).__name__,
            exc,
        )
        return _empty_scrape_result()

    title_elem = soup.find("title")
    product_name = (
        title_elem.get_text(strip=True).replace(" - Keepa", "")
        if title_elem
        else None
    )
    current_price: Optional[float] = None
    for elem in soup.find_all(string=_KEEPA_PRICE_TEXT_RE):
        price = _clean_price(str(elem))
        if price:
            current_price = price
            break
    logger.info("Keepa page scrape for ASIN %s: current=%s", asin, current_price)
    return {"history": [], "current": current_price, "product": product_name}


async def _scrape_keepa_history(asin: str) -> Dict[str, Any]:
    """Scrape price history from Keepa API (with page fallback for current)."""
    if not asin or not isinstance(asin, str):
        logger.error("Invalid ASIN provided to Keepa: %s", asin)
        return _empty_scrape_result()
    asin = asin.strip().upper()
    api_result = _try_keepa_api(asin)
    if api_result is not None:
        return api_result
    return _try_keepa_page(asin)


# ─────────────────────────────────────────────
# /price-history endpoint
# ─────────────────────────────────────────────


def _build_no_asin_response(start_time: float) -> PriceHistoryResponse:
    """Response when the URL parsed but no ASIN could be extracted."""
    return PriceHistoryResponse(
        success=True,
        product=None,
        current_price=None,
        history=[],
        lowest_ever=None,
        highest_ever=None,
        sources=[],
        elapsed_ms=int((time.time() - start_time) * 1000),
        note="Could not extract ASIN from URL",
    )


def _resolve_asin(request: PriceHistoryRequest) -> Optional[str]:
    """Choose the request's ASIN (explicit field, else parse from URL)."""
    if request.asin:
        return request.asin.strip().upper()
    if request.url:
        return _extract_asin_from_url(request.url)
    return None


async def _safe_scrape(
    label: str,
    scraper,
    asin: str,
) -> Dict[str, Any]:
    """Run an async scraper coroutine; return zero-state on known failure."""
    try:
        return await scraper(asin)
    except _SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "%s scrape exception for ASIN %s (%s): %s",
            label,
            asin,
            type(exc).__name__,
            exc,
        )
        return _empty_scrape_result()


def _merge_histories(
    sources: Iterable[tuple[str, Dict[str, Any]]],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Concatenate scraper history payloads into a single list + source set."""
    combined: List[Dict[str, Any]] = []
    used: List[str] = []
    for label, payload in sources:
        if not payload.get("history"):
            continue
        combined.extend(payload["history"])
        if label not in used:
            used.append(label)
    return combined, used


def _sort_history_descending(history: List[Dict[str, Any]]) -> None:
    """In-place: most recent first; ``"all-time"`` sorts to bottom."""
    def _sort_key(entry: Dict[str, Any]) -> str:
        date_str = entry.get("date", "")
        return "0000-00-00" if date_str == "all-time" else date_str
    history.sort(key=_sort_key, reverse=True)


@router.post("/price-history", response_model=PriceHistoryResponse)
async def get_price_history(request: PriceHistoryRequest) -> PriceHistoryResponse:
    """Return combined CamelCamelCamel + Keepa price history for a product."""
    start_time = time.time()
    asin = _resolve_asin(request)
    if asin is None:
        logger.warning("Could not extract ASIN from URL: %s", request.url)
        return _build_no_asin_response(start_time)

    logger.info("Fetching price history for ASIN: %s", asin)
    ccc_data = await _safe_scrape("CCC", _scrape_camelcamelcamel_history, asin)
    keepa_data = await _safe_scrape("Keepa", _scrape_keepa_history, asin)

    combined_history, sources_used = _merge_histories(
        [("camelcamelcamel", ccc_data), ("keepa", keepa_data)]
    )
    _sort_history_descending(combined_history)

    all_prices = [e["price"] for e in combined_history if e.get("price")]
    history_entries = [
        PriceHistoryEntry(date=e["date"], price=e["price"], source=e["source"])
        for e in combined_history
    ]

    current_price = ccc_data.get("current") or keepa_data.get("current")
    product_name = (
        ccc_data.get("product") or keepa_data.get("product") or asin
    )

    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "Price history complete for ASIN %s: %d entries, sources=%s",
        asin,
        len(history_entries),
        sources_used,
    )
    return PriceHistoryResponse(
        success=True,
        product=product_name,
        current_price=current_price,
        history=history_entries,
        lowest_ever=min(all_prices) if all_prices else None,
        highest_ever=max(all_prices) if all_prices else None,
        sources=sources_used,
        elapsed_ms=elapsed_ms,
        note=None if combined_history else "no history found",
    )
