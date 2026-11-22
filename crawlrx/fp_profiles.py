"""fp_profiles.py -- Fingerprint profile pool using browserforge."""

from browserforge.fingerprints import FingerprintGenerator
import random
import json
import logging
from pathlib import Path

PROFILE_CACHE = Path(__file__).parent / "fp_cache.json"
logger = logging.getLogger("crawlrx.fp_profiles")

_FP_GENERATE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError, ValueError, KeyError, TypeError, AttributeError,
    RuntimeError, LookupError, json.JSONDecodeError, UnicodeDecodeError,
)


def _synthetic_profile() -> dict:
    """Deterministic-shape fallback profile when generation fails."""
    return {
        "ua": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 Chrome/12{random.randint(0,4)}.0.0.0 Safari/537.36"
        ),
        "width": random.choice([1920, 1440, 1536, 1366]),
        "height": random.choice([1080, 900, 864, 768]),
        "language": "en-US",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "hardware_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
    }


def _profile_from_browserforge(fg: FingerprintGenerator) -> dict:
    """Render one browserforge fingerprint into a profile dict."""
    fp = fg.generate(browser="chrome", os="windows")
    return {
        "ua": fp.navigator.user_agent,
        "width": fp.screen.width,
        "height": fp.screen.height,
        "language": "en-US",
        "platform": "Win32",
        "vendor": getattr(fp.navigator, "vendor", "Google Inc."),
        "hardware_concurrency": getattr(fp.navigator, "hardware_concurrency", 8),
        "device_memory": getattr(fp.navigator, "device_memory", 8),
    }


def _generate_profiles(n: int = 20) -> list[dict]:
    """Generate n correlated device profiles using browserforge."""
    fg = FingerprintGenerator()
    profiles: list[dict] = []
    for _ in range(n):
        try:
            profiles.append(_profile_from_browserforge(fg))
        except _FP_GENERATE_EXCEPTIONS as exc:
            logger.debug("browserforge generate failed (%s)", type(exc).__name__)
            profiles.append(_synthetic_profile())
    return profiles


def load_profiles() -> list[dict]:
    """Load profiles from cache or generate new ones."""
    if PROFILE_CACHE.exists():
        return json.loads(PROFILE_CACHE.read_text())
    profiles = _generate_profiles(20)
    PROFILE_CACHE.write_text(json.dumps(profiles, indent=2))
    return profiles


def get_profile(seed: str | int | None = None) -> dict:
    """Random profile, or deterministic when ``seed`` is supplied."""
    profiles = load_profiles()
    if seed:
        return profiles[hash(seed) % len(profiles)]
    return random.choice(profiles)
