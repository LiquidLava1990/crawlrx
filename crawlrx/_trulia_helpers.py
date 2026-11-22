"""Trulia JS-payload + URL builder helpers (split from ``trulia.py``)."""
from __future__ import annotations

from urllib.parse import quote
from typing import List, Optional

from pydantic import BaseModel


TRULIA_BASE_URL = "https://www.trulia.com"


class TruliaSearchParams(BaseModel):
    """Bundled args for ``_do_search_trulia`` (the parameter-count limit fix)."""

    location: str
    listing_type: str = "for_sale"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    limit: int = 20


_TYPE_MAP = {
    "for_sale": "for_sale",
    "for_rent": "for_rent",
    "sold": "sold",
}


def _query_params(params: TruliaSearchParams) -> str:
    """Build the query-string body for the search URL."""
    parts: List[str] = []
    if params.min_price is not None:
        parts.append(f"minPrice={params.min_price}")
    if params.max_price is not None:
        parts.append(f"maxPrice={params.max_price}")
    if params.min_beds is not None:
        parts.append(f"minBeds={params.min_beds}")
    return "&".join(parts)


def build_search_url(params: TruliaSearchParams) -> str:
    """Compose a Trulia search URL with embedded filters."""
    location_encoded = quote(params.location.replace(" ", "-").lower())
    prop_type = _TYPE_MAP.get(params.listing_type, "for_sale")
    base = f"{TRULIA_BASE_URL}/{prop_type}/{location_encoded}"
    query = _query_params(params)
    return f"{base}?{query}" if query else base


LISTING_WAIT = (
    "[data-testid='home-card'], [data-testid='property-card'], .PropertyCard"
)


DETAIL_WAIT = (
    "h1, [data-testid='property-details'], [data-testid='home-details']"
)


