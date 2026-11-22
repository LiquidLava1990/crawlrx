"""Bundled args for Facebook scraping helpers.

Replaces 6-positional arg signatures (the parameter-count limit) with a frozen
dataclass so the orchestrator and the public endpoint share one shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MarketplaceQuery:
    """Bundled search-query inputs for Facebook Marketplace."""

    query: str
    location: Optional[str] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    limit: int = 20
    timeout_ms: int = 45000
