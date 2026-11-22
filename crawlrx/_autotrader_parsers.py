"""Parsers + JS extraction snippets for autotrader.py.

Split from ``autotrader.py`` to keep that module under the 500-line ceiling.
The two big JS strings live here so the public scraper file stays scannable.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class AutoTraderListing(BaseModel):
    """Single AutoTrader vehicle listing."""

    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    price: Optional[str] = None
    mileage: Optional[str] = None
    location: Optional[str] = None
    dealer_name: Optional[str] = None
    listing_url: str
    image_url: Optional[str] = None
    condition: Optional[str] = None
    distance_miles: Optional[str] = None


class DealerInfo(BaseModel):
    """Dealer information."""

    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    rating: Optional[str] = None


class AutoTraderVehicleDetail(BaseModel):
    """Detailed AutoTrader vehicle information."""

    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    price: Optional[str] = None
    mileage: Optional[str] = None
    vin: Optional[str] = None
    engine: Optional[str] = None
    transmission: Optional[str] = None
    color: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    dealer_info: Optional[DealerInfo] = None
    accident_history: Optional[str] = None
    listing_photos: List[str] = Field(default_factory=list)


_COMMON_MAKES = (
    "Toyota", "Honda", "Ford", "Chevrolet", "Chevy", "BMW", "Mercedes-Benz",
    "Mercedes", "Audi", "Lexus", "Nissan", "Hyundai", "Kia", "Volkswagen",
    "VW", "Subaru", "Mazda", "Jeep", "Ram", "GMC", "Cadillac", "Buick",
    "Chrysler", "Dodge", "Volvo", "Acura", "Infiniti", "Lincoln", "Genesis",
    "Tesla", "Porsche", "Jaguar", "Land Rover", "Mini", "Fiat", "Mitsubishi",
)


def extract_year_make_model(
    text: str,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Extract year + best-guess make from a listing title (model unresolved)."""
    year: Optional[int] = None
    year_match = re.search(r'\b(19|20)\d{2}\b', text)
    if year_match:
        year = int(year_match.group(0))
    make: Optional[str] = None
    text_lower = text.lower()
    for candidate in _COMMON_MAKES:
        if candidate.lower() in text_lower:
            make = candidate
            break
    return year, make, None


# ── JS extraction snippets (kept verbatim to preserve scraping behaviour) ──

