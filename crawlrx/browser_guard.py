"""Keep every Playwright browser out of internal address space.

CrawlRX opens browsers in many places (the shared pool, site scrapers, CDP).
Instead of trusting each call site, ``install()`` wraps the Playwright entry
points that create browser contexts, so every context gets one route handler
that aborts requests (navigations, subresources, iframes, websockets) whose
host is loopback, private, link-local or otherwise internal.

Playwright does not route redirect hops, so each context also gets a
navigation tripwire: when a redirect lands a page on an internal address, the
page is closed before the caller can read it (the navigation fails with
"page closed"). The redirected GET itself has already been sent by then.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Dict, TypeVar
from urllib.parse import urlparse

from ._utils_safety import is_safe_url

logger = logging.getLogger("crawlrx.browser_guard")

_GUARDED_FLAG = "__crawlrx_guarded__"
_CONTEXT_FLAG = "_crawlrx_route_guard"
_CHECKED_SCHEMES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_CACHE_LIMIT = 4096
_host_cache: Dict[str, bool] = {}

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def clear_cache() -> None:
    """Forget cached host decisions (tests, or after DNS changes)."""
    _host_cache.clear()


async def _url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    scheme = _CHECKED_SCHEMES.get(parsed.scheme.lower())
    if scheme is None:
        return True  # data:, blob:, about: never leave the browser
    key = f"{scheme}://{parsed.netloc.lower()}"
    cached = _host_cache.get(key)
    if cached is None:
        cached = await asyncio.to_thread(is_safe_url, f"{key}/")
        if len(_host_cache) >= _CACHE_LIMIT:
            _host_cache.clear()
        _host_cache[key] = cached
    return cached


async def guard_route(route: Any) -> None:
    """Playwright route handler: let public requests through, abort internal ones."""
    url = route.request.url
    if await _url_allowed(url):
        await route.fallback()
        return
    logger.warning("Browser request to internal address blocked: %s", url)
    await route.abort("blockedbyclient")


async def _close_if_internal(request: Any) -> None:
    if request.redirected_from is None or await _url_allowed(request.url):
        return
    frame = request.frame
    logger.warning("Redirect into internal address space; closing page: %s", request.url)
    if frame.parent_frame is None:
        await frame.page.close()
    else:
        await frame.goto("about:blank")


def _watch_page(page: Any) -> None:
    page.on("request", _close_if_internal)


async def guard_context(context: Any) -> Any:
    """Attach the route guard and redirect tripwire to ``context`` once; returns it."""
    if getattr(context, _CONTEXT_FLAG, False):
        return context
    setattr(context, _CONTEXT_FLAG, True)
    await context.route("**/*", guard_route)
    for page in context.pages:
        _watch_page(page)
    context.on("page", _watch_page)
    return context


def _mark(wrapper: F) -> F:
    setattr(wrapper, _GUARDED_FLAG, True)
    return wrapper


def _guard_returned_context(original: F) -> F:
    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await guard_context(await original(*args, **kwargs))
    return _mark(wrapper)  # type: ignore[return-value]


def _guard_new_page(original: F) -> F:
    """``Browser.new_page`` makes a hidden context; guard it before first navigation."""
    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        page = await original(*args, **kwargs)
        await guard_context(page.context)
        return page
    return _mark(wrapper)  # type: ignore[return-value]


def _guard_connected_browser(original: F) -> F:
    """CDP browsers arrive with existing contexts; guard those too."""
    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        browser = await original(*args, **kwargs)
        for context in browser.contexts:
            await guard_context(context)
        return browser
    return _mark(wrapper)  # type: ignore[return-value]


def install() -> None:
    """Wrap Playwright's async context factories. Safe to call more than once."""
    try:
        from playwright.async_api import Browser, BrowserType
    except ImportError:
        logger.debug("Playwright not installed; browser guard not needed")
        return
    patches = (
        (Browser, "new_context", _guard_returned_context),
        (Browser, "new_page", _guard_new_page),
        (BrowserType, "launch_persistent_context", _guard_returned_context),
        (BrowserType, "connect_over_cdp", _guard_connected_browser),
    )
    for owner, name, wrap in patches:
        original = getattr(owner, name)
        if not getattr(original, _GUARDED_FLAG, False):
            setattr(owner, name, wrap(original))
