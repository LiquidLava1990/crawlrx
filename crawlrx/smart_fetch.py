"""smart_fetch.py -- Tiered anti-detection HTTP fetcher

Tries multiple HTTP clients in order of capability:
Tier 1: hrequests (real browser TLS fingerprints automatically)
Tier 2: primp (Rust HTTP client with impersonation)
Tier 3: cloudscraper (Cloudflare bypass)
Tier 4: requests (always-installed dependable HTTP baseline)

Returns first successful response that isn't blocked.
"""

import logging
import random
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urljoin

from ._scraper_helpers import network_exception_types
from ._utils_safety import is_safe_url

logger = logging.getLogger("crawlrx.smart_fetch")

_FETCH_FAILURE_EXCEPTIONS = network_exception_types() + (
    ValueError,
    AttributeError,
    TypeError,
    RuntimeError,
)

BLOCK_INDICATORS = [
    "captcha", "press & hold", "press and hold",
    "robot", "access denied", "blocked",
    "unusual traffic", "verify you are human",
    "cloudflare", "ddos protection", "security check",
    "access denied", "forbidden", "blocked",
]


def _is_blocked(status: int, body: str) -> bool:
    """Check if response indicates blocking."""
    if status != 200:
        return True
    body_lower = body.lower()
    return any(ind in body_lower for ind in BLOCK_INDICATORS)


def _get_browser_fingerprint() -> Dict[str, Any]:
    """Generate randomized browser fingerprint settings."""
    chrome_versions = ["120", "121", "122", "123", "124", "125", "126"]
    version = random.choice(chrome_versions)
    
    # Random screen resolutions
    resolutions = [
        (1920, 1080), (1366, 768), (1440, 900), (1536, 864),
        (1280, 720), (1600, 900), (1920, 1200), (2560, 1440),
    ]
    width, height = random.choice(resolutions)
    
    # Random platforms
    platforms = ["Windows NT 10.0; Win64; x64", "Macintosh; Intel Mac OS X 10_15_7"]
    platform = random.choice(platforms)
    
    return {
        "version": version,
        "width": width,
        "height": height,
        "platform": platform,
    }


def _build_default_headers(fp: Dict[str, Any]) -> Dict[str, str]:
    platform_label = "Windows" if "Windows" in fp["platform"] else "macOS"
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": f'"Google Chrome";v="{fp["version"]}", "Chromium";v="{fp["version"]}", "Not=A?Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": f'"{platform_label}"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


class UnsafeRedirectError(Exception):
    """A response tried to redirect the fetch into internal address space."""


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5

# A tier opener returns ``get_once(url) -> response`` with automatic redirects
# turned off, so every hop goes through ``_fetch_following_redirects``.
GetOnce = Callable[[str], Any]


def _ok_response(text: str, status: int, tier: str, final_url: str) -> Dict[str, Any]:
    return {
        "success": True,
        "html": text,
        "status": status,
        "tier": tier,
        "final_url": final_url,
        "error": None,
    }


def _failure(url: str, error: str, error_type: str = "network_error") -> Dict[str, Any]:
    return {
        "success": False,
        "html": "",
        "status": 0,
        "tier": None,
        "final_url": url,
        "error": error,
        "error_type": error_type,
    }


def _location(resp: Any) -> Optional[str]:
    headers = getattr(resp, "headers", None) or {}
    return headers.get("location") or headers.get("Location")


def _fetch_following_redirects(get_once: GetOnce, url: str) -> Tuple[Any, str]:
    """Follow redirects by hand, refusing any hop that leaves public address space."""
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        resp = get_once(current)
        location = _location(resp)
        if resp.status_code not in _REDIRECT_STATUSES or not location:
            return resp, current
        target = urljoin(current, location)
        if not is_safe_url(target):
            raise UnsafeRedirectError(f"redirect from {current} to {target} blocked: internal address")
        current = target
    raise ValueError(f"too many redirects (>{_MAX_REDIRECTS}) starting at {url}")


def _open_hrequests(headers: Dict[str, str], timeout: int, fp: Dict[str, Any]) -> GetOnce:
    import hrequests
    return lambda url: hrequests.get(url, headers=headers, timeout=timeout, allow_redirects=False)


def _primp_client(primp: Any, timeout: int, version: str) -> Any:
    """Build a primp client, falling back when this primp lacks the named profile.

    Older primp silently falls back to a random profile; newer releases raise
    ``BuilderError`` for names such as ``chrome_123``.
    """
    builder_error = getattr(primp, "BuilderError", ValueError)
    for impersonate in (f"chrome_{version}", "chrome", "random"):
        try:
            return primp.Client(impersonate=impersonate, timeout=timeout, follow_redirects=False)
        except builder_error:
            logger.debug("primp has no %r profile; trying the next", impersonate)
    return primp.Client(timeout=timeout, follow_redirects=False)


