"""Wayback Machine price scraper for CrawlRX (POST /scrape/wayback)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, List, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


class WaybackRequest(BaseModel):
    """Request body for Wayback scraping."""

    url: str
    snapshots: int = Field(default=5, ge=1, le=20)


class SnapshotResult(BaseModel):
    """Result for a single Wayback snapshot."""

    timestamp: str
    archived_url: str
    price_found: Optional[str]


class WaybackResponse(BaseModel):
    """Response body for Wayback scraping."""

    success: bool
    url: str
    snapshots: List[SnapshotResult]
    elapsed_ms: int
    note: Optional[str] = None


# ─────────────────────────────────────────────
# Price extraction
# ─────────────────────────────────────────────


_PRICE_SELECTORS: tuple[str, ...] = (
    ".price",
    '[itemprop="price"]',
    ".product-price",
    ".current-price",
    ".sale-price",
    ".regular-price",
    ".amount",
    ".woocommerce-Price-amount",
    ".product-price-current",
)
_DOLLAR_RE = re.compile(r"\$[\d,]+\.?\d{0,2}")


def _price_from_selector(soup: BeautifulSoup, selector: str) -> Optional[str]:
    """Return the first dollar-shaped match inside ``selector`` (or ``None``)."""
    for element in soup.select(selector):
        text = element.get_text(strip=True)
        if not text:
            continue
        match = _DOLLAR_RE.search(text)
        if match:
            return match.group(0)
    return None


def _is_reasonable_price(value: str) -> bool:
    """Return True if ``value`` parses to a price between 0 and 100k."""
    try:
        numeric = float(value.replace("$", "").replace(",", ""))
    except ValueError:
        return False
    return 0 < numeric < 100000


def _price_from_text(soup: BeautifulSoup) -> Optional[str]:
    """Search the entire page text for a reasonable dollar amount."""
    matches = _DOLLAR_RE.findall(soup.get_text())
    if not matches:
        return None
    for match in matches:
        if _is_reasonable_price(match):
            return match
    return matches[0]


def _extract_price(html: str) -> Optional[str]:
    """Extract a price from HTML using selectors first, then full-text regex."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in _PRICE_SELECTORS:
        found = _price_from_selector(soup, selector)
        if found:
            return found
    return _price_from_text(soup)


# ─────────────────────────────────────────────
# Wayback CDX API
# ─────────────────────────────────────────────


async def _fetch_wayback_snapshots(url: str, limit: int) -> List[dict[str, Any]]:
    """Fetch the snapshot list from the Wayback CDX API."""
    cdx_url = "https://web.archive.org/cdx/search/cdx"
    params = {
        "url": url,
        "output": "json",
        "limit": limit,
        "fl": "timestamp,original",
        "filter": "statuscode:200",
    }
    logger.info("Fetching Wayback snapshots for url=%s with limit=%d", url, limit)
    data = await asyncio.to_thread(_sync_cdx_fetch, cdx_url, params, url)
    return _shape_cdx_rows(data, url)


