"""Apartments.com URL builder + JS-payload helpers (split from
``apartments.py``)."""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import quote

from pydantic import BaseModel


class ApartmentsSearchParams(BaseModel):
    """Bundled args for ``_do_search_apartments`` (param_count fix)."""

    location: str
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    pet_friendly: Optional[bool] = None
    limit: int = 20


def build_search_url(params: ApartmentsSearchParams) -> str:
    """Compose an Apartments.com search URL with embedded filters."""
    location_encoded = quote(params.location)
    base_url = f"https://www.apartments.com/{location_encoded}/"
    parts: List[str] = []
    if params.min_price is not None:
        parts.append(f"minRent={params.min_price}")
    if params.max_price is not None:
        parts.append(f"maxRent={params.max_price}")
    if params.min_beds is not None:
        parts.append(f"minBedrooms={params.min_beds}")
    if params.max_beds is not None:
        parts.append(f"maxBedrooms={params.max_beds}")
    if params.pet_friendly:
        parts.append("petFriendly=dogs,cats")
    if not parts:
        return base_url
    return f"{base_url}?{'&'.join(parts)}"


LISTING_WAIT = (
    "[data-tab-content='all'] .placard, .property-info, article.placard"
)


DETAIL_WAIT = "h1, .property-name, [data-testid='property-name']"


