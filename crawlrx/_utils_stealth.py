"""Stealth-mode helpers: viewport, headers, init script, browser context.

Covers the anti-bot fingerprint surface: random viewport, hardware
spec rotation, navigator-property override script, locale-aware
``Accept-*`` headers, and the BrowserContext factory.
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stealth knobs -- timezones, hardware, viewports
# ---------------------------------------------------------------------------

STEALTH_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
]

STEALTH_HARDWARE_CONCURRENCY = [4, 8, 16]
STEALTH_DEVICE_MEMORY = [4, 8]

STEALTH_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]


def stealth_viewport() -> dict:
    """Return one of the approved stealth viewports at random."""
    return random.choice(STEALTH_VIEWPORTS)


# ---------------------------------------------------------------------------
# Init script -- override navigator + chrome to mask automation
# ---------------------------------------------------------------------------

_JS_NAVIGATOR_OVERRIDES = """
    // Override navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });

    // Override navigator.plugins to look like a real browser
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: "Chrome PDF Plugin", filename: "internal-pdf-viewer"},
            {name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai"},
            {name: "Native Client", filename: "internal-nacl-plugin"}
        ]
    });

    // Override plugins.length
    Object.defineProperty(navigator.plugins, 'length', {
        get: () => 3
    });

    // Override navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
"""

_JS_CHROME_OBJECT = """
    // Fake chrome object (only present in Chrome)
    window.chrome = {
        runtime: {
            OnInstalledReason: {CHROME_UPDATE: "chrome_update", EXTENSION_UPDATE: "extension_update", INSTALL: "install", SHARED_MODULE_UPDATE: "shared_module_update", UPDATE: "update"},
            OnRestartRequiredReason: {APP_UPDATE: "app_update", OS_UPDATE: "os_update", PERIODIC: "periodic"},
            PlatformArch: {ARM: "arm", ARM64: "arm64", MIPS: "mips", MIPS64: "mips64", MIPS64EL: "mips64el", MIPSEL: "mipsel", X86_32: "x86-32", X86_64: "x86-64"},
            PlatformNaclArch: {ARM: "arm", MIPS: "mips", MIPS64: "mips64", MIPS64EL: "mips64el", MIPSEL: "mipsel", MIPSEL64: "mipsel64", X86_32: "x86-32", X86_64: "x86-64"},
            PlatformOs: {ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", OPENBSD: "openbsd", WIN: "win"},
            RequestUpdateCheckStatus: {NO_UPDATE: "no_update", THROTTLED: "throttled", UPDATE_AVAILABLE: "update_available"}
        },
        loadTimes: () => {
            return {
                connectionInfo: "h2",
                npnNegotiatedProtocol: undefined,
                requestTime: Date.now() / 1000 - Math.random() * 5,
                startLoadTime: Date.now() / 1000 - Math.random() * 3,
                commitLoadTime: Date.now() / 1000 - Math.random() * 2,
                finishDocumentLoadTime: Date.now() / 1000 - Math.random() * 1,
                firstPaintAfterLoadTime: Date.now() / 1000,
                finishLoadTime: Date.now() / 1000
            };
        },
        csi: () => {
            return {
                startE: Date.now(),
                onloadT: Date.now(),
                pageT: Date.now() - performance.timing.navigationStart,
                tran: 15
            };
        }
    };
"""

_JS_NOTIFICATION_AND_PERMISSIONS = """
    // Override Notification.permission
    const originalNotification = window.Notification;
    Object.defineProperty(window, 'Notification', {
        get: function() {
            return originalNotification;
        }
    });
    Object.defineProperty(Notification, 'permission', {
        get: () => "default"
    });

    // Clean up automation traces
    delete navigator.__proto__.webdriver;

    // Override Permissions API
    if (window.navigator.permissions) {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    }
"""


def _js_hardware_overrides(hardware_concurrency: int, device_memory: int) -> str:
    """Render the per-call randomized hardwareConcurrency / deviceMemory overrides."""
    return f"""
    // Randomize hardwareConcurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', {{
        get: () => {hardware_concurrency}
    }});

    // Randomize deviceMemory
    Object.defineProperty(navigator, 'deviceMemory', {{
        get: () => {device_memory}
    }});
"""


def _get_stealth_init_script() -> str:
    """Compose the JS init script with randomized hardware specs."""
    hardware_concurrency = random.choice(STEALTH_HARDWARE_CONCURRENCY)
    device_memory = random.choice(STEALTH_DEVICE_MEMORY)
    return (
        _JS_NAVIGATOR_OVERRIDES
        + _js_hardware_overrides(hardware_concurrency, device_memory)
        + _JS_CHROME_OBJECT
        + _JS_NOTIFICATION_AND_PERMISSIONS
    )


# ---------------------------------------------------------------------------
# Header + context-kwargs builders
# ---------------------------------------------------------------------------


def _build_default_headers(locale: str) -> Dict[str, str]:
    """Default ``Accept-*`` / ``DNT`` / ``Upgrade-Insecure`` headers."""
    return {
        "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "Upgrade-Insecure-Requests": "1",
    }


def _build_stealth_ctx_kwargs(
    ua: str,
    locale: str,
    viewport: dict,
    extra_http_headers: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Build the kwargs dict that ``browser.new_context`` expects."""
    headers = _build_default_headers(locale)
    if extra_http_headers:
        headers.update(extra_http_headers)
    return {
        "user_agent": ua,
        "locale": locale,
        "timezone_id": random.choice(STEALTH_TIMEZONES),
        "viewport": viewport,
        "java_script_enabled": True,
        "extra_http_headers": headers,
    }


async def create_stealth_context(
    browser: "Browser | BrowserContext | Page | None",
    user_agent: Optional[str] = None,
    locale: str = "en-US",
    viewport: Optional[dict] = None,
    extra_http_headers: Optional[Dict[str, str]] = None,
) -> "BrowserContext":
    """Create a browser context with anti-bot (stealth) settings."""
    from .config import USER_AGENTS
    ua = user_agent or random.choice(USER_AGENTS)
    if viewport is None:
        viewport = stealth_viewport()
    ctx_kwargs = _build_stealth_ctx_kwargs(ua, locale, viewport, extra_http_headers)
    context = await browser.new_context(**ctx_kwargs)
    await context.add_init_script(_get_stealth_init_script())
    logger.debug(
        "Created stealth context: UA=%s... viewport=%dx%d timezone=%s",
        ua[:50], viewport["width"], viewport["height"], ctx_kwargs["timezone_id"],
    )
    return context
