"""Pydantic models for the Target scraper (split out of target.py)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TargetFulfillmentOptions(BaseModel):
    """Fulfillment options for a Target product."""

    shipping: bool = False
    pickup: bool = False
    delivery: bool = False
    same_day_delivery: bool = False


class TargetProduct(BaseModel):
    """Single Target product data."""

    title: str
    price: Optional[str] = None
    original_price: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    product_url: str
    image_url: Optional[str] = None
    tcin: str
    availability: Optional[str] = None
    brand: Optional[str] = None
    fulfillment_options: TargetFulfillmentOptions = Field(default_factory=TargetFulfillmentOptions)


class TargetReview(BaseModel):
    """Single product review."""

    title: Optional[str] = None
    rating: Optional[int] = None
    text: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    verified_purchase: bool = False
    helpful_count: Optional[int] = None


class TargetProductDetail(BaseModel):
    """Detailed Target product data."""

    title: str
    price: Optional[str] = None
    original_price: Optional[str] = None
    description: Optional[str] = None
    specs: Dict[str, str] = Field(default_factory=dict)
    images: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    rating_breakdown: Dict[str, int] = Field(default_factory=dict)
    review_count: Optional[int] = None
    top_reviews: List[TargetReview] = Field(default_factory=list)
    availability: Optional[str] = None
    fulfillment_options: TargetFulfillmentOptions = Field(default_factory=TargetFulfillmentOptions)
    brand: Optional[str] = None
    category: Optional[str] = None
    tcin: str
    model_number: Optional[str] = None
    upc: Optional[str] = None
    related_products: List[Dict[str, Any]] = Field(default_factory=list)
    store_availability: Dict[str, Any] = Field(default_factory=dict)


class TargetSearchRequest(BaseModel):
    """Request model for POST /scrape/target/search."""

    query: str = Field(..., description="Search query")
    min_price: Optional[float] = Field(default=None, description="Minimum price filter")
    max_price: Optional[float] = Field(default=None, description="Maximum price filter")
    sort: str = Field(default="relevance", description="Sort token")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results")


class TargetSearchResponse(BaseModel):
    """Response model for POST /scrape/target/search."""

    success: bool
    products: List[TargetProduct] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class TargetProductRequest(BaseModel):
    """Request model for POST /scrape/target/product."""

    url: Optional[str] = Field(default=None, description="Target product URL")
    tcin: Optional[str] = Field(default=None, description="Target item ID (TCIN)")


class TargetProductResponse(BaseModel):
    """Response model for POST /scrape/target/product."""

    success: bool
    product: Optional[TargetProductDetail] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


__all__ = (
    "TargetFulfillmentOptions",
    "TargetProduct",
    "TargetProductDetail",
    "TargetProductRequest",
    "TargetProductResponse",
    "TargetReview",
    "TargetSearchRequest",
    "TargetSearchResponse",
)
