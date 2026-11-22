"""Shared helpers for crawlrx scrapers (shared deduplication target).

This module exists primarily to absorb code that the Tier-0 linter flags as
`duplication checks` across the scraper family. New helpers are appended
here as more duplication is removed.

Conventions:
- Functions are pure / side-effect-free unless their name signals otherwise.
- Type hints on every signature.
- Specific exception types documented at every call boundary.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

logger = logging.getLogger("crawlrx._scraper_helpers")


# ---------------------------------------------------------------------------
# Narrow exception families (re-exported as tuples for `except (...)` blocks)
# ---------------------------------------------------------------------------

# Network-layer failures (httpx / aiohttp / primp / urllib / sockets).
# Imports are lazy because some products ship without these libraries installed.
def network_exception_types() -> tuple[type[BaseException], ...]:
    """Network-layer exceptions to catch in scrapers (httpx/aiohttp/primp lazy)."""
    types: list[type[BaseException]] = [OSError, ConnectionError, TimeoutError]
    try:
        import httpx
        types.extend([httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError])
    except ImportError:
        pass
    try:
        import aiohttp
        types.append(aiohttp.ClientError)
    except ImportError:
        pass
    try:
        # primp's Rust exceptions subclass PrimpError, not OSError — without
        # explicit registration a primp.TimeoutError escapes every fetch tier.
        import primp
        types.append(primp.PrimpError)
    except ImportError:
        pass
    return tuple(types)


# JSON / parsing failures common across scraper response handlers.
PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    json.JSONDecodeError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    UnicodeDecodeError,
)


def playwright_exception_types() -> tuple[type[BaseException], ...]:
    """Return the tuple of Playwright + asyncio failure types."""
    import asyncio
    types: list[type[BaseException]] = [asyncio.TimeoutError, OSError, RuntimeError]
    try:
        from playwright.async_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
        )
        types.extend([PlaywrightError, PlaywrightTimeoutError])
    except ImportError:
        pass
    return tuple(types)


SCRAPE_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
    json.JSONDecodeError,
    UnicodeDecodeError,
)


# Playwright + asyncio failure tuple resolved at import time. If playwright
# isn't installed the symbols collapse to a sentinel that can't actually
# fire (asyncio.TimeoutError + OSError + RuntimeError still match the
# realistic surface for a Playwright-driven scraper running without the
# library; the swallow verdict is preserved).
def _resolve_playwright_exception_tuple() -> tuple[type[BaseException], ...]:
    import asyncio
    base: list[type[BaseException]] = [asyncio.TimeoutError, OSError, RuntimeError]
    try:
        from playwright.async_api import (
            Error as _PlaywrightError,
            TimeoutError as _PlaywrightTimeoutError,
        )
        base.extend([_PlaywrightError, _PlaywrightTimeoutError])
    except ImportError:
        pass
    return tuple(base)


PLAYWRIGHT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    _resolve_playwright_exception_tuple()
)


# Combined tuple used by browser-driven scrapers where either the network
# transport, Playwright, or response-parsing can raise. Saves cross-file
# duplication of the same `except (...)` list.
BROWSER_SCRAPE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + SCRAPE_FAILURE_EXCEPTIONS
)


# Selector-extraction context: a card / element extraction inside a loop
# can fail because the DOM didn't match expected shape, attribute access
# returned None, or a substring conversion failed. None of these are
# Playwright `Error`s -- those bubble out so the outer browser-level
# handler sees them.
SELECTOR_EXTRACTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
    IndexError,
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    """Return `scheme://netloc` for a URL, lowercase netloc."""
    parsed = urlparse(url)
    netloc = (parsed.netloc or "").lower()
    return f"{parsed.scheme}://{netloc}"


def same_domain(left: str, right: str) -> bool:
    """True if two URLs share scheme+netloc (case-insensitive)."""
    a = urlparse(left)
    b = urlparse(right)
    return (a.scheme, (a.netloc or "").lower()) == (
        b.scheme,
        (b.netloc or "").lower(),
    )


# ---------------------------------------------------------------------------
# Logging helpers (R-05 secret hygiene compliant)
# ---------------------------------------------------------------------------

def safe_value_summary(value: object) -> str:
    """R-05 safe summary: `<type:len>` instead of repr of untrusted data."""
    type_name = type(value).__name__
    try:
        length = len(value)  # type: ignore[arg-type] because value is Any; non-Sized values raise TypeError handled below
    except TypeError:
        return f"<{type_name}>"
    return f"<{type_name}:{length}>"


def log_and_swallow(
    log: logging.Logger,
    level: int,
    where: str,
    exc: BaseException,
) -> None:
    """Standardised log line for an intentionally-swallowed exception."""
    log.log(level, "%s failed (%s): %s", where, type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Dict / mapping helpers
# ---------------------------------------------------------------------------

def first_present(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any | None:
    """Return the first value in `mapping` whose key is in `keys` (in order)."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def coerce_str(value: object, default: str = "") -> str:
    """Best-effort string coercion that never raises."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except (TypeError, ValueError):
        return default


def _coerce_int_from_float(value: float, default: int) -> int:
    """Convert a float to int, falling back to ``default`` on overflow / NaN."""
    try:
        return int(value)
    except (OverflowError, ValueError):
        return default


def _coerce_int_from_str(value: str, default: int) -> int:
    """Parse an int from a string, accepting numeric float strings as well."""
    stripped = value.strip()
    if not stripped:
        return default
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return int(float(stripped))
    except (ValueError, OverflowError):
        return default


def coerce_int(value: object, default: int = 0) -> int:
    """Best-effort int coercion that never raises."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _coerce_int_from_float(value, default)
    if isinstance(value, str):
        return _coerce_int_from_str(value, default)
    return default


