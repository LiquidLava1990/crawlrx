"""Browser fingerprint dataclass + UA / locale / timezone / WebGL pools.

Extracted from ``stealth_browser.py`` to keep the orchestrator under the
500-LOC sibling cap. Pool-side code imports :class:`BrowserFingerprint`
and the constants from here.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List


@dataclass
class BrowserFingerprint:
    """Unique fingerprint for each browser instance."""

    user_agent: str
    viewport_width: int
    viewport_height: int
    timezone: str
    locale: str
    platform: str
    webgl_vendor: str
    webgl_renderer: str
    color_depth: int
    pixel_ratio: float


# Diverse user agents for rotation
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

TIMEZONES: List[str] = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Asia/Tokyo", "Asia/Shanghai", "Australia/Sydney",
]

LOCALES: List[str] = ["en-US", "en-GB", "de-DE", "fr-FR", "ja-JP", "zh-CN"]

WEBGL_VENDORS: List[str] = [
    "Intel Inc.", "NVIDIA Corporation", "AMD",
    "Apple Inc.", "Google Inc. (NVIDIA)", "Google Inc. (AMD)",
]

WEBGL_RENDERERS: List[str] = [
    "Intel Iris Xe Graphics", "NVIDIA GeForce GTX 1060", "AMD Radeon RX 580",
    "Apple M1", "NVIDIA GeForce RTX 3080", "AMD Radeon Pro 5500M",
    "Intel HD Graphics 630", "NVIDIA GeForce RTX 4090",
]


def generate_fingerprint() -> BrowserFingerprint:
    """Generate a unique randomized :class:`BrowserFingerprint`."""
    chosen_ua = random.choice(USER_AGENTS)
    return BrowserFingerprint(
        user_agent=chosen_ua,
        viewport_width=random.randint(1280, 1920),
        viewport_height=random.randint(720, 1080),
        timezone=random.choice(TIMEZONES),
        locale=random.choice(LOCALES),
        platform="Win32" if "Windows" in chosen_ua else "MacIntel",
        webgl_vendor=random.choice(WEBGL_VENDORS),
        webgl_renderer=random.choice(WEBGL_RENDERERS),
        color_depth=24,
        pixel_ratio=random.choice([1.0, 1.25, 1.5, 2.0]),
    )
