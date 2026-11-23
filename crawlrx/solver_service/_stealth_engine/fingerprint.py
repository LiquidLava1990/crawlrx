"""Coherent browser identity: browserforge UA + brands, our pool data.

browserforge (Apache-2.0) supplies UA / brands / platform; screen metrics,
WebGL pairs, and locales come from our own pools (``solvers.
_stealth_browser_fingerprint``). Coherence rule: the Chrome major version in
the UA must equal the major version in ``brands`` and ``fullVersionList`` —
mismatched versions are a classic automation tell.
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from typing import List, Tuple

from ..solvers._stealth_browser_fingerprint import (
    LOCALES,
    TIMEZONES,
    WEBGL_RENDERERS,
    WEBGL_VENDORS,
)

try:
    from browserforge.fingerprints import FingerprintGenerator

    _HAVE_BROWSERFORGE = True
except ImportError:  # pragma: no cover - graceful lane when missing
    _HAVE_BROWSERFORGE = False


_PLATFORM_HINTS = {
    "Windows": ("Win32", "Windows NT 10.0; Win64, x64", "10.0.0"),
    "macOS": ("MacIntel", "Macintosh; Intel Mac OS X 10_15_7", "13.5.0"),
    "Linux": ("Linux x86_64", "X11; Linux x86_64", "6.5.0"),
}


@dataclass(frozen=True)
class Identity:
    """Everything needed to forge one coherent browser persona."""

    user_agent: str
    brands: List[Tuple[str, str]] = field(default_factory=list)
    full_version_list: List[Tuple[str, str]] = field(default_factory=list)
    brand_brand: str = "Chromium"
    platform: str = "Win32"
    platform_name: str = "Windows"
    platform_version: str = "10.0.0"
    architecture: str = "x86"
    bitness: str = "64"
    locale: str = "en-US"
    timezone: str = "America/New_York"
    screen_width: int = 1366
    screen_height: int = 768
    viewport_width: int = 1366
    viewport_height: int = 728
    color_depth: int = 24
    device_pixel_ratio: float = 1.0
    webgl_vendor: str = "Intel Inc."
    webgl_renderer: str = "Intel Iris Xe Graphics"

    @property
    def chrome_major(self) -> str:
        try:
            return self.user_agent.split("Chrome/")[1].split(" ")[0].split(".")[0]
        except IndexError:
            return ""

    @property
    def full_version(self) -> str:
        try:
            return self.user_agent.split("Chrome/")[1].split(" ")[0]
        except IndexError:
            return ""


_GREASE_BRANDS = [
    ("Not:A-Brand", "99"),
    ("Not A;Brand", "99"),
    ("Not;A=Brand", "8"),
]


def _brands_for(major: str, full: str) -> List[Tuple[str, str]]:
    grease = random.choice(_GREASE_BRANDS)
    return [
        (grease[0], grease[1]),
        ("Chromium", major),
        ("Google Chrome", major),
    ]


def _full_version_list(major: str, full: str) -> List[Tuple[str, str]]:
    grease = random.choice(_GREASE_BRANDS)
    return [
        (grease[0], grease[1]),
        ("Chromium", full),
        ("Google Chrome", full),
    ]


def build_identity(seed_generator: Any = None) -> Identity:
    """Forge one coherent identity (browserforge when present, pools otherwise)."""
    if _HAVE_BROWSERFORGE:
        fp = (seed_generator or FingerprintGenerator()).generate(browser="chrome")
        nav = fp.navigator
        ua = nav.userAgent
        uad = nav.userAgentData if isinstance(nav.userAgentData, dict) else {}
        brands_raw = uad.get("brands") or []
        brands = [(b.get("brand", ""), b.get("version", "")) for b in brands_raw]
        major = ua.split("Chrome/")[1].split(" ")[0].split(".")[0] if "Chrome/" in ua else ""
        full = ua.split("Chrome/")[1].split(" ")[0] if "Chrome/" in ua else ""
        if major:
            brands = _brands_for(major, full)
            full_version_list = _full_version_list(major, full)
        else:
            # Non-Chrome UA (Edge/Safari variants): derive a coherent Chrome
            # family brand set anyway — client hints without brands are an
            # automation tell, and the CDP override contract requires them.
            derived = next(
                (ver for _, ver in brands if ver and ver[0].isdigit()), None
            )
            major = derived or "122"
            full = f"{major}.0.0.0"
            brands = _brands_for(major, full)
            full_version_list = _full_version_list(major, full)
        platform_name = next(
            (name for name, hints in _PLATFORM_HINTS.items() if hints[0] == nav.platform),
            "Windows",
        )
        hint, _, plat_ver = _PLATFORM_HINTS.get(platform_name, _PLATFORM_HINTS["Windows"])
        sw, sh = fp.screen.width or 1366, fp.screen.height or 768
        vw = fp.screen.innerWidth or sw
        vh = fp.screen.innerHeight or max(sh - 40, 480)
        return Identity(
            user_agent=ua,
            brands=brands,
            full_version_list=full_version_list,
            brand_brand="Google Chrome",
            platform=nav.platform or hint,
            platform_name=platform_name,
            platform_version=uad.get("platformVersion") or plat_ver,
            locale=getattr(nav, "language", None) or random.choice(LOCALES),
            timezone=random.choice(TIMEZONES),
            screen_width=sw,
            screen_height=sh,
            viewport_width=max(vw, 640),
            viewport_height=max(vh, 480),
            color_depth=getattr(fp.screen, "colorDepth", 24) or 24,
            device_pixel_ratio=float(getattr(fp.screen, "devicePixelRatio", 1.0) or 1.0),
            webgl_vendor=random.choice(WEBGL_VENDORS),
            webgl_renderer=random.choice(WEBGL_RENDERERS),
        )

    # Static-pool fallback (no browserforge): ours-only path.
    from ..solvers._stealth_browser_fingerprint import generate_fingerprint as pool_fp

    p = pool_fp()
    major = p.user_agent.split("Chrome/")[1].split(" ")[0] if "Chrome/" in p.user_agent else "122"
    full = f"{major}.0.0.0"
    return Identity(
        user_agent=p.user_agent,
        brands=_brands_for(major, full),
        full_version_list=_full_version_list(major, full),
        brand_brand="Google Chrome",
        platform=p.platform,
        platform_name="Windows" if p.platform == "Win32" else "macOS",
        platform_version=_PLATFORM_HINTS["Windows"][2],
        locale=p.locale,
        timezone=p.timezone,
        screen_width=p.viewport_width,
        screen_height=p.viewport_height,
        viewport_width=p.viewport_width,
        viewport_height=p.viewport_height,
        color_depth=p.color_depth,
        device_pixel_ratio=p.pixel_ratio,
        webgl_vendor=p.webgl_vendor,
        webgl_renderer=p.webgl_renderer,
    )
