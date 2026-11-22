"""Realtor.com URL builder + JS-payload helpers (split from ``realtor.py``)."""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote

from pydantic import BaseModel


REALTOR_BASE_URL = "https://www.realtor.com"


class RealtorSearchParams(BaseModel):
    """Bundled args for ``_do_search_realtor`` (the parameter-count limit fix)."""

    location: str
    listing_type: str = "for_sale"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    limit: int = 20


def _type_param(listing_type: str) -> str:
    """Map ``listing_type`` to the Realtor.com URL fragment."""
    if listing_type == "for_rent":
        return "?optz=rental"
    if listing_type == "sold":
        return "?rspt=2"
    return ""


def _filter_string(
    type_param: str,
    min_price: Optional[int],
    max_price: Optional[int],
    min_beds: Optional[int],
) -> str:
    """Build the trailing ``?key=value&...`` filter string."""
    filters: List[str] = []
    if min_price is not None:
        filters.append(f"min_price={min_price}")
    if max_price is not None:
        filters.append(f"max_price={max_price}")
    if min_beds is not None:
        filters.append(f"min_beds={min_beds}")
    if not filters:
        return ""
    separator = "&" if type_param else "?"
    return separator + "&".join(filters)


def build_search_url(params: RealtorSearchParams) -> str:
    """Compose a Realtor.com search URL with embedded filters."""
    location_encoded = quote(params.location)
    type_param = _type_param(params.listing_type)
    filter_string = _filter_string(
        type_param, params.min_price, params.max_price, params.min_beds,
    )
    return (
        f"{REALTOR_BASE_URL}/realestateandhomes-search/"
        f"{location_encoded}{type_param}{filter_string}"
    )


LISTING_WAIT = (
    "[data-testid='property-card'], .component_property-card, "
    "[data-label='property-card']"
)


DETAIL_WAIT = (
    "h1, [data-testid='property-address'], .property-address"
)


LISTING_CARDS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(
        '[data-testid="property-card"], .component_property-card, '
        + '[data-label="property-card"]'
    );

    cards.forEach(card => {
        const addrEl = card.querySelector(
            '[data-testid="card-address"], .address, .listing-address'
        );
        const address = addrEl ? addrEl.textContent.trim() : '';

        const priceEl = card.querySelector(
            '[data-testid="card-price"], .price, .listing-price'
        );
        const price = priceEl ? priceEl.textContent.trim() : '';

        const bedsEl = card.querySelector(
            '[data-testid="card-beds"], li:contains("bed"), '
            + 'span:contains("bed")'
        );
        let beds = null;
        if (bedsEl) {
            const match = bedsEl.textContent.match(/\\d+/);
            if (match) beds = parseInt(match[0]);
        }

        const bathsEl = card.querySelector(
            '[data-testid="card-baths"], li:contains("bath"), '
            + 'span:contains("bath")'
        );
        let baths = null;
        if (bathsEl) {
            const match = bathsEl.textContent.match(/[\\d.]+/);
            if (match) baths = parseFloat(match[0]);
        }

        const sqftEl = card.querySelector(
            '[data-testid="card-sqft"], li:contains("sqft"), '
            + 'span:contains("sq ft")'
        );
        let sqft = null;
        if (sqftEl) {
            const match = sqftEl.textContent.match(/[\\d,]+/);
            if (match) sqft = parseInt(match[0].replace(/,/g, ''));
        }

        const linkEl = card.querySelector(
            'a[href*="/realestateandhomes-detail/"], a[href*="/property/"]'
        );
        let url = '';
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            url = href.startsWith('http')
                ? href : 'https://www.realtor.com' + href;
        }

        const imgEl = card.querySelector(
            'img[data-testid="card-image"], img'
        );
        const image_url = imgEl
            ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src'))
            : '';

        const statusEl = card.querySelector(
            '[data-testid="card-status"], .status, .listing-status'
        );
        const status = statusEl ? statusEl.textContent.trim() : '';

        const domEl = card.querySelector(
            '[data-testid="card-days-on-market"], .days-on-market'
        );
        let days_on_market = null;
        if (domEl) {
            const match = domEl.textContent.match(/\\d+/);
            if (match) days_on_market = parseInt(match[0]);
        }

        let listing_id = null;
        if (url) {
            const match = url.match(/\\/(\\d+)(?:_|$)/);
            if (match) listing_id = match[1];
        }

        const agentEl = card.querySelector(
            '[data-testid="card-agent"], .agent-name, .listing-agent'
        );
        const agent_name = agentEl ? agentEl.textContent.trim() : '';

        if (address) {
            results.push({
                address, price, beds, baths, sqft, url, image_url,
                status, days_on_market, listing_id, agent_name
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
        'h1, [data-testid="property-address"], .property-address'
    );
    result.address = addrEl ? addrEl.textContent.trim() : '';

    const priceEl = document.querySelector(
        '[data-testid="property-price"], .price, .listing-price'
    );
    result.price = priceEl ? priceEl.textContent.trim() : '';

    const bedsEl = document.querySelector(
        '[data-testid="property-beds"], span:contains("bed"), '
        + 'li:contains("bed")'
    );
    if (bedsEl) {
        const match = bedsEl.textContent.match(/\\d+/);
        if (match) result.beds = parseInt(match[0]);
    }

    const bathsEl = document.querySelector(
        '[data-testid="property-baths"], span:contains("bath"), '
        + 'li:contains("bath")'
    );
    if (bathsEl) {
        const match = bathsEl.textContent.match(/[\\d.]+/);
        if (match) result.baths = parseFloat(match[0]);
    }

    const sqftEl = document.querySelector(
        '[data-testid="property-sqft"], span:contains("sqft"), '
        + 'li:contains("sq ft")'
    );
    if (sqftEl) {
        const match = sqftEl.textContent.match(/[\\d,]+/);
        if (match) result.sqft = parseInt(match[0].replace(/,/g, ''));
    }

    const lotEl = document.querySelector(
        '[data-testid="property-lot"], span:contains("lot"), '
        + 'li:contains("acre")'
    );
    result.lot_size = lotEl ? lotEl.textContent.trim() : '';

    const yearEls = document.querySelectorAll('span, div, li');
    for (let el of yearEls) {
        const text = el.textContent;
        const match = text.match(/(?:built|year)[^\\d]*(\\d{4})/i);
        if (match) {
            result.year_built = parseInt(match[1]);
            break;
        }
    }

    const descEl = document.querySelector(
        '[data-testid="property-description"], .description, '
        + '.listing-description'
    );
    result.description = descEl ? descEl.textContent.trim() : '';

    result.features = [];
    const featureEls = document.querySelectorAll(
        '[data-testid="property-features"] li, .features li, '
        + '.amenities li'
    );
    featureEls.forEach(el => {
        const text = el.textContent.trim();
        if (text) result.features.push(text);
    });

    const agentEl = document.querySelector(
        '[data-testid="listing-agent"], .agent-name, .listing-agent'
    );
    if (agentEl) {
        result.agent_name = agentEl.textContent.trim();
    }

    result.price_history = [];
    const priceRows = document.querySelectorAll(
        '[data-testid="price-history"] tr, .price-history tr'
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
