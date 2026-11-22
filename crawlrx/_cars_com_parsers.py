"""Parsers + JS extraction snippets for cars_com.py.

Split from ``cars_com.py`` to keep that module under the 500-line ceiling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────

class CarListing(BaseModel):
    """Single car listing from search."""

    year: Optional[int] = Field(default=None, description="Vehicle year")
    make: Optional[str] = Field(default=None, description="Vehicle make")
    model: Optional[str] = Field(default=None, description="Vehicle model")
    trim: Optional[str] = Field(default=None, description="Vehicle trim level")
    price: Optional[str] = Field(default=None, description="Listed price")
    mileage: Optional[int] = Field(default=None, description="Vehicle mileage")
    location: Optional[str] = Field(default=None, description="Vehicle location")
    dealer_name: Optional[str] = Field(default=None, description="Dealer/seller name")
    listing_url: str = Field(..., description="URL to vehicle detail page")
    image_url: Optional[str] = Field(default=None, description="Vehicle image URL")
    rating: Optional[float] = Field(default=None, description="Dealer or vehicle rating")
    accident_reported: Optional[bool] = Field(
        default=None, description="Whether accident was reported"
    )


class DealerInfo(BaseModel):
    """Dealer information."""

    name: Optional[str] = Field(default=None, description="Dealer name")
    address: Optional[str] = Field(default=None, description="Dealer address")
    phone: Optional[str] = Field(default=None, description="Dealer phone")
    rating: Optional[float] = Field(default=None, description="Dealer rating")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")


class PriceHistoryItem(BaseModel):
    """Price history entry."""

    date: Optional[str] = Field(default=None, description="Date of price change")
    price: Optional[str] = Field(default=None, description="Price at that date")
    event: Optional[str] = Field(default=None, description="Event type")


class VehicleDetail(BaseModel):
    """Detailed vehicle information."""

    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    price: Optional[str] = None
    mileage: Optional[int] = None
    vin: Optional[str] = None
    engine: Optional[str] = None
    transmission: Optional[str] = None
    exterior_color: Optional[str] = None
    interior_color: Optional[str] = None
    mpg: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    dealer_info: Optional[DealerInfo] = None
    price_history: List[PriceHistoryItem] = Field(default_factory=list)
    carfax_link: Optional[str] = None
    accident_reported: Optional[bool] = None
    owners_count: Optional[int] = None
    usage_type: Optional[str] = None


@dataclass(frozen=True)
class CarsSearchArgs:
    """Bundle of /search query parameters; keeps URL-build + scrape ≤5 args."""

    make: Optional[str] = None
    model: Optional[str] = None
    zip_code: str = "40202"
    max_price: Optional[int] = None
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    max_mileage: Optional[int] = None
    listing_type: str = "any"
    limit: int = 20


# ─────────────────────────────────────────────
# Number / price helpers
# ─────────────────────────────────────────────

def extract_number(text: str) -> Optional[int]:
    """Extract first integer from text."""
    if not text:
        return None
    match = re.search(r'([\d,]+)', text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_price(text: str) -> Optional[str]:
    """Extract price from text."""
    if not text:
        return None
    match = re.search(r'\$[\d,]+', text)
    return match.group(0) if match else None


def _extract_float(text: str) -> Optional[float]:
    """First float in ``text`` or ``None`` if unparseable."""
    if not text:
        return None
    match = re.search(r'([\d.]+)', text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


# ─────────────────────────────────────────────
# JS extraction snippets (verbatim from prior implementation)
# ─────────────────────────────────────────────

LISTING_EXTRACT_JS = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-testid="listing-card"], .vehicle-card, .sds-listing-card');
    cards.forEach(card => {
        const titleEl = card.querySelector('.title, .vehicle-name, h2 a, [data-testid="vehicle-name"]');
        const title = titleEl ? titleEl.textContent.trim() : '';
        let year = null, make = null, model = null, trim = null;
        const titleMatch = title.match(/(\d{4})\s+(\w+)\s+(.+)/);
        if (titleMatch) {
            year = parseInt(titleMatch[1]);
            make = titleMatch[2];
            const modelTrim = titleMatch[3].split(' - ');
            model = modelTrim[0].trim();
            trim = modelTrim[1] ? modelTrim[1].trim() : null;
        }
        const priceEl = card.querySelector('.price, .primary-price, [data-testid="price"]');
        const price = priceEl ? priceEl.textContent.trim() : '';
        const mileageEl = card.querySelector('.mileage, .vehicle-mileage, [data-testid="mileage"]');
        const mileage_text = mileageEl ? mileageEl.textContent.trim() : '';
        const locEl = card.querySelector('.location, .dealer-location, [data-testid="location"]');
        const location = locEl ? locEl.textContent.trim() : '';
        const dealerEl = card.querySelector('.dealer-name, .seller-name, [data-testid="dealer-name"]');
        const dealer_name = dealerEl ? dealerEl.textContent.trim() : '';
        const linkEl = card.querySelector('a[href*="/vehicledetail/"], a.vehicle-card-link');
        let href = linkEl ? linkEl.getAttribute('href') : '';
        if (href && !href.startsWith('http')) href = 'https://www.cars.com' + href;
        const imgEl = card.querySelector('.vehicle-image img, .sds-image img, img[data-testid="vehicle-image"]');
        const image_url = imgEl ? imgEl.getAttribute('src') : '';
        const ratingEl = card.querySelector('.rating, .dealer-rating, [data-testid="rating"]');
        const rating_text = ratingEl ? ratingEl.textContent.trim() : '';
        const accidentEl = card.querySelector('.accident-badge, .accident-reported, [data-testid="accident-badge"]');
        const accident_text = accidentEl ? accidentEl.textContent.toLowerCase() : '';
        const accident_reported = accident_text.includes('accident');
        if (title) {
            results.push({title, year, make, model, trim, price, mileage_text, location,
                dealer_name, url: href, image_url, rating_text, accident_reported});
        }
    });
    return results;
}
"""


