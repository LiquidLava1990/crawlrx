"""_cms_helpers.py -- Shared helpers for CMS / e-commerce platform scrapers.

Absorbs patterns common across woocommerce.py, webflow.py, squarespace_scraper.py,
ghost_scraper.py, duda_scraper.py, ecwid_scraper.py, godaddy_scraper.py,
weebly_scraper.py, wix_scraper.py, joomla_scraper.py, jimdo_scraper.py,
knoji_scraper.py, kajabi_scraper.py, clickfunnels_scraper.py, magento_scraper.py,
nopcommerce_scraper.py, volusion_scraper.py, cscart_scraper.py, hybris_scraper.py,
drupal_scraper.py.

All exception tuples here are narrow (no `Exception` catches) to satisfy
`the no-broad-except rule`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping, Optional

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx._cms_helpers")


# Standard browser-style headers used by every CMS detector.
CMS_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# `requests` library failure tuple -- narrow alternative to `except Exception`.
def requests_exception_types() -> tuple[type[BaseException], ...]:
    """Return the tuple of `requests` + transport-layer failure classes."""
    base: list[type[BaseException]] = [
        OSError,
        ConnectionError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ]
    try:
        import requests as _rq  # type: ignore[import-untyped] because optional dep; no PEP 561 stubs ship with requests
        base.extend([
            _rq.RequestException,
            _rq.Timeout,
            _rq.ConnectionError,
            _rq.HTTPError,
        ])
    except ImportError:
        pass
    return tuple(base)


REQUESTS_EXCEPTIONS: tuple[type[BaseException], ...] = requests_exception_types()


# Combined tuple covering every transport / parse failure a `requests` based
# CMS scraper can hit. Saves per-file `except (...)` duplication.
HTTP_SCRAPE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    REQUESTS_EXCEPTIONS + (KeyError, TypeError, AttributeError)
)


# Tuple for Playwright-driven CMS scrapers (page-level failures).
CMS_PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + SCRAPE_FAILURE_EXCEPTIONS
)


# Per-card / per-item extraction failure tuple.
CARD_EXTRACTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    SELECTOR_EXTRACTION_EXCEPTIONS
)


def normalize_base_url(url: str) -> str:
    """Strip whitespace + trailing slash."""
    return url.strip().rstrip("/")


def http_get_text(url: str, timeout: int = 8) -> Optional[str]:
    """GET a URL, return text on 200, None on any narrow transport failure."""
    try:
        import requests
        resp = requests.get(
            url,
            headers={"User-Agent": CMS_HEADERS["User-Agent"]},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("http_get_text(%s) suppressed %s: %s", url, type(exc).__name__, exc)
    except ImportError:
        return None
    return None


def http_get_json(
    url: str,
    timeout: int = 8,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Optional[Any]:
    """GET a URL, return parsed JSON or None on any narrow transport failure."""
    try:
        import requests
        resp = requests.get(
            url,
            headers=dict(headers) if headers else CMS_HEADERS,
            params=dict(params) if params else None,
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.json()
    except REQUESTS_EXCEPTIONS as exc:
        logger.debug("http_get_json(%s) suppressed %s: %s", url, type(exc).__name__, exc)
    except ImportError:
        return None
    return None


def text_contains_any(text: str, needles: Iterable[str]) -> bool:
    """True if any needle appears in text (case-sensitive)."""
    return any(n in text for n in needles)


def text_contains_any_ci(text: str, needles: Iterable[str]) -> bool:
    """True if any needle appears in text (case-insensitive)."""
    haystack = text.lower()
    return any(n.lower() in haystack for n in needles)


def detect_platform_by_text(
    base_url: str,
    needles: Iterable[str],
    *,
    timeout: int = 8,
    case_insensitive: bool = False,
) -> bool:
    """True if any needle is present in `GET base_url`'s response body."""
    text = http_get_text(base_url, timeout=timeout)
    if not text:
        return False
    if case_insensitive:
        return text_contains_any_ci(text, needles)
    return text_contains_any(text, needles)


def safe_inner_text(value: object) -> str:
    """Best-effort `.strip()` on a string-like, never raises."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def first_image_src(images: object) -> Optional[str]:
    """Pull first valid image URL from a Shopify/WC/Ecwid-style images list."""
    if not images:
        return None
    if isinstance(images, list):
        first = images[0]
    else:
        first = images
    if isinstance(first, dict):
        for key in ("src", "url", "source_url", "thumbnail", "image", "href"):
            value = first.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    if isinstance(first, str):
        return first
    return None