LISTING_CARDS_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(
        '[data-tab-content="all"] .placard, article.placard, '
        + '.property-info'
    );

    cards.forEach(card => {
        const nameEl = card.querySelector(
            '.property-title a, .placardTitle a, h2 a, .property-name a'
        );
        const name = nameEl ? nameEl.textContent.trim() : '';

        const addrEl = card.querySelector(
            '.property-address, .address, [data-testid="property-address"]'
        );
        const address = addrEl ? addrEl.textContent.trim() : '';

        const priceEl = card.querySelector(
            '.property-pricing, .price-range, .rent, '
            + '[data-testid="property-price"]'
        );
        const price_range = priceEl ? priceEl.textContent.trim() : '';

        const bedsEl = card.querySelector(
            '.property-beds, .beds, [data-testid="property-beds"]'
        );
        const beds_range = bedsEl ? bedsEl.textContent.trim() : '';

        const bathsEl = card.querySelector(
            '.property-baths, .baths, [data-testid="property-baths"]'
        );
        const baths_range = bathsEl ? bathsEl.textContent.trim() : '';

        const sqftEl = card.querySelector(
            '.property-sqft, .sqft, [data-testid="property-sqft"]'
        );
        const sqft_range = sqftEl ? sqftEl.textContent.trim() : '';

        const ratingEl = card.querySelector(
            '.rating .score, .property-rating, '
            + '[data-testid="property-rating"]'
        );
        const rating_text = ratingEl ? ratingEl.textContent.trim() : '';

        const reviewEl = card.querySelector(
            '.rating .reviewCount, .reviews-count, '
            + '[data-testid="review-count"]'
        );
        const review_text = reviewEl ? reviewEl.textContent.trim() : '';

        const linkEl = card.querySelector(
            'a[href*="/"], .property-title a'
        );
        let href = linkEl ? linkEl.getAttribute('href') : '';
        if (href && !href.startsWith('http')) {
            href = 'https://www.apartments.com' + href;
        }

        const imgEl = card.querySelector(
            '.property-img img, .listing-img img, '
            + 'img[data-testid="property-image"]'
        );
        const image_url = imgEl ? imgEl.getAttribute('src') : '';

        const amenityEl = card.querySelector(
            '.property-amenities, .amenities, [data-testid="amenities"]'
        );
        const amenities_summary = amenityEl ? amenityEl.textContent.trim() : '';

        const availEl = card.querySelector(
            '.availability, .available, [data-testid="availability"]'
        );
        const available_text = availEl
            ? availEl.textContent.toLowerCase() : '';
        const available_now = available_text.includes('available')
            || available_text.includes('now');

        const petEl = card.querySelector(
            '.pet-policy, [data-testid="pet-policy"]'
        );
        const pet_policy = petEl ? petEl.textContent.trim() : '';

        if (name) {
            results.push({
                name, address, price_range, beds_range, baths_range,
                sqft_range, rating_text, review_text, url: href,
                image_url, amenities_summary, available_now, pet_policy
            });
        }
    });

    return results;
}
"""


LISTING_DETAIL_JS = """
() => {
    const result = {};

    const nameEl = document.querySelector(
        'h1, .property-name, [data-testid="property-name"]'
    );
    result.name = nameEl ? nameEl.textContent.trim() : '';

    const addrEl = document.querySelector(
        '.property-address, .address, [data-testid="property-address"]'
    );
    result.address = addrEl ? addrEl.textContent.trim() : '';

    const phoneEl = document.querySelector(
        '.phone, .property-phone, [data-testid="phone-number"]'
    );
    result.phone = phoneEl ? phoneEl.textContent.trim() : '';

    const webEl = document.querySelector(
        '.website a, .property-website a'
    );
    result.website = webEl ? webEl.getAttribute('href') : '';

    const priceEl = document.querySelector(
        '.price-range, .rent-range, [data-testid="price-range"]'
    );
    result.price_range = priceEl ? priceEl.textContent.trim() : '';

    const ratingEl = document.querySelector(
        '.rating .score, .property-rating, [data-testid="rating"]'
    );
    result.rating_text = ratingEl ? ratingEl.textContent.trim() : '';

    const reviewCountEl = document.querySelector(
        '.review-count, [data-testid="review-count"]'
    );
    result.review_count_text = reviewCountEl
        ? reviewCountEl.textContent.trim() : '';

    result.floor_plans = [];
    const fpElements = document.querySelectorAll(
        '.floor-plan, .unit, [data-testid="floor-plan"]'
    );
    fpElements.forEach(fp => {
        const fpName = fp.querySelector('.floor-plan-name, .unit-name');
        const fpBeds = fp.querySelector('.beds, .bedrooms');
        const fpBaths = fp.querySelector('.baths, .bathrooms');
        const fpSqft = fp.querySelector('.sqft, .square-feet');
        const fpPrice = fp.querySelector('.price, .rent');
        result.floor_plans.push({
            name: fpName ? fpName.textContent.trim() : '',
            beds: fpBeds ? fpBeds.textContent.trim() : '',
            baths: fpBaths ? fpBaths.textContent.trim() : '',
            sqft: fpSqft ? fpSqft.textContent.trim() : '',
            price: fpPrice ? fpPrice.textContent.trim() : ''
        });
    });

    result.amenities = [];
    const amenityElements = document.querySelectorAll(
        '.amenity, .amenities-list li, [data-testid="amenity"]'
    );
    amenityElements.forEach(am => {
        const text = am.textContent.trim();
        if (text) result.amenities.push(text);
    });

    const petEl = document.querySelector(
        '.pet-policy, [data-testid="pet-policy"]'
    );
    result.pet_policy = petEl ? petEl.textContent.trim() : '';

    const parkEl = document.querySelector(
        '.parking, [data-testid="parking"]'
    );
    result.parking = parkEl ? parkEl.textContent.trim() : '';

    const utilEl = document.querySelector(
        '.utilities, [data-testid="utilities"]'
    );
    result.utilities_included = utilEl ? utilEl.textContent.trim() : '';

    const leaseEl = document.querySelector(
        '.lease-terms, [data-testid="lease-terms"]'
    );
    result.lease_terms = leaseEl ? leaseEl.textContent.trim() : '';

    const photosEl = document.querySelector(
        '.photos-count, [data-testid="photos-count"]'
    );
    result.photos_count_text = photosEl ? photosEl.textContent.trim() : '';

    result.reviews = [];
    const reviewElements = document.querySelectorAll(
        '.review, .property-review'
    );
    Array.prototype.slice.call(reviewElements, 0, 3).forEach(rev => {
        const authorEl = rev.querySelector('.reviewer-name, .author');
        const ratingEl = rev.querySelector('.rating, .stars');
        const textEl = rev.querySelector('.review-text, .content');
        const dateEl = rev.querySelector('.review-date, .date');
        result.reviews.push({
            author: authorEl ? authorEl.textContent.trim() : '',
            rating: ratingEl ? ratingEl.textContent.trim() : '',
            text: textEl ? textEl.textContent.trim() : '',
            date: dateEl ? dateEl.textContent.trim() : ''
        });
    });

    return result;
}
"""


# ---------------------------------------------------------------------------
# Scalar parsers
# ---------------------------------------------------------------------------

_RATING_RE = re.compile(r"([\d.]+)")
_NUMBER_RE = re.compile(r"([\d,]+)")
_FLOAT_RE = re.compile(r"([\d.]+)")
_SQFT_RE = re.compile(r"([\d,]+)")


def parse_rating(text: Optional[str]) -> Optional[float]:
    """Pull a float (rating) out of free text like ``"4.5 out of 5"``."""
    if not text:
        return None
    match = _RATING_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_number(text: Optional[str]) -> Optional[int]:
    """Pull a comma-tolerant integer (review count, photos count, ...)."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, OverflowError):
        return None


def parse_float_field(text: str) -> Optional[float]:
    """First float in ``text`` -- ``None`` if not present."""
    if not text:
        return None
    match = _FLOAT_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_sqft_field(text: str) -> Optional[int]:
    """First integer (with commas) in ``text`` -- ``None`` if not present."""
    if not text:
        return None
    match = _SQFT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, OverflowError):
        return None
