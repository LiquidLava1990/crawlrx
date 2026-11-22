"""DataDome browser strategies — Camoufox + Botright + Playwright (stealth).

Split out of ``datadome_solver.py``. All public helpers take the
solver instance as their first argument so the orchestrator can stay
thin while keeping access to ``self.fingerprint_manager``,
``self.cookie_store``, etc.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from datadome_solver import DataDomeSolver  # noqa: F401
    from playwright.async_api import (
        Browser as PlaywrightBrowser,
        BrowserContext as PlaywrightBrowserContext,
        Page as PlaywrightPage,
    )


class _Closable(Protocol):
    """Anything with an awaitable ``close()``; used by cleanup helpers."""

    async def close(self) -> None: ...


class _CookieStore(Protocol):
    """Subset of the cookie-store the bypass helper writes through."""

    def set_cookie(self, url: str, name: str, value: str) -> None: ...


_CookieList = List[Dict[str, Any]]
_ExtractCookie = Callable[[_CookieList], Optional[Dict[str, Any]]]


# --- ErrCode/emit shim -----------------------------------------------------

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

from ._solver_helpers import BROAD_EXC as _BROAD_EXC

from ..models import TaskResult  # noqa: E402
from playwright.async_api import async_playwright


logger = logging.getLogger(__name__)


# --- Generic browser-flow helpers -----------------------------------------


async def close_quietly(target: Optional[_Closable]) -> None:
    """Close ``target`` swallowing failures (cleanup-only)."""
    if target is None:
        return
    try:
        await target.close()
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def cleanup_browser_chain(
    page: Optional[PlaywrightPage],
    context: Optional[PlaywrightBrowserContext],
    browser: Optional[PlaywrightBrowser],
) -> None:
    """Close page -> context -> browser in order, swallowing errors."""
    await close_quietly(page)
    await close_quietly(context)
    await close_quietly(browser)


def bypass_taskresult(
    url: str,
    cookies: _CookieList,
    start_time: float,
    cookie_store: _CookieStore,
    extract_datadome_cookie: _ExtractCookie,
) -> TaskResult:
    """Persist cookie and build a success TaskResult."""
    datadome_cookie = extract_datadome_cookie(cookies)
    if datadome_cookie:
        cookie_store.set_cookie(url, datadome_cookie["name"], datadome_cookie["value"])
    return TaskResult(
        success=True,
        token=datadome_cookie["value"] if datadome_cookie else "bypassed",
        solved_at=datetime.now(tz=timezone.utc),
        solve_time_ms=int((time.time() - start_time) * 1000),
    )


@dataclass(frozen=True)
class NavTargets:
    """Bundle the page + browser-context pair used during nav/check passes."""

    page: Any
    context: Any


async def navigate_and_check(
    solver: "DataDomeSolver",
    targets: "NavTargets",
    url: str,
    wait_until: str = "networkidle",
    recheck_after: bool = True,
) -> "tuple[float, Any, bool]":
    """Goto + content check. Returns ``(start_time, cookies_or_none, bypassed)``."""
    page, context = targets.page, targets.context
    start_time = time.time()
    await page.goto(url, wait_until=wait_until, timeout=solver.timeout * 1000)
    html = await page.content()
    if not solver.is_datadome_protected(html):
        cookies = await context.cookies()
        return start_time, cookies, True
    if not recheck_after:
        return start_time, None, False
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Recheck wait failed: %s", e)
        return start_time, None, False
    html = await page.content()
    if not solver.is_datadome_protected(html):
        cookies = await context.cookies()
        return start_time, cookies, True
    return start_time, None, False


# --- Strategy 3: Camoufox -------------------------------------------------


def init_camoufox(solver: "DataDomeSolver") -> bool:
    """Initialize Camoufox if available; cache outcome on the solver."""
    if solver._camoufox_available and solver._camoufox_fp:
        return True
    try:
        try:
            from camoufox import AsyncCamoufox
            solver._camoufox_fp = AsyncCamoufox
            solver._camoufox_available = True
            logger.info("Camoufox is available")
            return True
        except ImportError:
            logger.warning("Camoufox not installed (pip install camoufox && camoufox fetch)")
            return False
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Failed to initialize Camoufox: %s", e)
        return False


async def try_camoufox_strategy(
    solver: "DataDomeSolver", url: str, domain: str,
) -> Optional[Any]:
    """Try solving with Camoufox - best fingerprint spoofing."""
    if not solver.strategies["camoufox"]:
        return None
    if not init_camoufox(solver):
        return None
    logger.info("Trying Camoufox strategy for %s", url)
    browser = None
    context = None
    page = None
    try:
        fingerprint = solver.fingerprint_manager.get_fingerprint(domain)
        async with solver._camoufox_fp(
            headless=solver.headless,
            proxy=solver.proxy,
            fingerprint={
                "os": "windows" if fingerprint["platform"] == "Win32" else "macos",
                "screen": fingerprint["viewport"],
                "timezone": fingerprint["timezone"],
                "locale": fingerprint["locale"],
            },
        ) as browser:
            context = await browser.new_context(
                viewport=fingerprint["viewport"],
                user_agent=fingerprint["user_agent"],
                locale=fingerprint["locale"],
                timezone_id=fingerprint["timezone"],
            )
            page = await context.new_page()
            start_time, cookies, ok = await navigate_and_check(
                solver, NavTargets(page, context), url, wait_until="networkidle",
            )
            if ok:
                logger.info("Camoufox bypass succeeded")
                await cleanup_browser_chain(page, context, browser)
                return bypass_taskresult(
                    url, cookies, start_time,
                    solver.cookie_store, solver.extract_datadome_cookie,
                )
            await cleanup_browser_chain(page, context, browser)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Camoufox strategy failed: %s", e)
        return None
    return None


# --- Strategy 4: Botright -------------------------------------------------


def _try_import_botright() -> Any:
    """Return ``botright`` module or ``None`` if not installed."""
    try:
        import botright
        return botright
    except ImportError:
        logger.warning("Botright not installed (pip install botright)")
        return None


async def _botright_recheck(
    solver: "DataDomeSolver", page: Any, browser: Any,
) -> "tuple[Any, bool]":
    """Run the post-load networkidle re-check; return ``(cookies, ok)``."""
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
    html = await page.content()
    if solver.is_datadome_protected(html):
        return None, False
    cookies = await browser.context.cookies()
    return cookies, True


async def _close_botright(browser: Any, bot: Any) -> None:
    """Close every botright page + browser + driver instance."""
    for pg in browser.context.pages:
        await close_quietly(pg)
    await close_quietly(browser)
    try:
        await bot.stop()
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _stop_bot_quiet(bot: Any) -> None:
    """``bot.stop()`` swallowing failures; for cleanup paths only."""
    try:
        await bot.stop()
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _run_botright_pass(
    solver: "DataDomeSolver", url: str, domain: str, bot: Any,
) -> Optional[Any]:
    """Drive one Botright nav + check + cleanup."""
    fingerprint = solver.fingerprint_manager.get_fingerprint(domain)
    browser = await bot.new_browser(
        user_agent=fingerprint["user_agent"],
        viewport=fingerprint["viewport"],
    )
    page = await browser.new_page()
    start_time, cookies, ok = await navigate_and_check(
        solver, NavTargets(page, browser.context), url, recheck_after=False,
    )
    if not ok:
        cookies, ok = await _botright_recheck(solver, page, browser)
    result = (
        bypass_taskresult(
            url, cookies, start_time,
            solver.cookie_store, solver.extract_datadome_cookie,
        )
        if ok else None
    )
    if ok:
        logger.info("Botright strategy succeeded")
    await _close_botright(browser, bot)
    return result


async def try_botright_strategy(
    solver: "DataDomeSolver", url: str, domain: str,
) -> Optional[Any]:
    """Try solving with Botright as fallback (real Chromium + stealth)."""
    if not solver.strategies["botright"]:
        return None
    logger.info("Trying Botright strategy for %s", url)
    botright_mod = _try_import_botright()
    if botright_mod is None:
        return None
    try:
        bot = await botright_mod.Botright(headless=solver.headless, proxy=solver.proxy)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Botright init failed: %s", e)
        return None
    try:
        return await _run_botright_pass(solver, url, domain, bot)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Botright strategy failed: %s", e)
        await _stop_bot_quiet(bot)
        return None


# --- Strategy 5: Playwright + stealth -------------------------------------

_PLAYWRIGHT_STEALTH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
    "--disable-features=BlockInsecurePrivateNetworkRequests",
    "--disable-accelerated-2d-canvas",
    "--disable-accelerated-jpeg-decoding",
    "--disable-accelerated-mjpeg-decode",
    "--disable-accelerated-video-decode",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
)


def _playwright_launch_args(fingerprint: Dict[str, Any]) -> List[str]:
    """Compose chromium launch args including viewport sizing."""
    return [
        f"--window-size={fingerprint['viewport']['width']},{fingerprint['viewport']['height']}",
        *_PLAYWRIGHT_STEALTH_ARGS,
    ]


def _playwright_context_options(
    solver: "DataDomeSolver", fingerprint: Dict[str, Any],
) -> Dict[str, Any]:
    """Compose ``new_context()`` kwargs with optional proxy."""
    opts: Dict[str, Any] = {
        "viewport": fingerprint["viewport"],
        "user_agent": fingerprint["user_agent"],
        "locale": fingerprint["locale"],
        "timezone_id": fingerprint["timezone"],
        "color_scheme": "light",
        "device_scale_factor": fingerprint["pixel_ratio"],
    }
    if solver.proxy:
        opts["proxy"] = {"server": solver.proxy}
    return opts


async def _stop_playwright(solver: "DataDomeSolver") -> None:
    """Stop the playwright driver swallowing failures."""
    if solver._playwright is None:
        return
    try:
        await solver._playwright.stop()
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


_STEALTH_SCRIPTS_STATIC = (
    """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });
    """,
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
            {name: 'Native Client', filename: 'internal-nacl-plugin'}
        ]
    });
    """,
    """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
    """,
    """
    const toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const getImageData = CanvasRenderingContext2D.prototype.getImageData;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const data = toDataURL.call(this, type);
        return data.slice(0, 20) + data.slice(20).split('').sort(() => 0.5 - Math.random()).join('');
    };
    """,
)


