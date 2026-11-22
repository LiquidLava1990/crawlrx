"""facebook.py -- Facebook Marketplace scraper endpoints.

Endpoints
---------
POST /scrape/facebook/marketplace -- search Facebook Marketplace
POST /scrape/facebook/listing     -- scrape a single Facebook Marketplace listing

Implementation lives in sibling modules:
- :mod:`._facebook_extractors` -- per-field DOM probes
- :mod:`._facebook_pages`      -- page orchestrators (search / listing)
- :mod:`._facebook_models`     -- ``MarketplaceQuery`` arg bundle

This module owns only the public surface required by the snapshot tests
(pydantic models, ``router``, and the two endpoint coroutines).
Uses Playwright CDP -- requires a logged-in Facebook session.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import _facebook_pages as _pages
from ._facebook_models import MarketplaceQuery

logger = logging.getLogger("crawlrx.facebook")
router = APIRouter()


# ─────────────────────────────────────────────
# Pydantic Models (must remain in this module: signature snapshot
# filters by ``__module__`` and would drop re-exports.)
# ─────────────────────────────────────────────


class FacebookMarketplaceRequest(BaseModel):
    """Request model for POST /scrape/facebook/marketplace."""

    query: str = Field(..., description="Search query for Facebook Marketplace")
    location: Optional[str] = Field(
        default=None,
        description="Location to search near (e.g., 'Louisville, KY')",
    )
    max_price: Optional[int] = Field(
        default=None, description="Maximum price filter"
    )
    min_price: Optional[int] = Field(
        default=None, description="Minimum price filter"
    )
    limit: int = Field(
        default=20, ge=1, le=50, description="Maximum listings to return"
    )
    timeout: int = Field(
        default=45000, ge=5000, le=120000,
        description="Timeout in milliseconds",
    )


class FacebookListingItem(BaseModel):
    """Single Facebook Marketplace listing."""

    title: Optional[str] = Field(default=None, description="Listing title")
    price: Optional[str] = Field(
        default=None, description="Listing price (as displayed)"
    )
    location: Optional[str] = Field(
        default=None, description="Item location"
    )
    condition: Optional[str] = Field(
        default=None, description="Item condition (New, Used, etc.)"
    )
    seller_name: Optional[str] = Field(
        default=None, description="Seller display name"
    )
    listing_url: Optional[str] = Field(
        default=None, description="Full listing URL"
    )
    image_url: Optional[str] = Field(
        default=None, description="Primary image URL"
    )
    posted_date: Optional[str] = Field(
        default=None, description="When listed (relative text)"
    )
    description: Optional[str] = Field(
        default=None, description="Brief description/preview"
    )


class FacebookMarketplaceResponse(BaseModel):
    """Response model for POST /scrape/facebook/marketplace."""

    success: bool = Field(..., description="Whether scrape succeeded")
    listings: List[FacebookListingItem] = Field(
        default_factory=list, description="List of marketplace listings"
    )
    count: int = Field(default=0, description="Number of listings returned")
    query: Optional[str] = Field(
        default=None, description="Original search query"
    )
    location: Optional[str] = Field(
        default=None, description="Location searched"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if failed"
    )
    elapsed_ms: int = Field(
        default=0, description="Time taken in milliseconds"
    )


class FacebookListingRequest(BaseModel):
    """Request model for POST /scrape/facebook/listing."""

    url: str = Field(..., description="Full Facebook Marketplace listing URL")
    timeout: int = Field(
        default=45000, ge=5000, le=120000,
        description="Timeout in milliseconds",
    )


class FacebookListingDetail(BaseModel):
    """Detailed Facebook Marketplace listing."""

    title: Optional[str] = Field(default=None, description="Listing title")
    price: Optional[str] = Field(default=None, description="Listing price")
    description: Optional[str] = Field(
        default=None, description="Full item description"
    )
    seller_name: Optional[str] = Field(
        default=None, description="Seller display name"
    )
    seller_url: Optional[str] = Field(
        default=None, description="Seller profile URL"
    )
    location: Optional[str] = Field(
        default=None, description="Item location"
    )
    condition: Optional[str] = Field(
        default=None, description="Item condition"
    )
    category: Optional[str] = Field(
        default=None, description="Item category"
    )
    images: List[str] = Field(
        default_factory=list, description="List of image URLs"
    )
    posted_date: Optional[str] = Field(
        default=None, description="When listed"
    )
    listing_id: Optional[str] = Field(
        default=None, description="Facebook listing ID"
    )


class FacebookListingResponse(BaseModel):
    """Response model for POST /scrape/facebook/listing."""

    success: bool = Field(..., description="Whether scrape succeeded")
    listing: Optional[FacebookListingDetail] = Field(
        default=None, description="Listing details"
    )
    url: Optional[str] = Field(
        default=None, description="Original listing URL"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if failed"
    )
    elapsed_ms: int = Field(
        default=0, description="Time taken in milliseconds"
    )


# ─────────────────────────────────────────────
# Endpoint helpers.
# ─────────────────────────────────────────────


def _outer_timeout_seconds(request_timeout_ms: int) -> int:
    """Outer asyncio timeout: scrape budget plus 15s grace, floor 45s."""
    return max(45, request_timeout_ms // 1000 + 15)


def _failed_marketplace(
    request: FacebookMarketplaceRequest,
    error_message: str,
    elapsed_ms: int,
) -> FacebookMarketplaceResponse:
    return FacebookMarketplaceResponse(
        success=False,
        listings=[],
        count=0,
        query=request.query,
        location=request.location,
        error=error_message,
        elapsed_ms=elapsed_ms,
    )


def _failed_listing(
    request: FacebookListingRequest,
    error_message: str,
    elapsed_ms: int,
) -> FacebookListingResponse:
    return FacebookListingResponse(
        success=False, url=request.url, error=error_message, elapsed_ms=elapsed_ms,
    )


# ─────────────────────────────────────────────
# API Endpoints.
# ─────────────────────────────────────────────


@router.post(
    "/scrape/facebook/marketplace",
    response_model=FacebookMarketplaceResponse,
    summary="Search Facebook Marketplace",
)
async def scrape_facebook_marketplace(
    request: FacebookMarketplaceRequest,
) -> FacebookMarketplaceResponse:
    """Search Facebook Marketplace for listings."""
    timeout_seconds = _outer_timeout_seconds(request.timeout)
    query = MarketplaceQuery(
        query=request.query,
        location=request.location,
        max_price=request.max_price,
        min_price=request.min_price,
        limit=request.limit,
        timeout_ms=request.timeout,
    )
    try:
        return await asyncio.wait_for(
            _pages.scrape_marketplace(query),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        logger.error("[Facebook] Marketplace search timed out: %s", exc)
        return _failed_marketplace(
            request, f"Request timed out after {timeout_seconds}s",
            timeout_seconds * 1000,
        )


@router.post(
    "/scrape/facebook/listing",
    response_model=FacebookListingResponse,
    summary="Scrape Facebook Marketplace listing",
)
async def scrape_facebook_listing(
    request: FacebookListingRequest,
) -> FacebookListingResponse:
    """Scrape a single Facebook Marketplace listing page."""
    timeout_seconds = _outer_timeout_seconds(request.timeout)
    try:
        return await asyncio.wait_for(
            _pages.scrape_listing(request.url, request.timeout),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        logger.error("[Facebook] Listing scrape timed out: %s", exc)
        return _failed_listing(
            request, f"Request timed out after {timeout_seconds}s",
            timeout_seconds * 1000,
        )


# ─────────────────────────────────────────────
# Backwards-compat shim for any callers reaching for the private names.
# ─────────────────────────────────────────────


__all__ = [
    "FacebookListingDetail",
    "FacebookListingItem",
    "FacebookListingRequest",
    "FacebookListingResponse",
    "FacebookMarketplaceRequest",
    "FacebookMarketplaceResponse",
    "router",
    "scrape_facebook_listing",
    "scrape_facebook_marketplace",
]
