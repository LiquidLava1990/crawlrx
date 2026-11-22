"""Compat re-export: ``sentinel_errors`` shim lives in :mod:`._sentinel_shim`.

The ``_stealth_browser_*`` siblings historically imported ``ErrCode`` /
``emit`` from this module; the single shared implementation now sits in
``_sentinel_shim.py``, so this file only preserves that import path.
"""
from __future__ import annotations

from ._sentinel_shim import ErrCode, emit

__all__ = ["ErrCode", "emit"]
