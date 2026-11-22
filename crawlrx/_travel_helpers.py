"""Travel-scraper helpers (sibling module).

Holds the narrow exception tuples, request helpers, and field-parse
primitives used by :mod:`crawlrx.travel_scraper`. Splitting them out
lets the public scraper functions stay below the code-quality cognitive /
nesting / cyclomatic ceilings.

Re-imported by ``travel_scraper`` only -- callers should not depend on
this private surface directly.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from ._scraper_helpers import (
    PARSE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    log_and_swallow,
)
from .stealth_context import close_stealth_context, make_stealth_context

logger = logging.getLogger("crawlrx.travel.helpers")

PLAYWRIGHT_PORT = 9233  # optional CDP port for travel scrapers

# Network-layer failures from `requests`. Kept narrow on purpose: anything
# else (KeyboardInterrupt, MemoryError, ...) bubbles out for callers to
# decide.
REQUESTS_NETWORK_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    requests.exceptions.RequestException,
    OSError,
    TimeoutError,
    ConnectionError,
)


# Playwright failures. Resolved at import time, falls back to a
# realistic surface if playwright isn't installed.
def _resolve_pw_exceptions() -> Tuple[type[BaseException], ...]:
    import asyncio

    base: list[type[BaseException]] = [
        asyncio.TimeoutError,
        OSError,
        RuntimeError,
        TimeoutError,
    ]
    try:
        from playwright.async_api import (
            Error as _PWError,
            TimeoutError as _PWTimeout,
        )
        base.extend([_PWError, _PWTimeout])
    except ImportError:
        pass
    return tuple(base)


PLAYWRIGHT_EXCEPTIONS: Tuple[type[BaseException], ...] = _resolve_pw_exceptions()


# Default request headers shared by all sub-scrapers.
_DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}


def requests_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 10,
) -> Optional[requests.Response]:
    """``requests.get`` that never raises -- returns the response or None.

    Catches only the documented :data:`REQUESTS_NETWORK_EXCEPTIONS` tuple;
    surprising failure types now propagate so they can be diagnosed.
    """
    merged = dict(_DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    try:
        resp = requests.get(url, headers=merged, timeout=timeout)
        resp.raise_for_status()
        return resp
    except REQUESTS_NETWORK_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"requests_get {url}", exc)
        return None


async def playwright_scrape(
    url: str,
    selector: str = "body",
    timeout: int = 15,
) -> Optional[str]:
    """Headless Playwright fetch returning the rendered HTML or None."""
    browser = ctx = page = None
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            await page.wait_for_timeout(2000)  # JS render breathing room
            return await page.content()
    except PLAYWRIGHT_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, f"playwright_scrape {url}", exc)
        return None
    finally:
        await close_stealth_context(browser, ctx, page)


def extract_price(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(price_str, currency)`` parsed from text or ``(None, None)``."""
    if not text:
        return None, None
    match = re.search(
        r"([\$€£¥]|USD|EUR|GBP)?\s*([\d,]+(?:\.\d{2})?)",
        text.replace(",", ""),
    )
    if not match:
        return None, None
    return match.group(2), (match.group(1) or "$")


# Pre-compiled airline regex shared by all flight-card extractors.
_AIRLINE_RE = re.compile(
    r"(American|Delta|United|Southwest|JetBlue|Alaska|Spirit|Frontier|"
    r"Air Canada|Lufthansa|Emirates|Qatar|British Airways|Air France|KLM)",
    re.IGNORECASE,
)


def airline_in(text: str) -> str:
    """Return a known airline name found in ``text`` or ``"Unknown"``."""
    match = _AIRLINE_RE.search(text or "")
    return match.group(1) if match else "Unknown"


def soup_of(html_or_resp: "requests.Response | str") -> BeautifulSoup:
    """Build a BeautifulSoup from either a `requests.Response` or HTML str."""
    text = html_or_resp.text if hasattr(html_or_resp, "text") else html_or_resp
    return BeautifulSoup(text, "html.parser")


# Field-extraction tuple for selector probes inside a card loop. Imported
# from _scraper_helpers but re-exported under a local name so call sites
# can read as `except _CARD_PARSE_EXC`.
CARD_PARSE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    SELECTOR_EXTRACTION_EXCEPTIONS + PARSE_EXCEPTIONS
)


def safe_card_parse(
    parser: Callable[[], Optional[Dict]],
    *,
    where: str,
) -> Optional[Dict]:
    """Run a per-card parser, swallowing the documented parse-error tuple."""
    try:
        return parser()
    except CARD_PARSE_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, where, exc)
        return None


async def safe_card_parse_async(
    parser: Callable[[], Awaitable[Optional[Dict]]],
    *,
    where: str,
) -> Optional[Dict]:
    """Async counterpart of :func:`safe_card_parse`."""
    try:
        return await parser()
    except CARD_PARSE_EXCEPTIONS as exc:
        log_and_swallow(logger, logging.DEBUG, where, exc)
        return None


@dataclass(frozen=True)
class FlightQuery:
    """Bundled args for flight-source extractors -- keeps param_count clean."""

    origin: str
    destination: str
    depart_date: str = ""
    return_date: str = ""


@dataclass(frozen=True)
class HotelQuery:
    """Bundled args for hotel-source extractors."""

    location: str
    checkin: str = ""
    checkout: str = ""
    guests: int = 2


def parse_rating_tenth(rating_text: str) -> Optional[float]:
    """Pull the first decimal out of ``rating_text``; values >10 normalised."""
    match = re.search(r"(\d+\.?\d*)", rating_text or "")
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value / 10 if value > 10 else value