def _stealth_scripts_for(fingerprint: Dict[str, Any]) -> List[str]:
    """Return the per-page stealth-patch scripts."""
    webgl_patch = (
        "const getParameter = WebGLRenderingContext.prototype.getParameter;"
        "WebGLRenderingContext.prototype.getParameter = function(parameter) {"
        f"  if (parameter === 37445) return '{fingerprint['webgl_vendor']}';"
        f"  if (parameter === 37446) return '{fingerprint['webgl_renderer']}';"
        "  return getParameter(parameter);"
        "};"
    )
    return [*_STEALTH_SCRIPTS_STATIC, webgl_patch]


async def _apply_stealth_scripts(page: Any, fingerprint: Dict[str, Any]) -> None:
    """Apply stealth patches to hide automation."""
    for script in _stealth_scripts_for(fingerprint):
        try:
            await page.add_init_script(script)
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _run_playwright_pass(
    solver: "DataDomeSolver", url: str, domain: str,
) -> Optional[Any]:
    """Drive one Playwright nav + check + cleanup pass."""
    fingerprint = solver.fingerprint_manager.get_fingerprint(domain)
    solver._playwright = await async_playwright().start()
    browser = await solver._playwright.chromium.launch(
        headless=solver.headless,
        args=_playwright_launch_args(fingerprint),
    )
    context = await browser.new_context(
        **_playwright_context_options(solver, fingerprint),
    )
    page = await context.new_page()
    await _apply_stealth_scripts(page, fingerprint)
    start_time, cookies, ok = await navigate_and_check(
        solver, NavTargets(page, context), url, wait_until="networkidle",
    )
    result = (
        bypass_taskresult(
            url, cookies, start_time,
            solver.cookie_store, solver.extract_datadome_cookie,
        )
        if ok else None
    )
    if ok:
        logger.info("Playwright stealth strategy succeeded")
    await cleanup_browser_chain(page, context, browser)
    await _stop_playwright(solver)
    return result


async def try_playwright_strategy(
    solver: "DataDomeSolver", url: str, domain: str,
) -> Optional[Any]:
    """Fallback using standard Playwright with stealth patches."""
    logger.info("Trying Playwright stealth strategy for %s", url)
    try:
        return await _run_playwright_pass(solver, url, domain)
    except _BROAD_EXC as e:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
        logger.warning("Playwright strategy failed: %s", e)
        await _stop_playwright(solver)
        return None


__all__ = [
    "try_camoufox_strategy",
    "try_botright_strategy",
    "try_playwright_strategy",
    "init_camoufox",
    "navigate_and_check",
    "bypass_taskresult",
    "cleanup_browser_chain",
    "close_quietly",
    "NavTargets",
]
