"""_ecommerce_product_helpers.py -- Shared helpers for e-commerce product scrapers.

Absorbs patterns common across amazon.py, amazon_scraper.py, producthunt.py,
shopify.py, shift4shop_scraper.py, bigcommerce_scraper.py.

All exception tuples here are narrow (no `Exception` catches) to satisfy
`the no-broad-except rule`. Helpers extract repeated parsing / DOM-probe shapes
that previously triggered `duplication checks`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, List, Optional, TypeVar

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._cms_helpers import REQUESTS_EXCEPTIONS
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
)

logger = logging.getLogger("crawlrx._ecommerce_product_helpers")

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Exception tuples (canonical, narrow)
# ---------------------------------------------------------------------------

# Per-card / DOM probe failures inside Playwright loops. Strict subset of
# SELECTOR_EXTRACTION_EXCEPTIONS plus LookupError parents.
CARD_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    SELECTOR_EXTRACTION_EXCEPTIONS + (LookupError,)
)


# Combined transport-level + parsing failure tuple for httpx-driven scrapers.
def httpx_exception_types() -> tuple[type[BaseException], ...]:
    """Return the tuple of httpx + transport + decode failure classes."""
    base: list[type[BaseException]] = [
        OSError,
        ConnectionError,
        TimeoutError,
        ValueError,
        UnicodeDecodeError,
    ]
    try:
        import httpx
        base.extend(
            [
                httpx.HTTPError,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RequestError,
            ]
        )
    except ImportError:
        pass
    return tuple(base)


HTTPX_EXCEPTIONS: tuple[type[BaseException], ...] = httpx_exception_types()


# Combined Playwright + network failures for browser-driven helpers.
BROWSER_OR_HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BROWSER_SCRAPE_EXCEPTIONS + REQUESTS_EXCEPTIONS
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"\$?[\d,]+\.?\d*")
_RATING_OUT_OF_RE = re.compile(r"([\d.]+)\s*out of", re.IGNORECASE)
_RATING_STARS_RE = re.compile(r"([\d.]+)\s*stars?", re.IGNORECASE)
_RATING_FALLBACK_RE = re.compile(r"([\d.]+)")
_REVIEW_COUNT_RE = re.compile(r"[\d,]+")
_ASIN_RES = (
    re.compile(r"/dp/([A-Z0-9]{10})"),
    re.compile(r"/gp/product/([A-Z0-9]{10})"),
    re.compile(r"/product/([A-Z0-9]{10})"),
)
_KFORMAT_RE = re.compile(r"([\d.]+k?)", re.IGNORECASE)


def parse_amazon_price(text: Optional[str]) -> Optional[str]:
    """Extract '$N.NN'-shaped price from text. Returns None on miss."""
    if not text:
        return None
    cleaned = text.replace(",", "")
    match = _PRICE_RE.search(cleaned)
    if not match:
        return None
    price = match.group()
    if not price.startswith("$"):
        price = f"${price}"
    return price


def _try_rating_regex(regex: re.Pattern[str], text: str) -> Optional[float]:
    """Run one rating regex; None when it doesn't match or float() fails."""
    match = regex.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_amazon_rating(text: Optional[str]) -> Optional[float]:
    """Extract a float rating from text like '4.5 out of 5 stars'."""
    if not text:
        return None
    lowered = text.lower()
    for regex in (_RATING_OUT_OF_RE, _RATING_STARS_RE, _RATING_FALLBACK_RE):
        value = _try_rating_regex(regex, lowered)
        if value is not None:
            return value
    return None


def parse_review_count_int(text: Optional[str]) -> Optional[int]:
    """Extract integer review count from text like '(1,234)'."""
    if not text:
        return None
    match = _REVIEW_COUNT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group().replace(",", ""))
    except ValueError:
        return None


def parse_asin_from_url(url: str) -> Optional[str]:
    """Extract a 10-char ASIN from any Amazon product URL flavor."""
    if not url:
        return None
    for regex in _ASIN_RES:
        match = regex.search(url)
        if match:
            return match.group(1)
    return None


def parse_kformat_int(text: Optional[str]) -> int:
    """Parse '1.2k' / '1,234' / '123' as int. Returns 0 on miss."""
    if not text:
        return 0
    cleaned = text.strip().lower().replace(",", "")
    if not cleaned:
        return 0
    try:
        if "k" in cleaned:
            return int(float(cleaned.replace("k", "")) * 1000)
        return int(cleaned)
    except ValueError:
        return 0


