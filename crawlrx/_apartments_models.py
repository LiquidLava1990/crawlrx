"""Pydantic models for the Apartments.com scraper (split out of
``apartments.py`` to keep the module under the 500 LOC ceiling)."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ApartmentListing(BaseModel):
    """Single apartment listing from search."""

    name: str = Field(...)
    address: Optional[str] = None
    price_range: Optional[str] = None
    beds_range: Optional[str] = None
    baths_range: Optional[str] = None
    sqft_range: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    listing_url: str = Field(...)
    image_url: Optional[str] = None
    amenities_summary: Optional[str] = None
    available_now: Optional[bool] = None
    pet_policy: Optional[str] = None


class FloorPlan(BaseModel):
    """Floor plan details."""

    name: Optional[str] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    price: Optional[str] = None


class Review(BaseModel):
    """Property review."""

    author: Optional[str] = None
    rating: Optional[float] = None
    text: Optional[str] = None
    date: Optional[str] = None


class ApartmentDetail(BaseModel):
    """Detailed apartment-complex information."""

    name: str = Field(...)
    address: Optional[str] = None
    price_range: Optional[str] = None
    floor_plans: List[FloorPlan] = Field(default_factory=list)
    amenities: List[str] = Field(default_factory=list)
    pet_policy: Optional[str] = None
    parking: Optional[str] = None
    utilities_included: Optional[str] = None
    lease_terms: Optional[str] = None
    contact_info: Optional[Dict[str, str]] = None
    photos_count: Optional[int] = None
    reviews: List[Review] = Field(default_factory=list)
    rating: Optional[float] = None
    review_count: Optional[int] = None
    website: Optional[str] = None
    phone: Optional[str] = None


class ApartmentsSearchRequest(BaseModel):
    """Request model for ``POST /scrape/apartments/search``."""

    location: str = Field(..., description="City, state, or ZIP code")
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    pet_friendly: Optional[bool] = None
    limit: int = Field(default=20, ge=1, le=50)


class ApartmentsSearchResponse(BaseModel):
    """Response model for ``POST /scrape/apartments/search``."""

    success: bool
    listings: List[ApartmentListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class ApartmentsListingRequest(BaseModel):
    """Request model for ``POST /scrape/apartments/listing``."""

    url: str = Field(..., description="Full Apartments.com listing URL")


class ApartmentsListingResponse(BaseModel):
    """Response model for ``POST /scrape/apartments/listing``."""

    success: bool
    property: Optional[ApartmentDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