LISTING_EXTRACT_JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-testid="vehicle-card"], .vehicle-card, .listing-item, [data-cmp="inventoryListing"]');
    cards.forEach(card => {
        const titleEl = card.querySelector('[data-testid="vehicle-card-title"], .vehicle-card-title, h3 a, .title');
        const title = titleEl ? titleEl.textContent.trim() : '';
        const priceEl = card.querySelector('[data-testid="vehicle-card-price"], .vehicle-card-price, .price, [data-cmp="price"]');
        const price = priceEl ? priceEl.textContent.trim() : '';
        const mileageEl = card.querySelector('[data-testid="vehicle-card-mileage"], .vehicle-card-mileage, .mileage, [data-cmp="mileage"]');
        const mileage = mileageEl ? mileageEl.textContent.trim() : '';
        const locationEl = card.querySelector('[data-testid="vehicle-card-location"], .vehicle-card-location, .location, [data-cmp="distance"]');
        const location = locationEl ? locationEl.textContent.trim() : '';
        const dealerEl = card.querySelector('[data-testid="vehicle-card-dealer"], .vehicle-card-dealer, .dealer-name, [data-cmp="sellerName"]');
        const dealer = dealerEl ? dealerEl.textContent.trim() : '';
        const linkEl = card.querySelector('a[href*="/cars-for-sale/"], a[data-cmp="link"]');
        const href = linkEl ? linkEl.getAttribute('href') : '';
        const imgEl = card.querySelector('img[data-testid="vehicle-image"], img');
        const image = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src')) : '';
        const conditionEl = card.querySelector('[data-testid="vehicle-card-condition"], .condition-badge, [data-cmp="listingType"]');
        const condition = conditionEl ? conditionEl.textContent.trim() : '';
        const distEl = card.querySelector('[data-testid="distance"], .distance');
        const distance = distEl ? distEl.textContent.trim() : '';
        if (title && price) {
            results.push({title, price, mileage, location, dealer_name: dealer,
                url: href.startsWith('http') ? href : 'https://www.autotrader.com' + href,
                image_url: image, condition, distance_miles: distance});
        }
    });
    return results;
}
"""


VEHICLE_EXTRACT_JS = """
() => {
    const result = {};
    const titleEl = document.querySelector('h1, [data-testid="vehicle-title"]');
    result.title = titleEl ? titleEl.textContent.trim() : '';
    const priceEl = document.querySelector('[data-testid="vehicle-price"], .vehicle-price, .price, [data-cmp="price"]');
    result.price = priceEl ? priceEl.textContent.trim() : '';
    const mileageEl = document.querySelector('[data-testid="vehicle-mileage"], .vehicle-mileage, .mileage, [data-cmp="mileage"]');
    result.mileage = mileageEl ? mileageEl.textContent.trim() : '';
    const vinEl = document.querySelector('[data-testid="vin"], [data-cmp="vin"], .vin');
    if (!vinEl) {
        const specs = document.querySelectorAll('[data-testid="spec-row"], .spec-row, .specification-row');
        specs.forEach(row => {
            const label = row.querySelector('[data-testid="spec-label"], .spec-label');
            if (label && label.textContent.toLowerCase().includes('vin')) {
                const value = row.querySelector('[data-testid="spec-value"], .spec-value');
                if (value) result.vin = value.textContent.trim();
            }
        });
    } else {
        result.vin = vinEl.textContent.trim();
    }
    const engineEl = document.querySelector('[data-testid="engine"], [data-cmp="engine"], .engine');
    result.engine = engineEl ? engineEl.textContent.trim() : '';
    const transEl = document.querySelector('[data-testid="transmission"], [data-cmp="transmission"], .transmission');
    result.transmission = transEl ? transEl.textContent.trim() : '';
    const colorEl = document.querySelector('[data-testid="exterior-color"], [data-cmp="exteriorColor"], .exterior-color');
    result.color = colorEl ? colorEl.textContent.trim() : '';
    result.features = [];
    document.querySelectorAll('[data-testid="feature"], [data-cmp="feature"], .feature-list li').forEach(el => {
        const text = el.textContent.trim();
        if (text) result.features.push(text);
    });
    const dealerEl = document.querySelector('[data-testid="dealer-info"], [data-cmp="sellerInfo"], .dealer-info');
    if (dealerEl) {
        const dealerName = dealerEl.querySelector('[data-testid="dealer-name"], .dealer-name, h3, h4');
        const dealerAddr = dealerEl.querySelector('[data-testid="dealer-address"], .dealer-address, address');
        const dealerPhone = dealerEl.querySelector('[data-testid="dealer-phone"], .dealer-phone, a[href^="tel:"]');
        const dealerRating = dealerEl.querySelector('[data-testid="dealer-rating"], .dealer-rating, .rating');
        result.dealer_info = {
            name: dealerName ? dealerName.textContent.trim() : '',
            address: dealerAddr ? dealerAddr.textContent.trim() : '',
            phone: dealerPhone ? dealerPhone.textContent.trim() : '',
            rating: dealerRating ? dealerRating.textContent.trim() : ''
        };
    }
    const accidentEl = document.querySelector('[data-testid="accident-history"], [data-cmp="accidentHistory"], .accident-history');
    result.accident_history = accidentEl ? accidentEl.textContent.trim() : '';
    result.photos = [];
    document.querySelectorAll('[data-testid="vehicle-photo"] img, .vehicle-photo img, .carousel img, [data-cmp="mediaViewer"] img').forEach(img => {
        const src = img.getAttribute('src') || img.getAttribute('data-src');
        if (src && !src.includes('placeholder')) result.photos.push(src);
    });
    document.querySelectorAll('[data-testid="specs-table"] tr, .specs-table tr, .specification-table tr').forEach(row => {
        const labelEl = row.querySelector('td:first-child, th');
        const valueEl = row.querySelector('td:last-child');
        if (labelEl && valueEl) {
            const label = labelEl.textContent.toLowerCase().trim();
            const value = valueEl.textContent.trim();
            if (label.includes('year') && !result.year) result.year = value;
            if (label.includes('make') && !result.make) result.make = value;
            if (label.includes('model') && !result.model) result.model = value;
            if (label.includes('trim') && !result.trim) result.trim = value;
            if (label.includes('vin') && !result.vin) result.vin = value;
            if (label.includes('engine') && !result.engine) result.engine = value;
            if (label.includes('transmission') && !result.transmission) result.transmission = value;
            if (label.includes('color') && !result.color) result.color = value;
        }
    });
    return result;
}
"""


def _listing_trim(title: str, year: Optional[int], make: Optional[str]) -> Optional[str]:
    """Trim text remaining after stripping ``year`` + ``make`` from ``title``."""
    if not (year and make):
        return None
    remainder = title.replace(str(year), "").replace(make, "").strip()
    return remainder or None


def listing_from_card(data: Dict[str, Any]) -> AutoTraderListing:
    """Build an :class:`AutoTraderListing` from one JS-extracted card payload."""
    title = data.get("title", "")
    year, make, model = extract_year_make_model(title)
    return AutoTraderListing(
        year=year,
        make=make,
        model=model,
        trim=_listing_trim(title, year, make),
        price=data.get("price"),
        mileage=data.get("mileage"),
        location=data.get("location"),
        dealer_name=data.get("dealer_name"),
        listing_url=data.get("url", ""),
        image_url=data.get("image_url"),
        condition=data.get("condition"),
        distance_miles=data.get("distance_miles"),
    )


def _dealer_from_data(dealer_data: Optional[Dict[str, Any]]) -> Optional[DealerInfo]:
    """Coerce the JS dealer payload into a :class:`DealerInfo`."""
    if not dealer_data or not dealer_data.get("name"):
        return None
    return DealerInfo(
        name=dealer_data.get("name", ""),
        address=dealer_data.get("address") or None,
        phone=dealer_data.get("phone") or None,
        rating=dealer_data.get("rating") or None,
    )


def vehicle_detail_from_payload(data: Dict[str, Any]) -> AutoTraderVehicleDetail:
    """Build the response model from one JS-extracted payload."""
    year, make, model = extract_year_make_model(data.get("title", ""))
    return AutoTraderVehicleDetail(
        year=year,
        make=make,
        model=model,
        trim=data.get("trim") or None,
        price=data.get("price") or None,
        mileage=data.get("mileage") or None,
        vin=data.get("vin") or None,
        engine=data.get("engine") or None,
        transmission=data.get("transmission") or None,
        color=data.get("color") or None,
        features=data.get("features", []),
        dealer_info=_dealer_from_data(data.get("dealer_info")),
        accident_history=data.get("accident_history") or None,
        listing_photos=data.get("photos", []),
    )
