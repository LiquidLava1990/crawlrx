"""Stealth browser launch + per-page stealth-script injection + cookie I/O.

Extracted from ``stealth_browser.py``. The pool delegates to these helpers
so it stays a thin lifecycle coordinator.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from playwright.async_api import Page

if TYPE_CHECKING:
    from ._stealth_browser_pool import BrowserInstance  # noqa: F401

from ._stealth_browser_bootstrap import ErrCode, emit
from ._solver_helpers import BROAD_EXC

# Stealth-async fallback chain (same as the original).
try:
    from playwright_stealth import stealth_async
except ImportError:
    try:
        from playwright_stealth import Stealth

        async def stealth_async(page: object) -> None:
            s = Stealth()
            await s.apply(page)
    except (ImportError, AttributeError):
        emit(ErrCode.IMPORT_ERROR, "unhandled")

        def stealth_async(page: object) -> None:  # type: ignore[no-redef]: redefined because playwright_stealth multi-version fallback
            return None  # no-op fallback


from ._stealth_browser_fingerprint import BrowserFingerprint, generate_fingerprint

logger = logging.getLogger("stealth_browser")


def build_launch_args(fingerprint: BrowserFingerprint) -> list:
    """Chromium launch args needed for stealth + reduced sandboxing."""
    return [
        f"--window-size={fingerprint.viewport_width},{fingerprint.viewport_height}",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--no-sandbox",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-site-isolation-trials",
        "--disable-features=BlockInsecurePrivateNetworkRequests",
    ]


def build_context_options(
    fingerprint: BrowserFingerprint, proxy: Optional[str],
) -> Dict[str, Any]:
    """BrowserContext kwargs derived from a fingerprint + optional proxy."""
    options: Dict[str, Any] = {
        "viewport": {
            "width": fingerprint.viewport_width,
            "height": fingerprint.viewport_height,
        },
        "user_agent": fingerprint.user_agent,
        "locale": fingerprint.locale,
        "timezone_id": fingerprint.timezone,
        "color_scheme": "light",
        "device_scale_factor": fingerprint.pixel_ratio,
    }
    if proxy:
        options["proxy"] = {"server": proxy}
    return options


async def close_existing_browser(instance: "BrowserInstance") -> None:
    """Close the browser/context attached to ``instance`` before relaunching."""
    if instance.context:
        await instance.context.close()
    if instance.browser:
        await instance.browser.close()


async def launch_instance(
    instance: "BrowserInstance",
    *,
    playwright: "Any",
    proxy: Optional[str],
    background_tasks: set,
) -> BrowserFingerprint:
    """Start the browser/context for ``instance`` and wire the per-page stealth hook.

    Returns the fingerprint actually applied so the caller can stamp it on
    the instance + log the viewport.
    """
    await close_existing_browser(instance)
    fingerprint = generate_fingerprint()
    instance.fingerprint = fingerprint
    instance.browser = await playwright.chromium.launch(
        headless=True, args=build_launch_args(fingerprint),
    )
    instance.context = await instance.browser.new_context(
        **build_context_options(fingerprint, proxy),
    )
    await load_cookies(instance)

    def _on_new_page(page: Page, fp: BrowserFingerprint = fingerprint) -> None:
        task = asyncio.create_task(apply_stealth_scripts(page, fp))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    instance.context.on("page", _on_new_page)
    instance.is_ready = True
    instance.health_failures = 0
    instance.last_used = datetime.now(tz=timezone.utc)
    return fingerprint


def _navigator_patch_script(locale: str) -> str:
    """JS that hides webdriver, normalises permissions, plugins, languages."""
    return (
        """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });

            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );

            delete window.chrome?.runtime?.OnInstalledReason;
            delete window.chrome?.runtime?.OnRestartRequiredReason;

            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            Object.defineProperty(navigator, 'languages', {
                get: () => ['""" + locale + """', 'en-US', 'en']
            });
        """
    )


def _webgl_spoof_script(vendor: str, renderer: str) -> str:
    """JS that spoofs WEBGL_debug_renderer_info parameters 37445/37446."""
    return f"""
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                if (parameter === 37445) {{ return '{vendor}'; }}
                if (parameter === 37446) {{ return '{renderer}'; }}
                return getParameter(parameter);
            }};"""


_CANVAS_NOISE_SCRIPT = """
            const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
            const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

            HTMLCanvasElement.prototype.toDataURL = function(type) {
                if (this.width > 16 && this.height > 16) {
                    const ctx = this.getContext('2d');
                    const imageData = ctx.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] = (imageData.data[i] + 1) % 256;
                    }
                    ctx.putImageData(imageData, 0, 0);
                }
                return originalToDataURL.call(this, type);
            };
        """


async def apply_stealth_scripts(page: Page, fingerprint: BrowserFingerprint) -> None:
    """Apply stealth patches to hide automation indicators on every new page."""
    await page.add_init_script(_navigator_patch_script(fingerprint.locale))
    await page.add_init_script(
        _webgl_spoof_script(fingerprint.webgl_vendor, fingerprint.webgl_renderer),
    )
    await page.add_init_script(_CANVAS_NOISE_SCRIPT)
    await stealth_async(page)


async def load_cookies(
    instance: "BrowserInstance",
    cookie_file: Path = Path("stealth_cookies.json"),
) -> None:
    """Load saved cookies from disk into ``instance.context`` (best-effort)."""
    try:
        if cookie_file.exists():
            with open(cookie_file, "r", encoding="utf-8-sig", errors="replace") as f:
                cookies = json.load(f)
            if cookies:
                await instance.context.add_cookies(cookies)
                logger.debug(
                    "Loaded %s cookies for instance %s", len(cookies), instance.id,
                )
    except BROAD_EXC as e:
        emit(ErrCode.FILE_READ_ERROR, str(e))
        logger.warning("Failed to load cookies: %s", e)
        return None  # explicit recovery: cookie load is non-fatal


async def save_cookies(
    instance: "BrowserInstance",
    cookie_file: Path = Path("stealth_cookies.json"),
) -> None:
    """Persist cookies from ``instance.context`` to disk (best-effort)."""
    try:
        cookies = await instance.context.cookies()
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        logger.debug(
            "Saved %s cookies from instance %s", len(cookies), instance.id,
        )
    except BROAD_EXC as e:
        emit(ErrCode.FILE_WRITE_FAIL, str(e))
        logger.warning("Failed to save cookies: %s", e)
        return None  # explicit recovery: cookie save is non-fatal
