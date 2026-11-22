"""Flight-card extractors per source (Google Flights, Kayak, Playwright).

Each ``collect_*`` returns a list of result dicts so the orchestrator
in :mod:`crawlrx.travel_scraper` is just a flat ``extend`` chain. Per-card
parse failures land in :func:`_travel_helpers.safe_card_parse`, never in
a bare ``except Exception``.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from . import _travel_helpers as _th
from ._travel_helpers import FlightQuery


# Compile-once: card-class and price-text patterns are constant; compiling
# per search call multiplies the cost across the request fan-out.
_GFLIGHTS_CARD_CLASS_RE = re.compile(r"flight|result|card|ticket", re.I)
_KAYAK_PRICE_TEXT_RE = re.compile(r"\$[\d,]+")
_PW_GFLIGHTS_CARD_CLASS_RE = re.compile(r"flight|result|card", re.I)


@dataclass(frozen=True)
class _CardCtx:
    """Per-source context bag for the shared card-collection loop."""

    query: FlightQuery
    url: str
    source: str
    cap: int


def _record_for(
    *, airline: str, price: str, currency: Optional[str], ctx: _CardCtx,
) -> Dict:
    """Assemble one flight result dict from already-extracted fields."""
    q = ctx.query
    return {
        "airline": airline,
        "price": price,
        "currency": currency or "$",
        "origin": q.origin,
        "destination": q.destination,
        "depart_date": q.depart_date,
        "return_date": q.return_date,
        "url": ctx.url,
        "source": ctx.source,
    }


def _build_card_record(card, ctx: _CardCtx) -> Optional[Dict]:
    """Extract one flight record from a soup card; return None on miss."""
    text = card.get_text(separator=" ", strip=True)
    if not text or len(text) < 20:
        return None
    price, currency = _th.extract_price(text)
    if not price:
        return None
    return _record_for(
        airline=_th.airline_in(text), price=price, currency=currency, ctx=ctx,
    )


def _collect_from_cards(cards: list, ctx: _CardCtx) -> List[Dict]:
    """Iterate cards, dedupe by airline, return up to ``ctx.cap`` records."""
    out: List[Dict] = []
    seen: set[str] = set()
    for card in cards[:20]:
        record = _th.safe_card_parse(
            lambda c=card: _build_card_record(c, ctx),
            where=f"flight card ({ctx.source})",
        )
        if record is None or record["airline"] in seen:
            continue
        seen.add(record["airline"])
        out.append(record)
        if len(out) >= ctx.cap:
            break
    return out


def google_flights_url(origin: str, destination: str) -> str:
    """Build the Google Flights search URL for a route."""
    query = urllib.parse.quote_plus(f"flights {origin} to {destination}")
    return f"https://www.google.com/travel/flights?q={query}"


def collect_google_flights(query: FlightQuery, *, cap: int) -> List[Dict]:
    """Search Google Flights via requests; return up to ``cap`` records."""
    url = google_flights_url(query.origin, query.destination)
    resp = _th.requests_get(url, timeout=10)
    if not resp:
        return []
    soup = _th.soup_of(resp)
    cards = soup.find_all(
        ["div", "li"],
        class_=_GFLIGHTS_CARD_CLASS_RE,
    )
    ctx = _CardCtx(query=query, url=url, source="google_flights", cap=cap)
    return _collect_from_cards(cards, ctx)


def kayak_url(origin: str, destination: str, depart_date: str) -> str:
    """Build Kayak's flight URL for a route + date."""
    date_part = f"/{depart_date}" if depart_date else ""
    return f"https://www.kayak.com/flights/{origin}-{destination}{date_part}"


def _kayak_card_record(el, ctx: _CardCtx) -> Optional[Dict]:
    price, currency = _th.extract_price(el)
    if not price:
        return None
    parent = el.parent
    parent_text = (
        parent.get_text(separator=" ", strip=True) if parent else ""
    )
    return _record_for(
        airline=_th.airline_in(parent_text),
        price=price,
        currency=currency,
        ctx=ctx,
    )


def collect_kayak(query: FlightQuery, *, cap: int) -> List[Dict]:
    """Search Kayak via requests; return up to ``cap`` records."""
    url = kayak_url(query.origin, query.destination, query.depart_date)
    resp = _th.requests_get(url, timeout=10)
    if not resp:
        return []
    soup = _th.soup_of(resp)
    price_els = soup.find_all(string=_KAYAK_PRICE_TEXT_RE)
    ctx = _CardCtx(query=query, url=url, source="kayak", cap=cap)
    out: List[Dict] = []
    seen: set[str] = set()
    for el in price_els[:15]:
        record = _th.safe_card_parse(
            lambda e=el: _kayak_card_record(e, ctx),
            where="kayak price",
        )
        if record is None:
            continue
        key = f"{record['airline']}_{record['price']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
        if len(out) >= cap:
            break
    return out


async def collect_playwright_google_flights(
    query: FlightQuery, *, cap: int,
) -> List[Dict]:
    """Playwright fallback for Google Flights when requests yields too few."""
    url = google_flights_url(query.origin, query.destination)
    html = await _th.playwright_scrape(url, timeout=15)
    if not html:
        return []
    soup = _th.soup_of(html)
    cards = soup.find_all(
        ["div", "li"],
        class_=_PW_GFLIGHTS_CARD_CLASS_RE,
    )
    ctx = _CardCtx(query=query, url=url, source="google_flights_pw", cap=cap)
    return _collect_from_cards(cards, ctx)
