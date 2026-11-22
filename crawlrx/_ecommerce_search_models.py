"""Pydantic request/response models for the ecommerce-search pipeline.

Extracted into its own sibling module so the source adapters and the
runner can construct ``ProductResult`` without importing the public
orchestrator (which would create an import cycle).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class EcommerceSearchRequest(BaseModel):
    query: str = Field(..., description='Product to search for')
    num_results: int = Field(default=20, ge=1, le=100)
    platforms: Optional[List[str]] = Field(
        default=None,
        description='Filter to specific platforms e.g. ["shopify", "woocommerce"]',
    )
    max_price: Optional[float] = None
    min_price: Optional[float] = None


class ProductResult(BaseModel):
    title: str
    price: Optional[float] = None
    price_original: Optional[float] = None
    currency: str = 'USD'
    url: str
    store_url: str
    platform: str
    image: Optional[str] = None
    in_stock: bool = True
    discount_pct: Optional[float] = None


class EcommerceSearchResponse(BaseModel):
    success: bool
    query: str
    results: List[ProductResult]
    total: int
    platforms_searched: List[str]
    elapsed_ms: int
    error: Optional[str] = None
