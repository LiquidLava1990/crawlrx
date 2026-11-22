"""stealth_context.py -- Self-contained anti-detection context factory for CrawlRX.

Fully async -- uses ``async_playwright`` for FastAPI compatibility.

Exports:
  await make_stealth_context(playwright_instance=None, domain=None) -> (browser, ctx, page)
  await close_stealth_context(browser, ctx, page)
  stealth_context(domain=None)  -- async context manager yielding page
  await load_synced_cookies(context, domain=None) -- load cookies from Smart Cookie Sync
  make_camoufox_page() -> (camoufox_browser, page)
  close_camoufox_page(browser, page)
"""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import async_playwright

from ._scraper_helpers import PLAYWRIGHT_EXCEPTIONS
from .fp_profiles import get_profile
from .paths import env_path

logger = logging.getLogger("crawlrx.stealth")

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

# Cookie domain mappings for common scraper targets
COOKIE_DOMAINS = {
    "zillow": ".zillow.com",
    "linkedin": ".linkedin.com",
    "indeed": ".indeed.com",
    "amazon": ".amazon.com",
    "ebay": ".ebay.com",
    "facebook": ".facebook.com",
    "twitter": ".twitter.com",
    "x": ".x.com",
    "instagram": ".instagram.com",
    "reddit": ".reddit.com",
    "youtube": ".youtube.com",
    "google": ".google.com",
    "glassdoor": ".glassdoor.com",
    "yelp": ".yelp.com",
}

# Common paths where Smart Cookie Sync may store cookies
_COOKIE_PATHS = [
    Path(__file__).parent / "cookies" / "cookies.json",
    Path(__file__).parent / "data" / "cookies.json",
    Path(env_path("CRAWLRX_COOKIES_FILE", "cookies.json")),
    Path.home() / ".crawlrx" / "cookies.json",
    Path.home() / ".crawlrx" / "cookies" / "cookies.json",
]

# Narrow exception families for cookie + cleanup paths.
_COOKIE_LOAD_EXCEPTIONS: tuple[type[BaseException], ...] = (
    json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError,
)
_CLEANUP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *PLAYWRIGHT_EXCEPTIONS,
    OSError, RuntimeError, ValueError, AttributeError,
)


async def _find_cookie_file() -> Optional[Path]:
    """Find the first available cookie file from common locations."""
    for path in _COOKIE_PATHS:
        if path.exists():
            return path
    return None


def _read_cookie_file(cookie_file: Path) -> Optional[list]:
    """Open ``cookie_file`` and return a list of cookies, or None on bad input."""
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except _COOKIE_LOAD_EXCEPTIONS as e:
        logger.debug("Failed to load cookies from %s: %s", cookie_file, e)
        return None
    if not isinstance(cookies, list):
        logger.debug("Invalid cookie format in %s: expected list", cookie_file)
        return None
    return cookies


def _filter_cookies_for_domain(
    cookies: list,
    domain: Optional[str],
) -> list:
    """Return cookies that match (or relate to) ``domain``."""
    if domain and domain in COOKIE_DOMAINS:
        domain = COOKIE_DOMAINS[domain]
    filtered: list = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        cookie_domain = cookie.get("domain", "")
        if domain and not (
            cookie_domain.endswith(domain) or domain.endswith(cookie_domain)
        ):
            continue
        filtered.append(cookie)
    return filtered


async def load_synced_cookies(context: "BrowserContext | None", domain: Optional[str] = None) -> None:
    """Load synced cookies from Smart Cookie Sync into ``context``.

    Cookies are optional -- silently returns if no cookie file exists.
    """
    cookie_file = await _find_cookie_file()
    if not cookie_file:
        return
    cookies = _read_cookie_file(cookie_file)
    if cookies is None:
        return
    filtered_cookies = _filter_cookies_for_domain(cookies, domain)
    if not filtered_cookies:
        logger.debug("No cookies found for domain %s", domain)
        return
    try:
        await context.add_cookies(filtered_cookies)
        logger.debug(
            "Loaded %d cookies for domain %s",
            len(filtered_cookies), domain or "all",
        )
    except _CLEANUP_EXCEPTIONS as e:
        logger.debug("Failed to add cookies to context: %s", e)


