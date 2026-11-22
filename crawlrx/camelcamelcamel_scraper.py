"""CamelCamelCamel price history scraper for CrawlRX webforge."""

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

TIMEOUT = 15
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

router = APIRouter(prefix="/scrape", tags=["scrapers"])


class CamelCamelRequest(BaseModel):
    asin: str


class CamelCamelSearchRequest(BaseModel):
    query: str


_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Compile-once shared by `_iter_search_asins`; the original site re-compiled
# both patterns on every search call.
_PRODUCT_HREF_RE = re.compile(r"/product/")
_PRODUCT_ASIN_RE = re.compile(r"/product/([A-Z0-9]{10})")


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


def _empty_result(asin: str) -> Dict[str, Any]:
    """Default zero-state CamelCamelCamel result row."""
    return {
        "asin": asin,
        "current": None,
        "all_time_low": None,
        "all_time_high": None,
        "avg_30d": None,
        "avg_90d": None,
        "avg_180d": None,
        "source": "camelcamelcamel",
    }


def _extract_current_price(soup: BeautifulSoup) -> Optional[float]:
    """First non-empty current price (green or red badge) in the page."""
    for cls in ("green", "red"):
        elem = soup.find("span", class_=cls)
        if not elem:
            continue
        price = _clean_price(elem.get_text(strip=True))
        if price is not None:
            return price
    return None


def _extract_high_low_stats(soup: BeautifulSoup) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(all_time_high, all_time_low)`` from ``<div class="stat">`` blocks."""
    high: Optional[float] = None
    low: Optional[float] = None
    for stat in soup.find_all("div", class_="stat"):
        label = stat.find("div", class_="label")
        value = stat.find("div", class_="value")
        if not (label and value):
            continue
        label_text = label.get_text(strip=True).lower()
        price = _clean_price(value.get_text(strip=True))
        if "high" in label_text and "all" in label_text:
            high = price
        elif "low" in label_text and "all" in label_text:
            low = price
    return high, low


def _bucket_for_label(label_text: str) -> Optional[str]:
    """Map an average-row label to its result-key (e.g. ``"avg_30d"``)."""
    if "30" in label_text:
        return "avg_30d"
    if "90" in label_text:
        return "avg_90d"
    if "180" in label_text:
        return "avg_180d"
    return None


def _averages_from_table(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """Pull rolling averages from the ``#section_amazon`` table cells."""
    section = soup.find("div", {"id": "section_amazon"})
    if not section:
        return {}
    averages: Dict[str, Optional[float]] = {}
    for row in section.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        bucket = _bucket_for_label(cells[0].get_text(strip=True).lower())
        if bucket is None:
            continue
        averages[bucket] = _clean_price(cells[1].get_text(strip=True))
    return averages


def _averages_from_cards(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """Fallback rolling-average extraction from ``<div class="average-row">`` cards."""
    averages: Dict[str, Optional[float]] = {}
    for row in soup.find_all("div", class_="average-row"):
        label = row.find("span", class_="average-label")
        value = row.find("span", class_="average-value")
        if not (label and value):
            continue
        bucket = _bucket_for_label(label.get_text(strip=True).lower())
        if bucket is None:
            continue
        averages[bucket] = _clean_price(value.get_text(strip=True))
    return averages


def _populate_result(soup: BeautifulSoup, result: Dict[str, Any]) -> None:
    """Fill ``result`` in-place from a parsed CamelCamelCamel page."""
    result["current"] = _extract_current_price(soup)
    high, low = _extract_high_low_stats(soup)
    result["all_time_high"] = high
    result["all_time_low"] = low
    averages = _averages_from_table(soup)
    if not any(averages.values()):
        averages = _averages_from_cards(soup)
    for bucket, price in averages.items():
        result[bucket] = price


def scrape_camelcamelcamel(asin: str) -> Dict[str, Any]:
    """Scrape price history from CamelCamelCamel by ASIN."""
    if not asin or not isinstance(asin, str):
        logger.error("Invalid ASIN provided: %s", asin)
        return {}
    asin = asin.strip().upper()
    url = "https://camelcamelcamel.com/product/%s" % asin
    try:
        response = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Request failed for ASIN %s: %s", asin, exc)
        return {}
    soup = BeautifulSoup(response.text, "html.parser")
    result = _empty_result(asin)
    _populate_result(soup, result)
    logger.info("CamelCamelCamel ASIN %s: current=%s", asin, result["current"])
    return result


def _resolve_search_name(link: Any) -> str:
    """Best-effort product name from a search-result anchor."""
    name = link.get_text(strip=True)
    if name:
        return name
    parent = link.find_parent("div", class_="product")
    if not parent:
        return ""
    name_elem = parent.find("h2") or parent.find("h3") or parent.find("a")
    if name_elem:
        return name_elem.get_text(strip=True)
    return ""


def _search_response(query: str) -> Optional[BeautifulSoup]:
    """POST the search query and return parsed soup (or ``None`` on failure)."""
    search_headers = {**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        response = requests.post(
            "https://camelcamelcamel.com/search",
            data={"sq": query.strip()},
            headers=search_headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Search request failed for query '%s': %s", query, exc)
        return None
    return BeautifulSoup(response.text, "html.parser")


def _iter_search_asins(soup: BeautifulSoup) -> Iterable[Tuple[str, Any]]:
    """Yield ``(asin, anchor_element)`` for the first 10 distinct product hits."""
    seen: set[str] = set()
    for link in soup.find_all("a", href=_PRODUCT_HREF_RE)[:10]:
        match = _PRODUCT_ASIN_RE.search(link.get("href", ""))
        if not match:
            continue
        asin = match.group(1)
        if asin in seen:
            continue
        seen.add(asin)
        yield asin, link


def search_camelcamelcamel(query: str) -> List[Dict[str, Any]]:
    """Search CamelCamelCamel by product name."""
    if not query or not isinstance(query, str):
        logger.error("Invalid query provided: %s", query)
        return []
    soup = _search_response(query)
    if soup is None:
        return []
    results: List[Dict[str, Any]] = []
    for asin, link in _iter_search_asins(soup):
        price_data = scrape_camelcamelcamel(asin)
        if not price_data:
            continue
        price_data["name"] = _resolve_search_name(link)
        results.append(price_data)
        time.sleep(0.5)
    logger.info("CamelCamelCamel search '%s': %d results", query, len(results))
    return results


@router.post("/camelcamelcamel")
async def api_scrape_camelcamelcamel(request: CamelCamelRequest) -> object:
    """POST /scrape/camelcamelcamel -- price history by ASIN."""
    result = scrape_camelcamelcamel(request.asin)
    if not result:
        return {
            "success": False,
            "asin": request.asin,
            "error": "Product not found or scrape failed",
        }
    return {"success": True, **result}


@router.post("/camelcamelcamel/search")
async def api_search_camelcamelcamel(request: CamelCamelSearchRequest) -> object:
    """POST /scrape/camelcamelcamel/search -- search by product name."""
    results = search_camelcamelcamel(request.query)
    if not results:
        return {
            "success": False,
            "query": request.query,
            "results": [],
            "error": "No products found or search failed",
        }
    return {
        "success": True,
        "query": request.query,
        "results": results,
        "count": len(results),
    }