VEHICLE_EXTRACT_JS = r"""
() => {
    const result = {};
    const titleEl = document.querySelector('h1, .vehicle-title, [data-testid="vehicle-title"]');
    const title = titleEl ? titleEl.textContent.trim() : '';
    const titleMatch = title.match(/(\d{4})\s+(\w+)\s+(.+)/);
    if (titleMatch) {
        result.year = parseInt(titleMatch[1]);
        result.make = titleMatch[2];
        const modelTrim = titleMatch[3].split(' - ');
        result.model = modelTrim[0].trim();
        result.trim = modelTrim[1] ? modelTrim[1].trim() : '';
    }
    const priceEl = document.querySelector('.price, .primary-price, [data-testid="price"]');
    result.price = priceEl ? priceEl.textContent.trim() : '';
    const mileageEl = document.querySelector('.mileage, .vehicle-mileage, [data-testid="mileage"]');
    result.mileage_text = mileageEl ? mileageEl.textContent.trim() : '';
    const vinEl = document.querySelector('.vin, [data-testid="vin"]');
    result.vin = vinEl ? vinEl.textContent.trim() : '';
    const engineEl = document.querySelector('.engine, [data-testid="engine"]');
    result.engine = engineEl ? engineEl.textContent.trim() : '';
    const transEl = document.querySelector('.transmission, [data-testid="transmission"]');
    result.transmission = transEl ? transEl.textContent.trim() : '';
    const extColorEl = document.querySelector('.exterior-color, [data-testid="exterior-color"]');
    result.exterior_color = extColorEl ? extColorEl.textContent.trim() : '';
    const intColorEl = document.querySelector('.interior-color, [data-testid="interior-color"]');
    result.interior_color = intColorEl ? intColorEl.textContent.trim() : '';
    const mpgEl = document.querySelector('.mpg, [data-testid="mpg"]');
    result.mpg = mpgEl ? mpgEl.textContent.trim() : '';
    result.features = [];
    document.querySelectorAll('.feature, .vehicle-feature, [data-testid="feature"]').forEach(f => {
        const text = f.textContent.trim();
        if (text) result.features.push(text);
    });
    const dealerEl = document.querySelector('.dealer-info, [data-testid="dealer-info"]');
    if (dealerEl) {
        const nameEl = dealerEl.querySelector('.dealer-name, .name');
        const addrEl = dealerEl.querySelector('.dealer-address, .address');
        const phoneEl = dealerEl.querySelector('.dealer-phone, .phone');
        const ratingEl = dealerEl.querySelector('.rating, .dealer-rating');
        result.dealer_name = nameEl ? nameEl.textContent.trim() : '';
        result.dealer_address = addrEl ? addrEl.textContent.trim() : '';
        result.dealer_phone = phoneEl ? phoneEl.textContent.trim() : '';
        result.dealer_rating_text = ratingEl ? ratingEl.textContent.trim() : '';
    }
    result.price_history = [];
    document.querySelectorAll('.price-history tr, [data-testid="price-history"] tr').forEach(row => {
        const cells = row.querySelectorAll('td');
        if (cells.length >= 2) {
            result.price_history.push({
                date: cells[0] ? cells[0].textContent.trim() : '',
                event: cells[1] ? cells[1].textContent.trim() : '',
                price: cells[2] ? cells[2].textContent.trim() : ''
            });
        }
    });
    const carfaxEl = document.querySelector('a[href*="carfax.com"], .carfax-link');
    result.carfax_link = carfaxEl ? carfaxEl.getAttribute('href') : '';
    const accidentEl = document.querySelector('.accident-badge, .accident-reported, [data-testid="accident-badge"]');
    result.accident_reported = accidentEl ? true : false;
    const ownersEl = document.querySelector('.owners-count, [data-testid="owners-count"]');
    result.owners_text = ownersEl ? ownersEl.textContent.trim() : '';
    const usageEl = document.querySelector('.usage-type, [data-testid="usage-type"]');
    result.usage_type = usageEl ? usageEl.textContent.trim() : '';
    return result;
}
"""