async def _make_context_and_page(browser: Any, domain: Optional[str] = None):
    """Create an isolated stealth context + page on the given browser."""
    profile = get_profile(seed=domain)
    ctx = await browser.new_context(
        user_agent=profile["ua"],
        viewport={"width": profile["width"], "height": profile["height"]},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
        java_script_enabled=True,
    )
    page = await ctx.new_page()
    try:
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)
    except _CLEANUP_EXCEPTIONS as e:
        logger.warning("playwright-stealth failed: %s", e)
        return None  # explicit recovery: non-fatal: _CLEANUP_EXCEPTIONS is recorded but does not abort the caller
    except ImportError as e:
        logger.warning("playwright-stealth not available: %s", e)
        return None  # explicit recovery: non-fatal: ImportError is recorded but does not abort the caller

    await page.add_init_script(
        f"""
        Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {profile['hardware_concurrency']}}});
        Object.defineProperty(navigator, 'deviceMemory', {{get: () => {profile['device_memory']}}});
        Object.defineProperty(navigator, 'platform', {{get: () => '{profile['platform']}'}});
        Object.defineProperty(navigator, 'vendor', {{get: () => '{profile['vendor']}'}});
        """
    )
    return ctx, page


async def _close_objects(pairs: list, label_prefix: str) -> None:
    """Close a sequence of ``(obj, label)`` pairs, swallowing cleanup errors."""
    for obj, label in pairs:
        if not obj:
            continue
        try:
            await obj.close()
        except _CLEANUP_EXCEPTIONS as e:
            logger.debug("%s close %s: %s", label_prefix, label, e)


async def make_stealth_context(
    playwright_instance: Optional[Any] = None,
    domain: Optional[str] = None,
    use_cookies: bool = True,
) -> object:
    """Launch a fresh browser and return ``(browser, ctx, page)``.

    If ``playwright_instance`` is None, a new playwright instance is started
    and tagged on ``browser._crawlrx_pw`` so :func:`close_stealth_context`
    can stop it. Caller is responsible for closing.
    """
    if playwright_instance is not None:
        browser = await playwright_instance.chromium.launch(
            headless=True, args=_LAUNCH_ARGS,
        )
    else:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        browser._crawlrx_pw = pw  # type: ignore[attr-defined] because stash playwright handle on browser for later teardown; playwright Browser has no slot for this
    ctx, page = await _make_context_and_page(browser, domain)
    if use_cookies:
        await load_synced_cookies(ctx, domain=domain)
    return browser, ctx, page


async def close_stealth_context(browser: "Browser | BrowserContext | Page | None", ctx: "BrowserContext | None", page: "Page") -> None:
    """Close page, context, and browser cleanly. Stops internal playwright."""
    await _close_objects(
        [(page, "page"), (ctx, "ctx"), (browser, "browser")],
        "close",
    )
    pw = getattr(browser, "_crawlrx_pw", None) if browser is not None else None
    if pw is None:
        return
    try:
        await pw.stop()
    except _CLEANUP_EXCEPTIONS as e:
        logger.debug("close playwright instance: %s", e)


@asynccontextmanager
async def stealth_context(
    domain: Optional[str] = None,
    use_cookies: bool = True,
) -> object:
    """Async context manager: yields a stealth ``page`` from a fresh browser."""
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    ctx, page = await _make_context_and_page(browser, domain)
    if use_cookies:
        await load_synced_cookies(ctx, domain=domain)
    try:
        yield page
    finally:
        await _close_objects(
            [(page, "page"), (ctx, "ctx"), (browser, "browser")],
            "stealth_context",
        )
        try:
            await p.stop()
        except _CLEANUP_EXCEPTIONS as e:
            logger.debug("stealth_context stop playwright: %s", e)


async def make_camoufox_page() -> object:
    """Launch a camoufox browser and return ``(browser, page)``."""
    try:
        from camoufox.async_api import AsyncCamoufox
        browser = await AsyncCamoufox(headless=True, humanize=True)
        page = await browser.new_page()
        return browser, page
    except ImportError:
        import asyncio
        from camoufox.sync_api import Camoufox
        browser = await asyncio.to_thread(Camoufox, headless=True, humanize=True)
        page = await asyncio.to_thread(browser.new_page)
        return browser, page


async def close_camoufox_page(browser: "Browser | BrowserContext | Page | None", page: "Page") -> None:
    """Close camoufox page and browser."""
    await _close_objects(
        [(page, "camoufox page"), (browser, "camoufox browser")],
        "close",
    )
