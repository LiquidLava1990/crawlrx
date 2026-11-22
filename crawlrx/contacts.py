"""contacts.py -- POST /extract/contacts -- extract emails, phones, social links from any URL"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Iterable, Set

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright

from urllib.parse import urlparse

from fastapi import APIRouter

from .models import ContactsRequest, ContactsResponse
from .utils import timer_ms
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import playwright_exception_types

logger = logging.getLogger("crawlrx.contacts")
router = APIRouter(prefix="/extract", tags=["extract"])

# Regex patterns
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_PATTERN = re.compile(r'(\+?1?\s?)?(\(?\d{3}\)?[\s.\-]?)(\d{3}[\s.\-]?\d{4})')

# Social media domains to detect
SOCIAL_DOMAINS = {
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "github.com": "github",
}

# Image/asset extensions to filter out from emails
BAD_EMAIL_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.pdf')

# Narrow exception families. Network/runtime/parse failures during a Playwright
# fetch are recoverable; anything else (KeyboardInterrupt, MemoryError) must
# propagate. Browser cleanup is best-effort.
_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    playwright_exception_types() + (RuntimeError, OSError, ValueError)
)
_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = _FETCH_EXCEPTIONS


def _normalize_phone(phone: str) -> str:
    """Normalize phone number to standard format (XXX) XXX-XXXX."""
    digits = re.sub(r'\D', '', phone)
    # Remove leading 1 if present (US country code)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return "(%s) %s-%s" % (digits[0:3], digits[3:6], digits[6:])
    return phone


def _extract_emails(html: str) -> Set[str]:
    """All distinct case-folded emails not pointing at image/asset extensions."""
    found: Set[str] = set()
    for match in EMAIL_PATTERN.findall(html):
        email = match.lower().strip()
        if any(email.endswith(ext) for ext in BAD_EMAIL_EXTENSIONS):
            continue
        found.add(email)
    return found


def _extract_phones(html: str) -> Set[str]:
    """All distinct normalised US-format phones found in raw HTML."""
    found: Set[str] = set()
    for match in PHONE_PATTERN.findall(html):
        phone = ''.join(match).strip()
        if phone:
            found.add(_normalize_phone(phone))
    return found


def _classify_social_link(href: str) -> tuple[str, str] | None:
    """Return ``(social_name, href)`` for known platforms, else ``None``."""
    if not href.startswith("http"):
        return None
    parsed = urlparse(href)
    domain = parsed.netloc.lower().replace("www.", "")
    for social_domain, social_name in SOCIAL_DOMAINS.items():
        if social_domain in domain:
            return social_name, href
    return None


def _extract_social_links(html: str) -> Dict[str, str]:
    """Map each known social platform to the first matching ``href`` in ``html``."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, str] = {}
    for a_tag in soup.find_all("a", href=True):
        classified = _classify_social_link(a_tag["href"].strip())
        if classified is None:
            continue
        social_name, href = classified
        found.setdefault(social_name, href)
    return found


def _extract_from_html(html: str) -> Dict[str, Any]:
    """Extract emails, phones, and social links from HTML content."""
    return {
        "emails": sorted(_extract_emails(html)),
        "phones": sorted(_extract_phones(html)),
        "social_links": _extract_social_links(html),
    }


def _empty_contact_result(url: str) -> Dict[str, Any]:
    """Default zero-state contacts result payload."""
    return {
        "success": False,
        "url": url,
        "emails": [],
        "phones": [],
        "social_links": {},
        "error": None,
        "elapsed_ms": 0,
    }


async def _populate_contacts(
    page: Any,
    url: str,
    timeout: int,
    result: Dict[str, Any],
) -> None:
    """Navigate, wait for network-idle (best-effort), extract contacts in place."""
    from playwright.async_api import TimeoutError as PWTimeout

    logger.info("Fetching URL for contacts extraction: %s", url)
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    except PWTimeout:
        pass  # Continue anyway

    extracted = _extract_from_html(await page.content())
    result["emails"] = extracted["emails"]
    result["phones"] = extracted["phones"]
    result["social_links"] = extracted["social_links"]
    result["success"] = True
    logger.info(
        "Extracted contacts from %s: %d emails, %d phones, %d social links",
        url,
        len(result["emails"]),
        len(result["phones"]),
        len(result["social_links"]),
    )


async def close_browser_quietly(browser: "Browser | BrowserContext | Page | None") -> None:
    """Best-effort browser close; logs but does not raise."""
    if browser is None:
        return
    try:
        await browser.close()
    except _CLEANUP_EXCEPTIONS as exc:
        logger.warning(
            "Error closing browser (%s): %s", type(exc).__name__, exc
        )
        return None  # explicit recovery: cleanup is best-effort; _CLEANUP_EXCEPTIONS is non-fatal


async def _do_extract_contacts(url: str, timeout: int) -> Dict[str, Any]:
    """Async Playwright fetch + contacts extraction (run-to-completion)."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    start = timer_ms()
    result = _empty_contact_result(url)
    browser = None
    ctx = None
    page = None

    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            await _populate_contacts(page, url, timeout, result)
    except PWTimeout as exc:
        result["error"] = "Timeout: %s" % exc
        logger.error("Timeout extracting contacts from %s: %s", url, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except _FETCH_EXCEPTIONS as exc:
        result["error"] = "Extraction failed (%s): %s" % (type(exc).__name__, exc)
        logger.error(
            "Error extracting contacts from %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return None  # explicit recovery: non-fatal: _FETCH_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await close_stealth_context(browser, ctx, page)
        await close_browser_quietly(browser)
        result["elapsed_ms"] = int(timer_ms() - start)

    return result


@router.post(
    "/contacts",
    response_model=ContactsResponse,
    summary="Extract emails, phones, and social links from a URL",
)
async def extract_contacts(request: ContactsRequest) -> ContactsResponse:
    """Extract emails, phones, and social links from a URL with a wall-clock cap."""
    timeout = 30000  # Default 30s timeout
    wall_timeout = timeout // 1000 + 10

    try:
        raw = await asyncio.wait_for(
            _do_extract_contacts(request.url, timeout),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for contacts extraction")
        return ContactsResponse(
            success=False,
            url=request.url,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
            elapsed_ms=wall_timeout * 1000,
        )

    return ContactsResponse(**raw)