# ─────────────────────────────────────────────
# Payload → response builders
# ─────────────────────────────────────────────

def _common_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Year/make/model/trim/price/mileage subset shared by both builders."""
    return {
        "year": data.get("year"),
        "make": data.get("make"),
        "model": data.get("model"),
        "trim": data.get("trim") or None,
        "price": extract_price(data.get("price", "")) or data.get("price") or None,
        "mileage": extract_number(data.get("mileage_text", "")),
    }


def listing_from_payload(data: Dict[str, Any]) -> CarListing:
    """Build a :class:`CarListing` from one JS-extracted card payload."""
    return CarListing(
        **_common_fields(data),
        location=data.get("location") or None,
        dealer_name=data.get("dealer_name") or None,
        listing_url=data.get("url", ""),
        image_url=data.get("image_url") or None,
        rating=_extract_float(data.get("rating_text", "")),
        accident_reported=data.get("accident_reported"),
    )


def _dealer_review_count(rating_text: str) -> Optional[int]:
    """Pull review count from text like ``"(124 reviews)"`` if present."""
    if not rating_text:
        return None
    count_match = re.search(r'\((\d+)\s*reviews?\)', rating_text, re.IGNORECASE)
    return int(count_match.group(1)) if count_match else None


def _dealer_info_from_payload(data: Dict[str, Any]) -> Optional[DealerInfo]:
    """Build a :class:`DealerInfo` from the JS payload (or ``None``)."""
    if not data.get("dealer_name"):
        return None
    rating_text = data.get("dealer_rating_text", "")
    return DealerInfo(
        name=data.get("dealer_name"),
        address=data.get("dealer_address") or None,
        phone=data.get("dealer_phone") or None,
        rating=_extract_float(rating_text),
        review_count=_dealer_review_count(rating_text),
    )


def _price_history_items(rows: List[Dict[str, Any]]) -> List[PriceHistoryItem]:
    """Map the JS price-history rows onto :class:`PriceHistoryItem`."""
    return [
        PriceHistoryItem(
            date=row.get("date") or None,
            price=row.get("price") or None,
            event=row.get("event") or None,
        )
        for row in rows
    ]


def vehicle_detail_from_payload(data: Dict[str, Any]) -> VehicleDetail:
    """Build a :class:`VehicleDetail` from one JS-extracted detail payload."""
    return VehicleDetail(
        **_common_fields(data),
        vin=data.get("vin") or None,
        engine=data.get("engine") or None,
        transmission=data.get("transmission") or None,
        exterior_color=data.get("exterior_color") or None,
        interior_color=data.get("interior_color") or None,
        mpg=data.get("mpg") or None,
        features=data.get("features", []),
        dealer_info=_dealer_info_from_payload(data),
        price_history=_price_history_items(data.get("price_history", [])),
        carfax_link=data.get("carfax_link") or None,
        accident_reported=data.get("accident_reported"),
        owners_count=extract_number(data.get("owners_text", "")),
        usage_type=data.get("usage_type") or None,
    )