def _open_primp(headers: Dict[str, str], timeout: int, fp: Dict[str, Any]) -> GetOnce:
    import primp
    client = _primp_client(primp, timeout, fp["version"])
    return lambda url: client.get(url, headers=headers)


def _open_cloudscraper(headers: Dict[str, str], timeout: int, fp: Dict[str, Any]) -> GetOnce:
    import cloudscraper
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    return lambda url: scraper.get(url, headers=headers, timeout=timeout, allow_redirects=False)


def _open_requests(headers: Dict[str, str], timeout: int, fp: Dict[str, Any]) -> GetOnce:
    import requests
    return lambda url: requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)


TierOpener = Callable[[Dict[str, str], int, Dict[str, Any]], GetOnce]

# hrequests: real browser TLS; primp: Rust impersonation; cloudscraper: Cloudflare;
# requests: the always-installed baseline.
_TIERS: Tuple[Tuple[str, TierOpener], ...] = (
    ("hrequests", _open_hrequests),
    ("primp", _open_primp),
    ("cloudscraper", _open_cloudscraper),
    ("requests", _open_requests),
)


def _is_timeout_error(exc: BaseException) -> bool:
    """True for any tier's timeout flavor (stdlib, httpx, aiohttp, primp...)."""
    return bool(
        isinstance(exc, TimeoutError)
        or "timeout" in type(exc).__name__.lower()
        or "timed out" in str(exc).lower()
    )


def _run_tier(
    name: str, opener: TierOpener, url: str, headers: Dict[str, str], timeout: int, fp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """One tier's attempt; ``None`` means try the next tier."""
    result, _failure_kind = _run_tier_checked(name, opener, url, headers, timeout, fp)
    return result


def _run_tier_checked(
    name: str, opener: TierOpener, url: str, headers: Dict[str, str], timeout: int, fp: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """One tier's attempt; returns (result-or-None, "timeout"| None).

    ``None`` result means try the next tier; the second element records whether
    the failure was a timeout so the caller can classify the final error.
    """
    try:
        logger.debug("Trying %s", name)
        resp, final_url = _fetch_following_redirects(opener(headers, timeout, fp), url)
    except ImportError:
        logger.debug("%s not installed", name)
        return None, None
    except _FETCH_FAILURE_EXCEPTIONS as e:
        if _is_timeout_error(e):
            logger.debug("%s timed out: %s", name, e)
            return None, "timeout"
        logger.debug("%s failed: %s", name, e)
        return None, None
    if _is_blocked(resp.status_code, resp.text):
        logger.debug("%s blocked (status %s)", name, resp.status_code)
        return None, None
    logger.info("%s success: %s", name, url)
    return _ok_response(resp.text, resp.status_code, name, final_url), None


def smart_fetch(url: str, timeout: int = 20, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Tiered HTTP fetch: hrequests -> primp -> cloudscraper -> requests, public URLs only."""
    if not is_safe_url(url):
        return _failure(url, "URL blocked: internal/private network addresses are not allowed", "blocked")
    fp = _get_browser_fingerprint()
    default_headers = _build_default_headers(fp)
    if headers:
        default_headers.update(headers)

    saw_timeout = False
    for name, opener in _TIERS:
        try:
            ok, failure_kind = _run_tier_checked(name, opener, url, default_headers, timeout, fp)
        except UnsafeRedirectError as e:
            logger.warning("smart_fetch: %s", e)
            return _failure(url, f"Unsafe redirect blocked: {e}", "blocked")
        if failure_kind == "timeout":
            saw_timeout = True
        if ok is not None:
            return ok

    if saw_timeout:
        logger.warning("All smart_fetch tiers timed out for: %s", url)
        return _failure(url, f"HTTP tiers timed out for: {url}", "timeout")
    logger.warning("All smart_fetch tiers failed for: %s", url)
    return _failure(url, "All HTTP tiers failed")


def smart_fetch_with_retry(url: str, retries: int = 2, timeout: int = 20, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Fetch with retry logic.
    
    Returns: {"success": bool, "html": str, "status": int, "tier": str, "error": str|None}
    """
    last_error = None
    
    for attempt in range(retries + 1):
        if attempt > 0:
            logger.info("Retry attempt %d/%d for %s", attempt, retries, url)
        
        result = smart_fetch(url, timeout, headers)
        if result["success"] or result.get("error_type") == "blocked":
            return result
        
        last_error = result.get("error")
    
    return _failure(url, f"Failed after {retries + 1} attempts: {last_error}")
