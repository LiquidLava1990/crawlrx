"""Shared request-spec dataclass for TLS solver engines.

Bundles the 6+ parameters that ``_execute_*`` request methods used to take
into a single value object so cc.shape.param_count stays under the
threshold (5) without hiding intent in **kwargs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RequestSpec:
    """One request: url + method + body + transport policy."""
    url: str
    method: str = "GET"
    headers: Dict[str, str] = field(default_factory=dict)
    data: Optional[Any] = None
    json_data: Optional[Dict[str, Any]] = None
    proxy: Optional[str] = None
    follow_redirects: bool = True
    fingerprint: Optional[str] = None  # tls-client profile name
    browser: Optional[str] = None      # hrequests browser name


__all__ = ["RequestSpec"]
