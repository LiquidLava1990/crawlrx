"""Zillow card / detail / comps extraction helpers (split from ``zillow.py``).

Three responsibility blocks:

* ``CardListing`` parsers -- mapped from ``page.evaluate`` payloads.
* ``PropertyDetail`` parser -- maps a single detail payload to the model.
* ``Comps`` parsers -- maps the recently-sold listing payload + zestimate.

Splitting these out keeps ``zillow.py`` under the 500 LOC ceiling and
gives each helper a single responsibility (complexity thresholds met).
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from urllib.parse import quote

from ._realestate_helpers import (
    parse_js_float,
    parse_js_int,
    parse_js_int_with_commas,
)


logger = logging.getLogger("crawlrx.zillow.helpers")

ZILLOW_BASE = "https://www.zillow.com"


# ---------------------------------------------------------------------------
# Search-URL builder bits
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "for_sale": "Cat0",
    "for_rent": "Cat2",
    "sold": "Cat3",
}


def status_token(listing_type: str) -> str:
    """Map ``listing_type`` to the Zillow ``Cat*`` token."""
    return _STATUS_MAP.get(listing_type, "Cat0")


def _price_filter(min_price: Optional[int], max_price: Optional[int]) -> str:
    """Build the ``"mp"`` filter blob for the searchQueryState payload."""
    if min_price is None and max_price is None:
        return ""
    parts: List[str] = []
    if min_price is not None:
        parts.append(f"\"min\":{min_price}")
    if max_price is not None:
        parts.append(f"\"max\":{max_price}")
    return "\"mp\":{\"value\":{" + ",".join(parts) + "}}"


def filter_state(
    listing_type: str,
    min_price: Optional[int],
    max_price: Optional[int],
    min_beds: Optional[int],
) -> str:
    """Compose the comma-joined Zillow filter-state body."""
    status = status_token(listing_type)
    parts: List[str] = [f"\"fr\":{{\"value\":true,\"status\":\"{status}\"}}"]
    price = _price_filter(min_price, max_price)
    if price:
        parts.append(price)
    if min_beds is not None:
        parts.append(f"\"beds\":{{\"min\":{min_beds}}}")
    return ",".join(parts)


def build_search_url(
    location: str,
    listing_type: str,
    min_price: Optional[int],
    max_price: Optional[int],
    min_beds: Optional[int],
) -> str:
    """Compose the canonical Zillow search URL with embedded filter state."""
    location_encoded = quote(location)
    state = filter_state(listing_type, min_price, max_price, min_beds)
    return (
        f"{ZILLOW_BASE}/homes/{location_encoded}_rb/"
        "?searchQueryState={%22pagination%22:{%22currentPage%22:1},"
        f"{state}}}"
    )


# ---------------------------------------------------------------------------
# Page-evaluate JavaScript payloads (extracted as constants)
# ---------------------------------------------------------------------------

LISTING_CARDS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(
        'article[data-test="property-card"], .list-card, '
        + '[data-testid="property-card"]'
    );
    cards.forEach(card => {
        const addressEl = card.querySelector(
            'address[data-test="property-card-addr"], '
            + '.list-card-addr, address'
        );
        const priceEl = card.querySelector(
            '[data-test="property-card-price"], .list-card-price'
        );
        const bedsEl = card.querySelector(
            '[data-test="property-card-beds"], '
            + 'li:has([alt*="bed"]), li:has(svg[title*="Bed"])'
        );
        const bathsEl = card.querySelector(
            '[data-test="property-card-baths"], '
            + 'li:has([alt*="bath"]), li:has(svg[title*="Bath"])'
        );
        const sqftEl = card.querySelector(
            '[data-test="property-card-sqft"], li:has([alt*="sqft"])'
        );
        const linkEl = card.querySelector(
            'a[href*="/homedetails/"], a[data-test="property-card-link"]'
        );
        const imgEl = card.querySelector(
            'img[data-testid="property-card-img"], img'
        );

        const address = addressEl ? addressEl.textContent.trim() : '';
        const price = priceEl ? priceEl.textContent.trim() : '';
        const beds = bedsEl ? bedsEl.textContent.match(/\\d+/) : null;
        const baths = bathsEl ? bathsEl.textContent.match(/[\\d.]+/) : null;
        const sqft = sqftEl ? sqftEl.textContent.match(/[\\d,]+/) : null;
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const image = imgEl ? imgEl.getAttribute('src') : '';

        if (address) {
            results.push({
                address, price,
                beds: beds ? parseInt(beds[0]) : null,
                baths: baths ? parseFloat(baths[0]) : null,
                sqft: sqft ? parseInt(sqft[0].replace(/,/g, '')) : null,
                url: href.startsWith('http')
                    ? href : 'https://www.zillow.com' + href,
                image_url: image
            });
        }
    });
    return results;
}
"""


