"""Hotel-card extractors per source (Kayak, Hotels.com, Playwright fallback).

Each ``collect_*`` returns up to ``cap`` records. Per-card parse failures
are caught by :func:`_travel_helpers.safe_card_parse`.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import _travel_helpers as _th
from ._travel_helpers import HotelQuery


# Compile-once: hotel-card class / data-testid patterns are constant; compiling
# these per scraper call repeats the cost on every search request.
_HOTEL_TITLE_CLASS_RE = re.compile(r"title|name", re.I)
_HOTEL_PRICE_TEXT_RE = re.compile(r"\$[\d,]+")
_KAYAK_REVIEW_SCORE_RE = re.compile(r"review-score", re.I)
_KAYAK_SCORE_CLASS_RE = re.compile(r"score|rating", re.I)
_KAYAK_PROPERTY_CARD_TESTID_RE = re.compile(r"property-card|hotel", re.I)
_KAYAK_PROPERTY_CLASS_RE = re.compile(r"property|hotel|sr_item", re.I)
_HOTELS_COM_RATING_CLASS_RE = re.compile(r"rating|score", re.I)
_HOTELS_COM_CARD_CLASS_RE = re.compile(r"hotel|property|listing", re.I)
_KAYAK_PW_PROPERTY_CARD_RE = re.compile(r"property-card", re.I)


@dataclass(frozen=True)
class _HotelCtx:
    """Per-source bag for hotel card collection."""

    location: str
    rating_selectors: list
    base_url: str
    fallback_url: str
    source: str
    cap: int
    card_cap: int = 20


def _name_of(card) -> str:
    name_el = card.find(
        ["h3", "h2", "a"],
        class_=_HOTEL_TITLE_CLASS_RE,
    )
    if not name_el:
        return "Unknown Hotel"
    return name_el.get_text(strip=True) or "Unknown Hotel"


def _price_of(card):
    price_el = card.find(string=_HOTEL_PRICE_TEXT_RE)
    if not price_el:
        return None, None
    return _th.extract_price(price_el)


def _rating_of(card, selectors: list) -> Optional[float]:
    """Try each selector + class regex; return the first parsed rating."""
    rating_el = None
    for sel in selectors:
        rating_el = card.find(**sel) if isinstance(sel, dict) else card.find(sel)
        if rating_el:
            break
    if not rating_el:
        return None
    return _th.parse_rating_tenth(rating_el.get_text())


def _absolute_url(href: Optional[str], fallback: str, base: str) -> str:
    if not href:
        return fallback
    if href.startswith("http"):
        return href
    return base + href


def _build_hotel_record(card, ctx: _HotelCtx) -> Optional[Dict]:
    name = _name_of(card)
    if name == "Unknown Hotel":
        return None
    price, currency = _price_of(card)
    rating = _rating_of(card, ctx.rating_selectors)
    link_el = card.find("a", href=True)
    href = link_el["href"] if link_el else None
    hotel_url = _absolute_url(href, ctx.fallback_url, ctx.base_url)
    return {
        "name": name,
        "price_per_night": price,
        "currency": currency or "$",
        "rating": rating,
        "location": ctx.location,
        "url": hotel_url,
        "source": ctx.source,
    }


def _collect_hotels(cards: list, ctx: _HotelCtx) -> List[Dict]:
    out: List[Dict] = []
    seen: set[str] = set()
    for card in cards[:ctx.card_cap]:
        record = _th.safe_card_parse(
            lambda c=card: _build_hotel_record(c, ctx),
            where=f"hotel card ({ctx.source})",
        )
        if record is None:
            continue
        key = f"{record['name']}_{record['price_per_night']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
        if len(out) >= ctx.cap:
            break
    return out


def kayak_hotels_url(query: HotelQuery) -> str:
    encoded = urllib.parse.quote_plus(query.location)
    return (
        f"https://www.Kayak.com/searchresults.html?ss={encoded}"
        f"&checkin={query.checkin or ''}&checkout={query.checkout or ''}"
        f"&group_adults={query.guests}"
    )


_KAYAK_RATING_SELECTORS = [
    {"attrs": {"data-testid": _KAYAK_REVIEW_SCORE_RE}},
    {"class_": _KAYAK_SCORE_CLASS_RE},
]


def collect_kayak_hotels(query: HotelQuery, *, cap: int) -> List[Dict]:
    url = kayak_hotels_url(query)
    resp = _th.requests_get(url, timeout=10)
    if not resp:
        return []
    soup = _th.soup_of(resp)
    cards = soup.find_all(
        ["div", "li"],
        attrs={"data-testid": _KAYAK_PROPERTY_CARD_TESTID_RE},
    )
    if not cards:
        cards = soup.find_all(
            ["div", "li"],
            class_=_KAYAK_PROPERTY_CLASS_RE,
        )
    ctx = _HotelCtx(
        location=query.location,
        rating_selectors=_KAYAK_RATING_SELECTORS,
        base_url="https://www.Kayak.com",
        fallback_url=url,
        source="Kayak.com",
        cap=cap,
    )
    return _collect_hotels(cards, ctx)


def hotels_com_url(location: str) -> str:
    encoded = urllib.parse.quote_plus(location)
    return f"https://www.hotels.com/search.do?q-destination={encoded}"


_HOTELS_COM_RATING_SELECTORS = [
    {"class_": _HOTELS_COM_RATING_CLASS_RE},
]


def collect_hotels_com(*, location: str, cap: int) -> List[Dict]:
    url = hotels_com_url(location)
    resp = _th.requests_get(url, timeout=10)
    if not resp:
        return []
    soup = _th.soup_of(resp)
    cards = soup.find_all(
        ["div", "li"],
        class_=_HOTELS_COM_CARD_CLASS_RE,
    )
    ctx = _HotelCtx(
        location=location,
        rating_selectors=_HOTELS_COM_RATING_SELECTORS,
        base_url="https://www.hotels.com",
        fallback_url=url,
        source="hotels.com",
        cap=cap,
        card_cap=15,
    )
    return _collect_hotels(cards, ctx)


async def collect_playwright_kayak_hotels(
    *, location: str, cap: int,
) -> List[Dict]:
    encoded = urllib.parse.quote_plus(location)
    url = f"https://www.Kayak.com/searchresults.html?ss={encoded}"
    html = await _th.playwright_scrape(url, timeout=15)
    if not html:
        return []
    soup = _th.soup_of(html)
    cards = soup.find_all(
        ["div", "li"],
        attrs={"data-testid": _KAYAK_PW_PROPERTY_CARD_RE},
    )
    ctx = _HotelCtx(
        location=location,
        rating_selectors=_KAYAK_RATING_SELECTORS,
        base_url="https://www.Kayak.com",
        fallback_url=url,
        source="Kayak.com_pw",
        cap=cap,
        card_cap=15,
    )
    return _collect_hotels(cards, ctx)
