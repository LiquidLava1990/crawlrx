"""Travel deal scrapers for CrawlRX.

Public surface:
- ``search_flight_deals`` -- flights via Google Flights / Kayak / Playwright.
- ``search_hotel_deals``  -- hotels via Kayak / Hotels.com / Playwright.
- ``search_promo_codes``  -- promos via RetailMeNot / Coupons.com.
- ``get_travel_context``  -- quick-search wrapper.

The heavy lifting lives in sibling modules (``_travel_helpers``,
``_travel_flights``, ``_travel_hotels``, ``_travel_promos``); this file
is thin orchestration. A refactor re-shaped each public function to
delegate to typed-source collectors so the cognitive / nesting / cyclomatic
ceilings stay clear.

Never raises. Returns ``[]`` (or a structured failure dict for
``get_travel_context``) on failure.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

from . import _travel_flights as _flights
from . import _travel_hotels as _hotels
from . import _travel_promos as _promos
from ._scraper_helpers import URLLIB_JSON_EXCEPTIONS, log_and_swallow
from ._travel_helpers import FlightQuery, HotelQuery, PLAYWRIGHT_PORT  # noqa: F401

logger = logging.getLogger("crawlrx.travel")

_RESULT_CAP = 10
# keep direct until service_registry lookup lands
_QUICK_SEARCH_URL = "http://localhost:7337/search/quick"
_QUICK_SEARCH_TIMEOUT_S = 8


def _normalise_route(origin: str, destination: str) -> tuple[str, str]:
    """Return ``(origin_clean, destination_clean)`` upper-cased + stripped."""
    return (origin or "").strip().upper(), (destination or "").strip().upper()


def _flight_query(
    origin: str, destination: str, depart_date: str, return_date: str,
) -> Optional[FlightQuery]:
    """Build a normalised :class:`FlightQuery` or ``None`` if route is blank."""
    origin_clean, dest_clean = _normalise_route(origin, destination)
    if not origin_clean or not dest_clean:
        logger.debug("travel: missing origin or destination")
        return None
    return FlightQuery(
        origin=origin_clean,
        destination=dest_clean,
        depart_date=depart_date,
        return_date=return_date,
    )


def _flights_sync_pipeline(query: FlightQuery) -> List[Dict]:
    """Google Flights + Kayak phase shared by sync + async entry points."""
    results: List[Dict] = list(
        _flights.collect_google_flights(query, cap=_RESULT_CAP)
    )
    if len(results) < 5:
        remaining = _RESULT_CAP - len(results)
        results.extend(_flights.collect_kayak(query, cap=remaining))
    return results


def search_flight_deals(
    origin: str,
    destination: str,
    depart_date: str = "",
    return_date: str = "",
    adults: int = 1,
) -> List[Dict]:
    """Search flight deals across Google Flights + Kayak.

    Returns up to :data:`_RESULT_CAP` records. Never raises -- per-source
    failures are swallowed via the source-specific exception tuples in the
    sibling collectors.

    The ``adults`` arg is part of the public contract but the underlying
    sources currently render the same SERP regardless of pax count; it's
    kept for API stability. Async callers wanting the Playwright fallback
    should use :func:`search_flight_deals_async`.
    """
    del adults  # reserved for future per-source pax wiring
    query = _flight_query(origin, destination, depart_date, return_date)
    if query is None:
        return []
    results = _flights_sync_pipeline(query)
    logger.info(
        "search_flight_deals returned %d results for %s->%s",
        len(results),
        query.origin,
        query.destination,
    )
    return results[:_RESULT_CAP]


async def search_flight_deals_async(
    origin: str,
    destination: str,
    depart_date: str = "",
    return_date: str = "",
    adults: int = 1,
) -> List[Dict]:
    """Async variant that includes the Playwright fallback when sync sources
    return fewer than three records."""
    del adults
    query = _flight_query(origin, destination, depart_date, return_date)
    if query is None:
        return []
    results = _flights_sync_pipeline(query)
    if len(results) < 3:
        remaining = _RESULT_CAP - len(results)
        results.extend(
            await _flights.collect_playwright_google_flights(query, cap=remaining)
        )
    return results[:_RESULT_CAP]


def _hotel_query(
    location: str, checkin: str, checkout: str, guests: int,
) -> Optional[HotelQuery]:
    """Build a normalised :class:`HotelQuery` or ``None`` if blank."""
    location_clean = (location or "").strip()
    if not location_clean:
        logger.debug("travel: missing location")
        return None
    return HotelQuery(
        location=location_clean,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
    )


def _hotels_sync_pipeline(query: HotelQuery) -> List[Dict]:
    """Kayak + Hotels.com phase shared by sync + async entry points."""
    results: List[Dict] = list(
        _hotels.collect_kayak_hotels(query, cap=_RESULT_CAP)
    )
    if len(results) < 5:
        remaining = _RESULT_CAP - len(results)
        results.extend(
            _hotels.collect_hotels_com(location=query.location, cap=remaining)
        )
    return results


def search_hotel_deals(
    location: str,
    checkin: str = "",
    checkout: str = "",
    guests: int = 2,
) -> List[Dict]:
    """Search hotel deals via Kayak + Hotels.com (sync).

    The async fallback to Playwright lives in
    :func:`search_hotel_deals_async`. Returns ``[]`` when ``location`` is
    blank.
    """
    query = _hotel_query(location, checkin, checkout, guests)
    if query is None:
        return []
    results = _hotels_sync_pipeline(query)
    logger.info(
        "search_hotel_deals returned %d results for %s",
        len(results),
        query.location,
    )
    return results[:_RESULT_CAP]


async def search_hotel_deals_async(
    location: str,
    checkin: str = "",
    checkout: str = "",
    guests: int = 2,
) -> List[Dict]:
    """Async hotel search that adds a Playwright fallback when sync sources
    return fewer than three records."""
    query = _hotel_query(location, checkin, checkout, guests)
    if query is None:
        return []
    results = _hotels_sync_pipeline(query)
    if len(results) < 3:
        remaining = _RESULT_CAP - len(results)
        results.extend(
            await _hotels.collect_playwright_kayak_hotels(
                location=query.location, cap=remaining,
            )
        )
    return results[:_RESULT_CAP]


def search_promo_codes(merchant: str) -> List[Dict]:
    """Scrape promo codes for a merchant from RetailMeNot + Coupons.com.

    Returns up to :data:`_RESULT_CAP` codes. Never raises.
    """
    merchant_clean = (merchant or "").strip().lower().replace(" ", "")
    if not merchant_clean:
        logger.debug("search_promo_codes: missing merchant")
        return []

    results: List[Dict] = list(
        _promos.collect_retailmenot(merchant=merchant_clean, cap=_RESULT_CAP)
    )
    if len(results) < 5:
        seen_codes = {record["code"] for record in results}
        remaining = _RESULT_CAP - len(results)
        results.extend(
            _promos.collect_coupons_com(
                merchant=merchant_clean,
                cap=remaining,
                seen_codes=seen_codes,
            )
        )
    logger.info(
        "search_promo_codes returned %d results for %s",
        len(results),
        merchant_clean,
    )
    return results[:_RESULT_CAP]


def _failure_payload(query: str, exc: BaseException) -> Dict:
    """Standard error envelope for :func:`get_travel_context`."""
    return {
        "success": False,
        "query": query,
        "results": [],
        "error": f"{type(exc).__name__}: {exc}",
        "elapsed_ms": 0,
    }


def get_travel_context(query: str) -> Dict:
    """Fast travel info lookup -- airline policies, airport codes, visa info.

    Uses the local quick-search service. Returns a structured dict whether
    the request succeeds or fails -- never raises.
    """
    started = time.time()
    try:
        response = requests.post(
            _QUICK_SEARCH_URL,
            json={"query": query, "num_results": 5},
            timeout=_QUICK_SEARCH_TIMEOUT_S,
        )
        data = response.json()
        return {
            "success": data.get("success", False),
            "query": query,
            "results": data.get("results", []),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    except URLLIB_JSON_EXCEPTIONS as exc:
        log_and_swallow(
            logger, logging.DEBUG, f"get_travel_context {query!s}", exc,
        )
        return _failure_payload(query, exc)
    except requests.exceptions.RequestException as exc:
        log_and_swallow(
            logger, logging.DEBUG, f"get_travel_context {query!s}", exc,
        )
        return _failure_payload(query, exc)
