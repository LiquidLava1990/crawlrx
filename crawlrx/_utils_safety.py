"""Safety guards: SSRF prevention + page size cap + bot-block detection."""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page size guard
# ---------------------------------------------------------------------------

MAX_PAGE_BYTES = 10 * 1024 * 1024  # 10MB


async def check_content_size(page: Page) -> bool:
    """Return False if the page HTML exceeds :data:`MAX_PAGE_BYTES`."""
    try:
        size = await page.evaluate("() => document.documentElement.outerHTML.length")
        if size > MAX_PAGE_BYTES:
            logger.warning("Page too large: %d bytes (limit %d)", size, MAX_PAGE_BYTES)
            return False
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Suppressed error: %s", exc)
    return True


# ---------------------------------------------------------------------------
# Bot / Cloudflare detection
# ---------------------------------------------------------------------------

BOT_DETECTION_SIGNALS = [
    "cf-browser-verification",
    "challenge-platform",
    "captcha",
    "access denied",
    "403 forbidden",
    "just a moment",
    "checking your browser",
    "ddos-guard",
    "please enable javascript and cookies",
]


def detect_bot_block(html: str, title: str = "") -> bool:
    """Return True if the page looks like a Cloudflare / bot challenge."""
    combined = (html[:5000] + title).lower()
    return any(signal in combined for signal in BOT_DETECTION_SIGNALS)


# ---------------------------------------------------------------------------
# SSRF prevention -- block requests to internal / private networks
# ---------------------------------------------------------------------------

_INTERNAL_HOSTNAMES = ("localhost", "0.0.0.0")
_INTERNAL_HOSTNAME_SUFFIXES = (".local", ".internal")


def _hostname_is_internal(hostname: str) -> bool:
    """True for obviously-internal hostnames (no DNS resolution required)."""
    lower = hostname.lower()
    if lower in _INTERNAL_HOSTNAMES:
        return True
    return any(lower.endswith(suffix) for suffix in _INTERNAL_HOSTNAME_SUFFIXES)


def _ip_is_internal(ip: "ipaddress._BaseAddress") -> bool:
    """True if the address sits in any non-public range."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    )


def _resolved_ip_is_internal(hostname: str) -> bool:
    """Resolve hostname; True iff ANY result is internal. False on DNS error."""
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, ValueError):
        return False
    for _family, _, _, _, sockaddr in addr_info:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_internal(ip):
            return True
    return False


_SSRF_OVERRIDE_ENV = "CRAWLRX_SSRF_ALLOW_PRIVATE"
_TRUTHY = ("1", "true", "yes")


def _ssrf_override_enabled() -> bool:
    """Opt-in escape hatch allowing private/loopback targets.

    Default is OFF (strict SSRF protection). Self-hosted operators who need
    to crawl internal dashboards or run local fixtures opt in explicitly via
    CRAWLRX_SSRF_ALLOW_PRIVATE=1.
    """
    return os.getenv(_SSRF_OVERRIDE_ENV, "").strip().lower() in _TRUTHY


def is_safe_url(url: str) -> bool:
    """Reject URLs whose host resolves to private / loopback / link-local space.

    Set ``CRAWLRX_SSRF_ALLOW_PRIVATE=1`` to permit internal targets (opt-in,
    for self-hosted intranet crawling and local fixture testing).
    """
    if _ssrf_override_enabled():
        return True
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if _hostname_is_internal(hostname):
        return False
    if _resolved_ip_is_internal(hostname):
        return False
    return True


# ---------------------------------------------------------------------------
# Error classification (shared by crawl, scrape, etc.)
# ---------------------------------------------------------------------------


def classify_error(err_msg: str) -> str:
    """Map an error message string to a structured ``error_type``."""
    msg = err_msg.lower()
    if "timeout" in msg:
        return "timeout"
    if any(w in msg for w in ("bot", "cloudflare", "blocked")):
        return "blocked"
    return "network_error"