def _coerce_float_from_str(value: str, default: float) -> float:
    """Parse a float from a string, returning ``default`` on empty / invalid."""
    stripped = value.strip()
    if not stripped:
        return default
    try:
        return float(stripped)
    except ValueError:
        return default


def coerce_float(value: object, default: float = 0.0) -> float:
    """Best-effort float coercion that never raises."""
    if value is None or isinstance(value, (bytes, bytearray)):
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _coerce_float_from_str(value, default)
    return default


# ---------------------------------------------------------------------------
# Shared helpers for the T-Z scrapers
# ---------------------------------------------------------------------------

import re as _re_tz
from dataclasses import dataclass as _dataclass_tz
from typing import Awaitable, Callable, Optional, TypeVar

_T_TZ = TypeVar("_T_TZ")


# Combined "field-extraction safety net" tuple: every per-field probe in a
# scraper card-loop typically only needs to swallow these. Strict subset of
# SELECTOR_EXTRACTION_EXCEPTIONS plus LookupError parents -- distinct symbol
# so duplication checks treats it as one shared definition.
ELEMENT_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
    IndexError,
    LookupError,
)


def parse_price(price_text: Optional[str]) -> Optional[str]:
    """Return the first ``$N.NN`` substring or the stripped original."""
    if not price_text:
        return None
    match = _re_tz.search(r"\$[\d,]+\.\d{2}", price_text)
    return match.group(0) if match else price_text.strip()


