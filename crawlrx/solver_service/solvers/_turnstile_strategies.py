"""Turnstile strategy plumbing — driver factories and token pollers.

Split out of `turnstile_solver.py` so that file stays under the project's
500-line ceiling. The four solver strategies share a common
`SolverAttemptSpec` shape (defined in this module) and differ only in:

  * `driver_factory` — how to launch the browser stack
  * `poll_for_token` — how to detect a successful solve

This module owns those callbacks and the narrowed exception tuples.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Tuple

from playwright.async_api import Error as PlaywrightError, Page, async_playwright


if TYPE_CHECKING:
    from .turnstile_solver import TurnstileSolver  # noqa: F401


    from playwright.async_api import Browser, BrowserContext, ElementHandle, Playwright
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Narrowed exception tuples — used in place of bare `except Exception`.
# Verdict per Layer-3 AST analysis remains `swallow` — handlers below set
# `result.error` / log / pass; they do NOT re-raise. Preserves the
# intentional "try next strategy" fallback semantics.
# ---------------------------------------------------------------------------
BROWSER_ERRORS: Tuple[type, ...] = (
    PlaywrightError,
    ImportError,
    RuntimeError,
    OSError,
    AttributeError,
    TypeError,
    ValueError,
    ConnectionError,
    TimeoutError,
    asyncio.CancelledError,
    asyncio.TimeoutError,
)
CLEANUP_ERRORS: Tuple[type, ...] = (
    RuntimeError,
    OSError,
    AttributeError,
    ConnectionError,
)
DOM_QUERY_ERRORS: Tuple[type, ...] = (
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
)


@dataclass
class SolverAttemptSpec:
    """Bundle of inputs required to drive a single solver attempt.

    Replaces the four previous `_try_*_solve(page_url, site_key, cached_cookies)`
    parameter triples. Strategy-specific behaviour is supplied via
    `driver_factory` (returns a (browser, context, page) tuple) and
    `poll_for_token` (extracts the token from a live page).
    """

    page_url: str
    site_key: Optional[str]
    cached_cookies: Optional[List[Dict]]
    strategy_name: str
    driver_factory: Callable[
        ["TurnstileSolver", "SolverAttemptSpec"],
        Awaitable[Tuple[Any, Any, Page]],
    ]
    poll_for_token: Callable[
        ["TurnstileSolver", Page],
        Awaitable[Optional[str]],
    ]
    pre_navigate_inject: Optional[
        Callable[["TurnstileSolver", Page], Awaitable[None]]
    ] = None
    post_navigate_pause_s: float = 2.0
    no_token_error: str = "strategy did not produce token"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyAttempt:
    """Inputs for `_attempt_strategy` — keeps that callsite under the 5-param limit."""

    strategy_name: str
    strategy_func: Callable[
        [str, Optional[str], Optional[List[Dict]]],
        Awaitable[Any],
    ]
    page_url: str
    site_key: Optional[str]
    cached_cookies: Optional[List[Dict]]
    timeout_s: float


async def safe_close(obj: "Page | BrowserContext | Browser | None") -> None:
    """Best-effort close that swallows shutdown errors."""
    if obj is None:
        return
    try:
        await obj.close()
    except CLEANUP_ERRORS:
        logger.debug("safe_close swallowed cleanup error")


async def get_cookies(context: "BrowserContext | None", browser: "Browser | BrowserContext | Page | None") -> List[Dict]:
    """Extract cookies from whichever container has them."""
    if context is not None and hasattr(context, 'cookies'):
        return await context.cookies()
    if browser is not None and getattr(browser, 'context', None) is not None:
        return await browser.context.cookies()
    return []


# ---------------------------------------------------------------------------
# Driver factories — one per browser stack
# ---------------------------------------------------------------------------


async def botright_driver_factory(
    solver: "TurnstileSolver", spec: SolverAttemptSpec
) -> Tuple[Any, Any, Page]:
    """Spin up a Botright browser, return (browser, context, page)."""
    import botright
    botright_client = await botright.Botright(
        headless=solver.headless,
        proxy=solver.proxy,
    )
    spec.extra['_botright_client'] = botright_client
    browser = await botright_client.new_browser()
    context = browser.context
    if spec.cached_cookies:
        await context.add_cookies(spec.cached_cookies)
    page = await browser.new_page()
    return browser, context, page


async def camoufox_driver_factory(
    solver: "TurnstileSolver", spec: SolverAttemptSpec
) -> Tuple[Any, Any, Page]:
    """Spin up a Camoufox browser, return (browser, context, page)."""
    from camoufox.async_api import AsyncCamoufox
    camoufox = AsyncCamoufox(
        headless=solver.headless,
        proxy=solver.proxy,
        humanize=True,
    )
    spec.extra['_camoufox'] = camoufox
    browser = await camoufox.__aenter__()
    context = await browser.new_context()
    if spec.cached_cookies:
        await context.add_cookies(spec.cached_cookies)
    page = await context.new_page()
    return browser, context, page


async def _playwright_driver(
    solver: "TurnstileSolver",
    spec: SolverAttemptSpec,
    *,
    use_proxy: bool,
) -> Tuple[Any, Any, Page]:
    """Shared Playwright stealth driver setup."""
    pw_ctx = await async_playwright().start()
    spec.extra['_pw_ctx'] = pw_ctx
    browser = await pw_ctx.chromium.launch(
        headless=solver.headless,
        args=solver._get_stealth_args(),
    )
    context_kwargs: Dict[str, Any] = {
        'viewport': {'width': 1920, 'height': 1080},
        'user_agent': solver._get_stealth_ua(),
    }
    if use_proxy and solver.proxy:
        context_kwargs['proxy'] = {'server': solver.proxy}
    context = await browser.new_context(**context_kwargs)
    if spec.cached_cookies:
        await context.add_cookies(spec.cached_cookies)
    page = await context.new_page()
    return browser, context, page


async def playwright_driver_factory(
    solver: "TurnstileSolver", spec: SolverAttemptSpec
) -> Tuple[Any, Any, Page]:
    """Stealth Playwright with proxy when configured."""
    return await _playwright_driver(solver, spec, use_proxy=True)


async def playwright_driver_factory_basic(
    solver: "TurnstileSolver", spec: SolverAttemptSpec
) -> Tuple[Any, Any, Page]:
    """Stealth Playwright without proxy (cookie replay/refresh)."""
    return await _playwright_driver(solver, spec, use_proxy=False)


# ---------------------------------------------------------------------------
# Token pollers — one per strategy shape
# ---------------------------------------------------------------------------


async def poll_token_default(
    solver: "TurnstileSolver", page: Page
) -> Optional[str]:
    """Poll DOM 20x at 1.5s intervals (botright + cookie_refresh)."""
    for _ in range(20):
        token = await solver._extract_turnstile_token(page)
        if token:
            return token
        await asyncio.sleep(1.5)
    return None


async def poll_token_camoufox(
    solver: "TurnstileSolver", page: Page
) -> Optional[str]:
    """Camoufox: alternate sleep based on visible challenge."""
    for _ in range(30):
        token = await solver._extract_turnstile_token(page)
        if token:
            return token
        challenge_visible = await solver._is_challenge_visible(page)
        await asyncio.sleep(1.0 if challenge_visible else 0.5)
    return None


async def poll_token_harvest(
    solver: "TurnstileSolver", page: Page
) -> Optional[str]:
    """Token harvest: try DOM, JS globals, and intercepted callback."""
    for _ in range(40):
        token = await solver._extract_turnstile_token(page)
        if token:
            return token
        token = await _evaluate_global_token_locations(page)
        if token:
            return token
        token = await page.evaluate("() => window.__capturedTurnstileToken || null")
        if token:
            return token
        await asyncio.sleep(1.0)
    return None


async def poll_token_cookie_replay(
    solver: "TurnstileSolver", page: Page
) -> Optional[str]:
    """Cookie replay: returns 'COOKIE_AUTH' sentinel if challenge is gone."""
    if await solver._is_turnstile_present(page):
        return None
    token = await solver._extract_turnstile_token(page)
    return token or "COOKIE_AUTH"


async def _evaluate_global_token_locations(page: Page) -> Optional[str]:
    """JS sweep across common Turnstile token storage spots."""
    return await page.evaluate(
        """
        () => {
            if (window.turnstileToken) return window.turnstileToken;
            if (window.cfTurnstileResponse) return window.cfTurnstileResponse;
            if (window.grecaptcha) return window.grecaptcha.getResponse?.();
            const inputs = document.querySelectorAll('input[name*="turnstile"]');
            for (const input of inputs) {
                if (input.value) return input.value;
            }
            const elems = document.querySelectorAll('[data-turnstile-sitekey]');
            for (const el of elems) {
                if (el.dataset.turnstileResponse) return el.dataset.turnstileResponse;
            }
            return null;
        }
        """
    )


async def inject_token_interceptor_adapter(
    solver: "TurnstileSolver", page: Page
) -> None:
    """Adapter so the SolverAttemptSpec hook can call the method."""
    await solver._inject_token_interceptor(page)


async def run_solver_attempt(
    solver: "TurnstileSolver", spec: SolverAttemptSpec
) -> object:
    """Drive a strategy: spin up driver, poll for token, capture cookies."""
    try:
        from .turnstile_solver import TurnstileResult
    except ImportError:
        from turnstile_solver import TurnstileResult  # type: ignore[import-not-found] because bare-module fallback when called outside the solvers package

    result = TurnstileResult(strategy=spec.strategy_name)
    browser: Any = None
    context: Any = None
    page: Optional[Page] = None
    try:
        browser, context, page = await spec.driver_factory(solver, spec)
        if spec.pre_navigate_inject is not None:
            await spec.pre_navigate_inject(solver, page)
        await page.goto(spec.page_url, wait_until='networkidle', timeout=30000)
        if spec.post_navigate_pause_s > 0:
            await asyncio.sleep(spec.post_navigate_pause_s)
        token = await spec.poll_for_token(solver, page)
        if token:
            result.success = True
            result.token = token
            result.cookies = await get_cookies(context, browser)
        else:
            result.error = spec.no_token_error
    except BROWSER_ERRORS as e:
        result.error = f"{spec.strategy_name} error: {e}"
        logger.error("%s solve failed: %s", spec.strategy_name, e)
        return None  # explicit recovery: non-fatal: BROWSER_ERRORS is recorded but does not abort the caller
    finally:
        await safe_close(context)
        camoufox = spec.extra.get('_camoufox')
        if camoufox is not None:
            try:
                await camoufox.__aexit__(None, None, None)
            except CLEANUP_ERRORS + (PlaywrightError,):
                logger.debug('Camoufox cleanup failed', exc_info=True)
        else:
            await safe_close(browser)
        client = spec.extra.get('_botright_client')
        if client is not None:
            await safe_close(client)
        playwright = spec.extra.get('_pw_ctx')
        if playwright is not None:
            try:
                await playwright.stop()
            except CLEANUP_ERRORS + (PlaywrightError,):
                logger.debug('Playwright cleanup failed', exc_info=True)
    return result


# ---------------------------------------------------------------------------
# SolverAttemptSpec builders — one per strategy. Keeps the orchestrator
# methods to a single line of call-and-return.
# ---------------------------------------------------------------------------


def make_botright_spec(
    page_url: str,
    site_key: Optional[str],
    cached_cookies: Optional[List[Dict]],
) -> SolverAttemptSpec:
    """Build the SolverAttemptSpec for the botright strategy."""
    return SolverAttemptSpec(
        page_url=page_url,
        site_key=site_key,
        cached_cookies=cached_cookies,
        strategy_name="botright",
        driver_factory=botright_driver_factory,
        poll_for_token=poll_token_default,
        post_navigate_pause_s=3.0,
        no_token_error="Botright did not produce token",
    )


def make_camoufox_spec(
    page_url: str,
    site_key: Optional[str],
    cached_cookies: Optional[List[Dict]],
) -> SolverAttemptSpec:
    """Build the SolverAttemptSpec for the camoufox strategy."""
    return SolverAttemptSpec(
        page_url=page_url,
        site_key=site_key,
        cached_cookies=cached_cookies,
        strategy_name="camoufox",
        driver_factory=camoufox_driver_factory,
        poll_for_token=poll_token_camoufox,
        post_navigate_pause_s=1.0,
        no_token_error="Camoufox did not auto-pass Turnstile",
    )


def make_token_harvest_spec(
    page_url: str,
    site_key: Optional[str],
    cached_cookies: Optional[List[Dict]],
) -> SolverAttemptSpec:
    """Build the SolverAttemptSpec for the token_harvest strategy."""
    return SolverAttemptSpec(
        page_url=page_url,
        site_key=site_key,
        cached_cookies=cached_cookies,
        strategy_name="token_harvest",
        driver_factory=playwright_driver_factory,
        poll_for_token=poll_token_harvest,
        pre_navigate_inject=inject_token_interceptor_adapter,
        post_navigate_pause_s=2.0,
        no_token_error="Could not harvest Turnstile token",
    )


def make_cookie_replay_spec(
    page_url: str,
    site_key: Optional[str],
    cached_cookies: Optional[List[Dict]],
) -> SolverAttemptSpec:
    """Build the SolverAttemptSpec for the cookie_replay strategy."""
    return SolverAttemptSpec(
        page_url=page_url,
        site_key=site_key,
        cached_cookies=cached_cookies,
        strategy_name="cookie_replay",
        driver_factory=playwright_driver_factory_basic,
        poll_for_token=poll_token_cookie_replay,
        post_navigate_pause_s=0.0,
        no_token_error="Cached cookies expired",
    )


def make_cookie_refresh_spec(page_url: str) -> SolverAttemptSpec:
    """Build the SolverAttemptSpec for the cookie_refresh fallback."""
    return SolverAttemptSpec(
        page_url=page_url,
        site_key=None,
        cached_cookies=None,
        strategy_name="cookie_refresh",
        driver_factory=playwright_driver_factory_basic,
        poll_for_token=poll_token_default,
        post_navigate_pause_s=5.0,
        no_token_error="Could not refresh cookies",
    )