PROPERTY_DETAIL_JS = """
() => {
    const result = {};
    const addrEl = document.querySelector(
        'h1, [data-testid="home-details"] h1, .ds-address-container h1'
    );
    result.address = addrEl ? addrEl.textContent.trim() : '';
    const priceEl = document.querySelector(
        '[data-testid="price"], .ds-price, '
        + '[data-testid="home-details"] span'
    );
    result.price = priceEl ? priceEl.textContent.trim() : '';
    const bedsEl = document.querySelector(
        '[data-testid="bed"], [data-testid="beds"], [aria-label*="bed"]'
    );
    result.beds = bedsEl ? bedsEl.textContent.match(/\\d+/) : null;
    const bathsEl = document.querySelector(
        '[data-testid="bath"], [data-testid="baths"], [aria-label*="bath"]'
    );
    result.baths = bathsEl ? bathsEl.textContent.match(/[\\d.]+/) : null;
    const sqftEl = document.querySelector(
        '[data-testid="sqft"], [aria-label*="square"]'
    );
    result.sqft = sqftEl ? sqftEl.textContent.match(/[\\d,]+/) : null;
    const yearEls = document.querySelectorAll('span');
    for (let el of yearEls) {
        const text = el.textContent;
        const match = text.match(/Built in (\\d{4})/);
        if (match) {
            result.year_built = parseInt(match[1]);
            break;
        }
    }
    const zestEl = document.querySelector('[data-testid="zestimate"]');
    result.zestimate = zestEl ? zestEl.textContent.trim() : '';
    result.price_history = [];
    const priceRows = document.querySelectorAll(
        '[data-testid="price-history"] tr, .ds-price-history-table tr'
    );
    priceRows.forEach(row => {
        const cells = row.querySelectorAll('td');
        if (cells.length >= 2) {
            result.price_history.push({
                date: cells[0] ? cells[0].textContent.trim() : '',
                event: cells[1] ? cells[1].textContent.trim() : '',
                price: cells[2] ? cells[2].textContent.trim() : ''
            });
        }
    });
    return result;
}
"""


COMPS_LISTINGS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(
        'article[data-test="property-card"], .list-card, '
        + '[data-testid="property-card"]'
    );
    cards.forEach(card => {
        const addressEl = card.querySelector(
            'address[data-test="property-card-addr"], '
            + '.list-card-addr, address'
        );
        const priceEl = card.querySelector(
            '[data-test="property-card-price"], .list-card-price'
        );
        const bedsEl = card.querySelector(
            '[data-test="property-card-beds"], li:has([alt*="bed"])'
        );
        const bathsEl = card.querySelector(
            '[data-test="property-card-baths"], li:has([alt*="bath"])'
        );
        const sqftEl = card.querySelector(
            '[data-test="property-card-sqft"], li:has([alt*="sqft"])'
        );
        const linkEl = card.querySelector(
            'a[href*="/homedetails/"], a[data-test="property-card-link"]'
        );
        const addr = addressEl ? addressEl.textContent.trim() : '';
        const price = priceEl ? priceEl.textContent.trim() : '';
        const beds = bedsEl ? bedsEl.textContent.match(/\\d+/) : null;
        const baths = bathsEl ? bathsEl.textContent.match(/[\\d.]+/) : null;
        const sqft = sqftEl ? sqftEl.textContent.match(/[\\d,]+/) : null;
        const href = linkEl ? linkEl.getAttribute('href') : '';
        if (addr && price && price.toLowerCase().includes('$')) {
            results.push({
                address: addr, sale_price: price,
                beds: beds ? parseInt(beds[0]) : null,
                baths: baths ? parseFloat(baths[0]) : null,
                sqft: sqft ? parseInt(sqft[0].replace(/,/g, '')) : null,
                url: href.startsWith('http')
                    ? href : 'https://www.zillow.com' + href
            });
        }
    });
    return results;
}
"""


# ---------------------------------------------------------------------------
# Comp price-per-sqft + zestimate parsers
# ---------------------------------------------------------------------------

_PRICE_DIGITS_RE = re.compile(r"[^\d]")
_ZEST_RE = re.compile(r"Zestimate[^\d$]*\$?([\d,]+)")


def price_per_sqft_label(
    price_str: str, sqft_val: object,
) -> Optional[str]:
    """Compose ``$NN`` label or ``None`` when either input is missing."""
    if not price_str or not sqft_val:
        return None
    digits = _PRICE_DIGITS_RE.sub("", price_str)
    if not digits:
        return None
    try:
        sqft = int(sqft_val)
        if sqft <= 0:
            return None
        return f"${int(digits) // sqft}"
    except (ValueError, OverflowError, TypeError):
        return None


def parse_zestimate(body_text: str) -> Optional[str]:
    """Pull the first ``Zestimate ... $X,YYY`` substring out of a body blob."""
    match = _ZEST_RE.search(body_text or "")
    if not match:
        return None
    return f"${match.group(1)}"


# ---------------------------------------------------------------------------
# Param bundles for the search / property / comps callers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ZillowSearchParams:
    """Bundled search args (replaces the 6-positional ``_do_search_zillow``)."""

    location: str
    listing_type: str = "for_sale"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    limit: int = 20


def random_uniform(low: float, high: float) -> float:
    """Re-export ``random.uniform`` so tests can swap the seed source."""
    return random.uniform(low, high)
