"""DataDome browser-side helpers — cookie store + fingerprint manager.

Split out of ``datadome_solver.py`` so the orchestrator stays thin.
This module owns the persistent cookie cache and the per-domain
browser fingerprint synthesizer; both classes are re-exported from
``datadome_solver`` to preserve the public API.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


# --- ErrCode/emit shim (kept aligned with the orchestrator) ----------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

from ._solver_helpers import BROAD_EXC as _BROAD_EXC


logger = logging.getLogger(__name__)


class DataDomeCookieStore:
    """Persistent cookie storage with TTL tracking for datadome cookies."""

    COOKIE_FILE = Path(__file__).parent / "datadome_cookies.json"
    DEFAULT_TTL_HOURS = 24  # DataDome cookies typically last 24-48 hours

    def __init__(self) -> None:
        self._cookies: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Load cookies from disk."""
        if not self.COOKIE_FILE.exists():
            return
        try:
            with open(self.COOKIE_FILE, "r", encoding="utf-8-sig", errors="replace") as f:
                self._cookies = json.load(f)
            logger.debug("Loaded %s cookie entries", len(self._cookies))
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.warning("Failed to load cookie store: %s", e)
            self._cookies = {}
            return  # explicit recovery: corrupt/missing file -> empty cookie store

    def _save(self) -> None:
        """Save cookies to disk."""
        try:
            with open(self.COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cookies, f, indent=2, default=str)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.warning("Failed to save cookie store: %s", e)
            return  # explicit recovery: persistence failure is non-fatal at runtime

    @staticmethod
    def _get_domain_key(url: str) -> str:
        """Generate normalized domain key from URL (strips ``www.`` prefix)."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def get_cookie(self, url: str) -> Optional[Dict[str, str]]:
        """Get valid datadome cookie for domain if available."""
        domain = self._get_domain_key(url)
        entry = self._cookies.get(domain)
        if not entry:
            return None
        if self._is_cookie_expired(entry):
            self._purge_cookie(domain)
            return None
        logger.info("Found valid cookie for %s", domain)
        return {
            "name": entry["name"],
            "value": entry["value"],
            "domain": entry.get("domain", domain),
        }

    def _is_cookie_expired(self, entry: Dict[str, Any]) -> bool:
        """True iff ``entry``'s ``expires`` field is in the past."""
        expires = entry.get("expires")
        if not expires:
            return False
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires)
            except _BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
                return False
        return datetime.now(tz=timezone.utc) > expires

    def _purge_cookie(self, domain: str) -> None:
        """Drop the stored cookie for ``domain`` and persist the change."""
        logger.info("Cookie expired for %s", domain)
        del self._cookies[domain]
        self._save()

    def set_cookie(
        self,
        url: str,
        name: str,
        value: str,
        ttl_hours: Optional[int] = None,
    ) -> None:
        """Store datadome cookie for ``url``'s domain with TTL."""
        domain = self._get_domain_key(url)
        ttl = ttl_hours or self.DEFAULT_TTL_HOURS
        now = datetime.now(tz=timezone.utc)
        self._cookies[domain] = {
            "name": name,
            "value": value,
            "domain": domain,
            "created_at": now.isoformat(),
            "expires": (now + timedelta(hours=ttl)).isoformat(),
        }
        self._save()
        logger.info("Stored cookie for %s (expires in %sh)", domain, ttl)

    def invalidate(self, url: str) -> None:
        """Invalidate cookie for domain (e.g., after detection)."""
        domain = self._get_domain_key(url)
        if domain in self._cookies:
            del self._cookies[domain]
            self._save()
            logger.info("Invalidated cookie for %s", domain)


# --- Fingerprint synthesis -------------------------------------------------

_VIEWPORTS = (
    (1920, 1080), (1366, 768), (1536, 864), (1440, 900),
    (1280, 720), (1600, 900), (2560, 1440), (1680, 1050),
)
_CHROME_VERSIONS = ("120", "121", "122", "123", "124", "125", "126")
_PLATFORMS = (
    "Windows NT 10.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
)
_TIMEZONES = (
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Europe/London", "Europe/Paris", "Europe/Berlin",
)
_LOCALES = ("en-US", "en-GB", "en-CA")
_WEBGL_RENDERERS: Dict[str, list[str]] = {
    "Google Inc. (NVIDIA)": [
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ],
    "Google Inc. (AMD)": [
        "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ],
    "Intel Inc.": [
        "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ],
    "Apple Inc.": ["Apple M1", "Apple M2", "Apple M3"],
}
_WEBGL_DEFAULT = "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)"


def _user_agent_for(platform: str, chrome_ver: str) -> str:
    """Build a UA string matching the platform/chrome combo."""
    if "Windows" in platform:
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_ver}.0.0.0 Safari/537.36"
        )
    return (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver}.0.0.0 Safari/537.36"
    )


def _webgl_vendor_for(platform: str) -> str:
    """Pick a plausible WebGL vendor for ``platform``."""
    if "Windows" in platform:
        return random.choice(
            ["Google Inc. (NVIDIA)", "Google Inc. (AMD)", "Intel Inc."]
        )
    return "Apple Inc."


class FingerprintManager:
    """Manages consistent fingerprints per domain session."""

    def __init__(self) -> None:
        self._fingerprints: Dict[str, Dict[str, Any]] = {}

    def get_fingerprint(self, domain: str) -> Dict[str, Any]:
        """Get or create consistent fingerprint for ``domain``."""
        if domain not in self._fingerprints:
            self._fingerprints[domain] = self._generate_fingerprint()
        return self._fingerprints[domain]

    def rotate_fingerprint(self, domain: str) -> Dict[str, Any]:
        """Generate new fingerprint for ``domain`` (for retry)."""
        self._fingerprints[domain] = self._generate_fingerprint()
        return self._fingerprints[domain]

    def _generate_fingerprint(self) -> Dict[str, Any]:
        """Generate a new browser fingerprint."""
        platform = random.choice(_PLATFORMS)
        chrome_ver = random.choice(_CHROME_VERSIONS)
        platform_str = "Win32" if "Windows" in platform else "MacIntel"
        webgl_vendor = _webgl_vendor_for(platform)
        viewport = random.choice(_VIEWPORTS)
        return {
            "user_agent": _user_agent_for(platform, chrome_ver),
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "platform": platform_str,
            "webgl_vendor": webgl_vendor,
            "webgl_renderer": self._get_webgl_renderer(webgl_vendor),
            "color_depth": 24,
            "pixel_ratio": random.choice([1.0, 1.25, 1.5, 2.0]),
            "timezone": random.choice(_TIMEZONES),
            "locale": random.choice(_LOCALES),
            "chrome_version": chrome_ver,
        }

    @staticmethod
    def _get_webgl_renderer(vendor: str) -> str:
        """Get consistent WebGL renderer for ``vendor``."""
        return random.choice(_WEBGL_RENDERERS.get(vendor, [_WEBGL_DEFAULT]))


__all__ = ["DataDomeCookieStore", "FingerprintManager"]