LISTING_CARDS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(
        '[data-testid="home-card"], [data-testid="property-card"], '
        + '.PropertyCard, [data-testid="srp-home-card"]'
    );
    cards.forEach(card => {
        const addressEl = card.querySelector(
            '[data-testid="property-address"], '
            + '[data-testid="home-address"] address, '
            + '.PropertyCard__Address, h3 a'
        );
        const priceEl = card.querySelector(
            '[data-testid="property-price"], '
            + '[data-testid="home-price"], .PropertyCard__Price'
        );
        const bedsEl = card.querySelector(
            '[data-testid="property-beds"], [data-testid="home-beds"], '
            + '[aria-label*="bed"]'
        );
        const bathsEl = card.querySelector(
            '[data-testid="property-baths"], [data-testid="home-baths"], '
            + '[aria-label*="bath"]'
        );
        const sqftEl = card.querySelector(
            '[data-testid="property-sqft"], [data-testid="home-sqft"], '
            + '[aria-label*="sqft"]'
        );
        const linkEl = card.querySelector(
            'a[href*="/p/"], a[href*="/homedetails/"]'
        );
        const imgEl = card.querySelector(
            'img[data-testid="property-image"], '
            + 'img[data-testid="home-image"], img'
        );
        const domEl = card.querySelector(
            '[data-testid="property-days"], .PropertyCard__DaysOnMarket'
        );
        const typeEl = card.querySelector(
            '[data-testid="property-type"], .PropertyCard__Type'
        );

        const address = addressEl ? addressEl.textContent.trim() : '';
        const price = priceEl ? priceEl.textContent.trim() : '';
        const beds = bedsEl ? bedsEl.textContent.match(/\\d+/) : null;
        const baths = bathsEl ? bathsEl.textContent.match(/[\\d.]+/) : null;
        const sqft = sqftEl ? sqftEl.textContent.match(/[\\d,]+/) : null;
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const image = imgEl
            ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src'))
            : '';
        const days_on_market = domEl ? domEl.textContent.trim() : '';
        const prop_type = typeEl ? typeEl.textContent.trim() : '';

        if (address && price) {
            results.push({
                address, price,
                beds: beds ? parseInt(beds[0]) : null,
                baths: baths ? parseFloat(baths[0]) : null,
                sqft: sqft ? parseInt(sqft[0].replace(/,/g, '')) : null,
                url: href.startsWith('http')
                    ? href : 'https://www.trulia.com' + href,
                image_url: image, days_on_market,
                property_type: prop_type
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
        'h1 [data-testid="property-address"], h1, '
        + '[data-testid="home-address"]'
    );
    result.address = addrEl ? addrEl.textContent.trim() : '';
    const priceEl = document.querySelector(
        '[data-testid="property-price"], '
        + '[data-testid="home-price"], [data-testid="price"]'
    );
    result.price = priceEl ? priceEl.textContent.trim() : '';
    const bedsEl = document.querySelector(
        '[data-testid="property-beds"], [data-testid="home-beds"], '
        + '[data-testid="beds"]'
    );
    result.beds = bedsEl ? bedsEl.textContent.match(/\\d+/) : null;
    const bathsEl = document.querySelector(
        '[data-testid="property-baths"], [data-testid="home-baths"], '
        + '[data-testid="baths"]'
    );
    result.baths = bathsEl ? bathsEl.textContent.match(/[\\d.]+/) : null;
    const sqftEl = document.querySelector(
        '[data-testid="property-sqft"], [data-testid="home-sqft"], '
        + '[data-testid="sqft"]'
    );
    result.sqft = sqftEl ? sqftEl.textContent.match(/[\\d,]+/) : null;
    const yearEl = document.querySelector(
        '[data-testid="year-built"], [data-testid="home-year-built"]'
    );
    result.year_built = yearEl ? yearEl.textContent.trim() : '';
    const descEl = document.querySelector(
        '[data-testid="property-description"], '
        + '[data-testid="home-description"], [data-testid="description"]'
    );
    result.description = descEl ? descEl.textContent.trim() : '';
    result.features = [];
    const featureEls = document.querySelectorAll(
        '[data-testid="property-feature"], '
        + '[data-testid="home-feature"], [data-testid="feature"]'
    );
    featureEls.forEach(el => {
        const text = el.textContent.trim();
        if (text) result.features.push(text);
    });
    result.school_ratings = [];
    const schoolEls = document.querySelectorAll(
        '[data-testid="school-rating"], [data-testid="nearby-school"]'
    );
    schoolEls.forEach(el => {
        const nameEl = el.querySelector(
            '[data-testid="school-name"], h4, h5'
        );
        const ratingEl = el.querySelector(
            '[data-testid="school-rating-value"], .rating'
        );
        const distEl = el.querySelector(
            '[data-testid="school-distance"], .distance'
        );
        const levelEl = el.querySelector(
            '[data-testid="school-level"], .level'
        );
        if (nameEl) {
            result.school_ratings.push({
                name: nameEl.textContent.trim(),
                rating: ratingEl ? ratingEl.textContent.trim() : '',
                distance: distEl ? distEl.textContent.trim() : '',
                level: levelEl ? levelEl.textContent.trim() : ''
            });
        }
    });
    const walkEl = document.querySelector(
        '[data-testid="walk-score"], [data-testid="walkscore"]'
    );
    result.walk_score = walkEl ? walkEl.textContent.trim() : '';
    result.neighborhood_stats = {};
    const statEls = document.querySelectorAll(
        '[data-testid="neighborhood-stat"]'
    );
    statEls.forEach(el => {
        const labelEl = el.querySelector(
            '.label, [data-testid="stat-label"]'
        );
        const valueEl = el.querySelector(
            '.value, [data-testid="stat-value"]'
        );
        if (labelEl && valueEl) {
            result.neighborhood_stats[labelEl.textContent.trim()] =
                valueEl.textContent.trim();
        }
    });
    return result;
}
"""