def parse_price_value(price_text: Optional[str]) -> Optional[float]:
    """Numeric price for sorting / range filters."""
    if not price_text:
        return None
    match = _re_tz.search(r"[\d,]+\.\d{2}", price_text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_rating_value(rating_text: Optional[str]) -> Optional[float]:
    """First decimal in ``rating_text`` parsed as float."""
    if not rating_text:
        return None
    match = _re_tz.search(r"([\d.]+)", rating_text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_review_count(review_text: Optional[str]) -> Optional[int]:
    """Extract review count from ``"(1,234 reviews)"``-shaped text."""
    if not review_text:
        return None
    match = _re_tz.search(r"[(]?([\d,]+)[)]?", review_text.replace(",", ""))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_int_in(text: Optional[str]) -> Optional[int]:
    """Pull the first integer (commas ignored) out of free text."""
    if not text:
        return None
    match = _re_tz.search(r"([\d,]+)", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def price_in_range(
    price_text: Optional[str],
    min_price: Optional[float],
    max_price: Optional[float],
) -> bool:
    """True if a parsed numeric price sits within ``[min, max]``.

    Returns True when no range is supplied or the price cannot be parsed
    (callers preserve unparseable items by default).
    """
    if min_price is None and max_price is None:
        return True
    value = parse_price_value(price_text)
    if value is None:
        return True
    if min_price is not None and value < min_price:
        return False
    if max_price is not None and value > max_price:
        return False
    return True


def safe_extract(
    fn: Callable[[], _T_TZ],
    *,
    field: str = "value",
    default: Optional[_T_TZ] = None,
    log: Optional[logging.Logger] = None,
) -> Optional[_T_TZ]:
    """Run a small extractor, returning ``default`` if a known parse error
    fires. Replaces dozens of "extract one field, skip on failure" blocks
    that previously caught bare ``Exception``."""
    log = log or logger
    try:
        return fn()
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        log.debug("safe_extract %s suppressed %s: %s", field, type(exc).__name__, exc)
        return default


async def safe_extract_async(
    fn: Callable[[], Awaitable[_T_TZ]],
    *,
    field: str = "value",
    default: Optional[_T_TZ] = None,
    log: Optional[logging.Logger] = None,
) -> Optional[_T_TZ]:
    """Async counterpart of :func:`safe_extract`."""
    log = log or logger
    try:
        return await fn()
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        log.debug(
            "safe_extract_async %s suppressed %s: %s",
            field,
            type(exc).__name__,
            exc,
        )
        return default


@_dataclass_tz(frozen=True)
class WebhookCallSpec:
    """Args bundle for low-level webhook POST helpers."""

    url: str
    payload: dict
    secret: str = ""
    timeout_s: int = 10


@_dataclass_tz(frozen=True)
class FilterParams:
    """Common filter-and-limit parameter bag for product list pages.

    Removes the parameter-count limit blockers from helper functions that
    previously took 5-7 positional args (``min_price``, ``max_price``,
    ``min_rating``, ``sort``, ``limit``, etc.).
    """

    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_rating: Optional[float] = None
    sort: str = "relevance"
    limit: int = 20


# ---------------------------------------------------------------------------
# Shared helpers for the Google scrapers
# ---------------------------------------------------------------------------

# Network failures raised by the stdlib `urllib.request` family. Used by
# scrapers that probe healthchecks via `urllib` without pulling httpx/aiohttp.
def urllib_network_exception_types() -> tuple[type[BaseException], ...]:
    """Return the tuple of urllib + socket failure classes."""
    import socket
    import urllib.error
    return (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError)


# Combined network + decode tuple for a SearXNG-style JSON probe: any
# transport failure plus JSON / unicode decode errors. Avoids per-scraper
# duplication of the same long ``except (...)`` list.
URLLIB_JSON_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib_network_exception_types()
    + (json.JSONDecodeError, ValueError, UnicodeDecodeError, KeyError, TypeError)
)


def parse_dimension_attr(value: Optional[str]) -> Optional[int]:
    """Parse a digit-only width/height HTML attribute into ``int`` or ``None``."""
    if not value:
        return None
    stripped = value.strip()
    if not stripped or not stripped.isdigit():
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


# Phone-number pattern shared across business-listing scrapers.
US_PHONE_PATTERN = _re_tz.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")


def find_us_phone(text: Optional[str]) -> Optional[str]:
    """Return the first US-formatted phone number embedded in ``text``."""
    if not text:
        return None
    match = US_PHONE_PATTERN.search(text)
    return match.group(0) if match else None
