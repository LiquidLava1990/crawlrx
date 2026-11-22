"""Pydantic models for the Zillow scraper (split out of ``zillow.py``)."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ZillowListing(BaseModel):
    """Single Zillow property listing."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_size: Optional[str] = None
    listing_url: str
    image_url: Optional[str] = None
    status: Optional[str] = None
    days_on_market: Optional[int] = None
    zestimate: Optional[str] = None


class PriceHistoryItem(BaseModel):
    """Single price history entry."""

    date: str
    event: str
    price: Optional[str] = None
    price_per_sqft: Optional[str] = None


class TaxHistoryItem(BaseModel):
    """Single tax history entry."""

    year: int
    taxes: Optional[str] = None
    assessment: Optional[str] = None


class SchoolInfo(BaseModel):
    """Nearby school information."""

    name: str
    rating: Optional[str] = None
    distance: Optional[str] = None
    type: Optional[str] = None


class ZillowPropertyDetail(BaseModel):
    """Detailed Zillow property information."""

    address: str
    price: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_size: Optional[str] = None
    year_built: Optional[int] = None
    zestimate: Optional[str] = None
    price_history: List[PriceHistoryItem] = Field(default_factory=list)
    tax_history: List[TaxHistoryItem] = Field(default_factory=list)
    nearby_schools: List[SchoolInfo] = Field(default_factory=list)
    listing_agent: Optional[Dict[str, str]] = None


class ZillowSearchRequest(BaseModel):
    """Request model for POST /scrape/zillow/search."""

    location: str = Field(..., description="Location to search")
    listing_type: str = Field(default="for_sale")
    min_price: Optional[int] = Field(default=None)
    max_price: Optional[int] = Field(default=None)
    min_beds: Optional[int] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=40)


class ZillowSearchResponse(BaseModel):
    """Response model for POST /scrape/zillow/search."""

    success: bool
    listings: List[ZillowListing] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class ZillowPropertyRequest(BaseModel):
    """Request model for POST /scrape/zillow/property."""

    url: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)


class ZillowPropertyResponse(BaseModel):
    """Response model for POST /scrape/zillow/property."""

    success: bool
    property: Optional[ZillowPropertyDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


class ZillowComp(BaseModel):
    """Comparable property sale."""

    address: str
    sale_price: Optional[str] = None
    sale_date: Optional[str] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    price_per_sqft: Optional[str] = None
    distance_miles: Optional[float] = None
    url: str


class ZillowCompsRequest(BaseModel):
    """Request model for POST /scrape/zillow/comps."""

    address: str = Field(..., description="Subject property address")
    zip_code: str = Field(..., description="ZIP code for search area")
    radius_miles: float = Field(default=0.5)
    limit: int = Field(default=8, ge=1, le=20)


class ZillowCompsResponse(BaseModel):
    """Response model for POST /scrape/zillow/comps."""

    success: bool
    subject_zestimate: Optional[str] = None
    comps: List[ZillowComp] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0