def _sync_cdx_fetch(
    cdx_url: str, params: dict[str, Any], url: str
) -> List[List[Any]]:
    """Synchronous CDX fetch (off the event loop)."""
    try:
        response = requests.get(cdx_url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        logger.warning("Wayback CDX API timeout for url=%s", url)
        return []
    except requests.exceptions.RequestException as exc:
        logger.warning("Wayback CDX API request failed for url=%s: %s", url, exc)
        return []
    except ValueError as exc:
        logger.warning("Wayback CDX API invalid JSON for url=%s: %s", url, exc)
        return []


def _shape_cdx_rows(data: List[List[Any]], url: str) -> List[dict[str, Any]]:
    """Transform the CDX [headers, *rows] response into list of dicts."""
    if not data or len(data) < 2:
        logger.info("No snapshots found for url=%s", url)
        return []
    headers = data[0]
    results: List[dict[str, Any]] = []
    for row in data[1:]:
        results.append(_zip_row(headers, row))
    logger.info("Found %d snapshots for url=%s", len(results), url)
    return results


def _zip_row(headers: List[Any], row: List[Any]) -> dict[str, Any]:
    """Zip a CDX row with its column headers (truncating to row length)."""
    width = min(len(headers), len(row))
    return {headers[i]: row[i] for i in range(width)}


def _fetch_snapshot_page(timestamp: str, original_url: str) -> Optional[str]:
    """Fetch a single Wayback archived page (sync)."""
    archived_url = f"https://web.archive.org/web/{timestamp}/{original_url}"
    logger.debug("Fetching archived page: %s", archived_url)
    try:
        response = requests.get(archived_url, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.exceptions.Timeout:
        logger.warning("Timeout fetching archived page: %s", archived_url)
        return None
    except requests.exceptions.RequestException as exc:
        logger.warning("Failed to fetch archived page %s: %s", archived_url, exc)
        return None


# ─────────────────────────────────────────────
# Snapshot processing
# ─────────────────────────────────────────────


async def _process_snapshot(
    snapshot: dict[str, Any], default_url: str
) -> Optional[SnapshotResult]:
    """Convert a CDX snapshot record into a :class:`SnapshotResult`."""
    timestamp = snapshot.get("timestamp", "")
    original = snapshot.get("original", default_url)
    if not timestamp:
        logger.warning("Skipping snapshot: missing timestamp")
        return None
    html = await asyncio.to_thread(_fetch_snapshot_page, timestamp, original)
    price = _extract_price(html) if html else None
    if price:
        logger.info("Found price=%s for timestamp=%s", price, timestamp)
    return SnapshotResult(
        timestamp=timestamp,
        archived_url=f"https://web.archive.org/web/{timestamp}/{original}",
        price_found=price,
    )


async def _walk_snapshots(
    snapshots: List[dict[str, Any]], default_url: str
) -> List[SnapshotResult]:
    """Process every snapshot record, sleeping 1s between fetches."""
    results: List[SnapshotResult] = []
    last = len(snapshots) - 1
    for index, snapshot in enumerate(snapshots):
        record = await _process_snapshot(snapshot, default_url)
        if record is not None:
            results.append(record)
        if index < last:
            logger.debug("Rate limiting: sleeping 1 second")
            await asyncio.sleep(1)
    return results


def _empty_response(url: str, start_time: float) -> WaybackResponse:
    """Build the "no snapshots" response."""
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info("No archived snapshots found for url=%s", url)
    return WaybackResponse(
        success=True, url=url, snapshots=[],
        elapsed_ms=elapsed_ms, note="no archived snapshots found",
    )


def _error_response(url: str, start_time: float, exc: BaseException) -> WaybackResponse:
    """Build the unexpected-error response (never raises)."""
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.error(
        "Unexpected error during Wayback scrape for url=%s: %s: %s",
        url, type(exc).__name__, exc,
    )
    return WaybackResponse(
        success=False, url=url, snapshots=[],
        elapsed_ms=elapsed_ms, note=f"error: {exc}",
    )


_SCRAPE_ERRORS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    OSError,
)


@router.post("/scrape/wayback", response_model=WaybackResponse)
async def scrape_wayback(request: WaybackRequest) -> WaybackResponse:
    """Scrape historical prices from Wayback Machine snapshots."""
    start_time = time.time()
    logger.info(
        "Starting Wayback scrape for url=%s, snapshots=%d",
        request.url, request.snapshots,
    )
    try:
        snapshots_data = await _fetch_wayback_snapshots(
            request.url, request.snapshots
        )
        if not snapshots_data:
            return _empty_response(request.url, start_time)
        results = await _walk_snapshots(snapshots_data, request.url)
    except _SCRAPE_ERRORS as exc:
        return _error_response(request.url, start_time, exc)
    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "Completed Wayback scrape for url=%s: %d snapshots, %d with prices in %d ms",
        request.url, len(results),
        sum(1 for r in results if r.price_found), elapsed_ms,
    )
    return WaybackResponse(
        success=True, url=request.url, snapshots=results, elapsed_ms=elapsed_ms,
    )