def parse_kformat_match(text: Optional[str]) -> int:
    """Pull the first '1.2k'-style number out of free text. Returns 0 on miss."""
    if not text:
        return 0
    match = _KFORMAT_RE.search(text)
    if not match:
        return 0
    return parse_kformat_int(match.group(1))


def parse_price_decimal(value: object) -> Optional[float]:
    """Coerce a Shopify-style price value to float. None on failure."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_price_string(value: object) -> str:
    """Format a price-shaped value as 'NN.NN'. Falls back to str()."""
    if value is None:
        return ""
    try:
        return "%.2f" % float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# DOM extraction helpers (BeautifulSoup-friendly)
# ---------------------------------------------------------------------------

def soup_text(element: "ElementHandle | None") -> Optional[str]:
    """Safely return stripped text from a soup element. None on missing."""
    if element is None:
        return None
    try:
        text = element.get_text(strip=True)
    except CARD_PROBE_EXCEPTIONS:
        return None
    return text or None


def attr_or_none(element: "ElementHandle | None", name: str) -> Optional[str]:
    """Safely fetch an HTML attribute. None on missing."""
    if element is None:
        return None
    try:
        value = element.get(name)
    except CARD_PROBE_EXCEPTIONS:
        return None
    if isinstance(value, str) and value:
        return value
    return None


# ---------------------------------------------------------------------------
# Captcha helpers
# ---------------------------------------------------------------------------

# Default captcha signal lookup -- callers extend / override this.
DEFAULT_CAPTCHA_SIGNALS: tuple[str, ...] = (
    "robot check",
    "captcha",
    "enter the characters",
    "type the characters you see",
    "sorry, we just need to make sure you're not a robot",
    "enter the characters you see in this image",
    "enter the characters you see below",
)


def text_has_captcha(text: str, signals: Iterable[str] = DEFAULT_CAPTCHA_SIGNALS) -> bool:
    """True if any captcha signal substring appears in ``text`` (case-insensitive)."""
    lowered = text.lower()
    return any(signal.lower() in lowered for signal in signals)


# ---------------------------------------------------------------------------
# Async safe-extract (per-field DOM probe wrapper)
# ---------------------------------------------------------------------------

async def safe_field_extract(
    fn: Callable[[], Awaitable[_T]],
    *,
    field: str,
    default: Optional[_T] = None,
    log: Optional[logging.Logger] = None,
) -> Optional[_T]:
    """Run an async per-field extractor; suppress narrow probe failures."""
    target_log = log or logger
    try:
        return await fn()
    except CARD_PROBE_EXCEPTIONS as exc:
        target_log.debug("safe_field_extract %s suppressed %s: %s", field, type(exc).__name__, exc)
        return default
    except PLAYWRIGHT_EXCEPTIONS as exc:
        target_log.debug("safe_field_extract %s suppressed %s: %s", field, type(exc).__name__, exc)
        return default


# ---------------------------------------------------------------------------
# Param dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreScrapeParams:
    """Common URL+filter params for storefront scrapers (Shift4Shop, BigCommerce, ...)."""

    url: str
    query: Optional[str] = None
    limit: int = 50


@dataclass(frozen=True)
class ProductCardSelectors:
    """Selector bundle for a Playwright product-card loop."""

    card: List[str]
    title: List[str]
    price: List[str]
    link: List[str]
    image: List[str]


# ---------------------------------------------------------------------------
# Shopify-specific helpers
# ---------------------------------------------------------------------------

def build_shopify_product(raw: dict, store_base: str) -> dict:
    """Normalize a Shopify /products.json variant into the standard shape."""
    variants = raw.get("variants", []) or []
    images = raw.get("images", []) or []
    first_variant = variants[0] if variants else {}
    first_image = images[0] if images else {}
    handle = raw.get("handle", "")
    image_url = first_image.get("src") if isinstance(first_image, dict) else None
    return {
        "title": raw.get("title", ""),
        "price": parse_price_decimal(first_variant.get("price")),
        "compare_at_price": parse_price_decimal(first_variant.get("compare_at_price")),
        "url": f"{store_base}/products/{handle}" if handle else None,
        "image": image_url,
        "available": bool(first_variant.get("available", True)),
        "sku": first_variant.get("sku") or None,
        "vendor": raw.get("vendor") or None,
    }
