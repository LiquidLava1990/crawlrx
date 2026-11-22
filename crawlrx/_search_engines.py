"""Per-engine search runners (split from search.py).

Engines: SearXNG, ddgs (DuckDuckGo lib), Playwright (Google/Bing/DDG).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional

from ._search_helpers import (
    EngineSearchParams,
    SEARCH_NET_EXCEPTIONS,
    SERP_BROWSER_EXCEPTIONS,
    safe_close_resources,
)
from ._search_parsers import (
    parse_bing_results,
    parse_ddg_results,
    parse_google_results,
)
from .browser_pool import get_browser, release_browser
from .config import SEARXNG_TIMEOUT, SEARXNG_URL, SEARXNG_WEB_ENGINES, USER_AGENTS
from .utils import create_stealth_context, timer_ms

logger = logging.getLogger("crawlrx.search.engines")

Page = Any


# ─────────────────────────────────────────────
# ddgs library detection (one place)
# ─────────────────────────────────────────────

try:
    from ddgs import DDGS as _DDGS
    DDGS_AVAILABLE = True
    logger.info("ddgs library available -- using for DuckDuckGo searches")
except ImportError:
    try:
        from duckduckgo_search import DDGS as _DDGS  # type: ignore[import-not-found] because optional fallback package; absent when only ddgs is installed
        DDGS_AVAILABLE = True
        logger.info("duckduckgo_search library available")
    except ImportError:
        DDGS_AVAILABLE = False
        _DDGS = None
        logger.info("ddgs not installed -- DDG will use Playwright")


def random_ua() -> str:
    """Pick a random User-Agent from the global list."""
    return random.choice(USER_AGENTS)


async def random_delay() -> None:
    """Sleep 0.5-2s to reduce bot-detection fingerprint."""
    await asyncio.sleep(random.uniform(0.5, 2.0))


def _empty_result(engine: str, query: str) -> dict:
    return {
        "success": False,
        "query": query,
        "engine": engine,
        "results": [],
        "total_results": None,
        "elapsed_ms": 0,
        "error": None,
    }


# ─────────────────────────────────────────────
# SearXNG engine (urllib JSON API)
# ─────────────────────────────────────────────

def _build_searxng_url(query: str, language: Optional[str]) -> str:
    lang = language or "en"
    params = {
        "q": query,
        "format": "json",
        "engines": SEARXNG_WEB_ENGINES,
        "language": lang,
    }
    return f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"


def _searxng_results_from(data: dict, num_results: int) -> List[dict]:
    out = []
    for i, item in enumerate(data.get("results", [])[:num_results], 1):
        out.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "position": i,
            "content": None,
        })
    return out


def do_search_searxng(query: str, num_results: int, language: Optional[str] = None, country: Optional[str] = None) -> dict:
    """Call SearXNG JSON API (sync). Use via ``asyncio.to_thread``."""
    start = timer_ms()
    result = _empty_result("searxng", query)
    url = _build_searxng_url(query, language)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": random_ua(),
            "Accept": "application/json",
            "X-Real-IP": "127.0.0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SEARXNG_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        result["error"] = "SearXNG connection error: %s" % exc
        logger.warning("SearXNG unavailable: %s", exc)
        result["elapsed_ms"] = int(timer_ms() - start)
        return result
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result["error"] = "SearXNG decode error: %s" % type(exc).__name__
        logger.warning("SearXNG bad payload: %s", type(exc).__name__)
        result["elapsed_ms"] = int(timer_ms() - start)
        return result
    except SEARCH_NET_EXCEPTIONS as exc:
        result["error"] = "SearXNG net error: %s" % type(exc).__name__
        logger.warning("SearXNG net failure: %s", type(exc).__name__)
        result["elapsed_ms"] = int(timer_ms() - start)
        return result

    raw = _searxng_results_from(data, num_results)
    result["results"] = raw
    result["success"] = True
    result["total_results"] = str(len(raw))
    logger.info("SearXNG returned %d results for '%s'", len(raw), query)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


# ─────────────────────────────────────────────
# DuckDuckGo via ddgs library
# ─────────────────────────────────────────────

def _ddgs_search_sync(query: str, num_results: int, region: str) -> list:
    with _DDGS() as ddgs:  # type: ignore[misc] because ddgs has no public stubs; context-manager protocol verified at runtime
        return list(ddgs.text(query, max_results=num_results, region=region))


async def do_search_ddgs(
    query: str,
    num_results: int,
    language: Optional[str] = None,
    country: Optional[str] = None,
) -> dict:
    """DuckDuckGo via ddgs library (offloaded to thread)."""
    start = timer_ms()
    result = _empty_result("duckduckgo", query)

    if not DDGS_AVAILABLE or _DDGS is None:
        result["error"] = "ddgs not installed -- run: pip install ddgs"
        result["elapsed_ms"] = int(timer_ms() - start)
        return result

    region = "%s-%s" % (language or "en", (country or "US").upper())
    try:
        raw = await asyncio.to_thread(_ddgs_search_sync, query, num_results, region)
    except SEARCH_NET_EXCEPTIONS as exc:
        result["error"] = "ddgs net error: %s" % type(exc).__name__
        logger.warning("ddgs net failure: %s", type(exc).__name__)
        result["elapsed_ms"] = int(timer_ms() - start)
        return result

    items = []
    for i, item in enumerate(raw, 1):
        items.append({
            "title": item.get("title", ""),
            "url": item.get("href", item.get("url", "")),
            "snippet": item.get("body", item.get("snippet", "")),
            "position": i,
            "content": None,
        })
    result["results"] = items
    result["success"] = True
    result["total_results"] = str(len(items))
    logger.info("ddgs returned %d results for '%s'", len(items), query)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


# ─────────────────────────────────────────────
# Playwright SERP engine
# ─────────────────────────────────────────────

def _build_playwright_url(query: str, engine: str, params: EngineSearchParams) -> str:
    q = urllib.parse.quote_plus(query)
    if engine == "google":
        url = "https://www.google.com/search?q=%s&num=%d" % (q, min(params.num_results, 100))
        if params.language:
            url += "&hl=%s" % params.language
        if params.country:
            url += "&gl=%s" % params.country
        return url
    if engine == "bing":
        url = "https://www.bing.com/search?q=%s&count=%d" % (q, params.num_results)
        if params.language:
            url += "&setlang=%s" % params.language
        if params.country:
            url += "&cc=%s" % params.country
        return url
    # duckduckgo playwright fallback
    url = "https://duckduckgo.com/?q=%s&ia=web" % q
    if params.language and params.country:
        url += "&kl=%s-%s" % (params.language, params.country)
    return url


_GOOGLE_CONSENT_SELECTORS = (
    'button[aria-label*="Reject"]',
    'button[aria-label*="refuser"]',
    'button:has-text("Reject all")',
    'button:has-text("Refuser tout")',
    'button#L2AGLb',
    'button[aria-label="Reject all"]',
    'form[action*="consent"] button:first-of-type',
)


async def _try_click_consent(page: Page, selector: str) -> bool:
    """Returns True if click succeeded -- caller stops iterating."""
    try:
        btn = await page.query_selector(selector)
        if not btn or not await btn.is_visible():
            return False
        await btn.click()
        await page.wait_for_timeout(500)
        return True
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.debug("Consent click skip (%s): %s", selector, type(exc).__name__)
        return False


async def dismiss_google_consent(page: Page) -> None:
    """Try to dismiss Google consent/cookie popup."""
    for selector in _GOOGLE_CONSENT_SELECTORS:
        if await _try_click_consent(page, selector):
            logger.debug("Clicked consent: %s", selector)
            return


_RESULT_WAIT_SELECTORS = {
    "google": "div.g, #search, #rso, [data-hveid]",
    "bing": "li.b_algo, #b_results",
    "duckduckgo": "[data-testid='result'], .result__body, article",
}


async def _wait_for_results(page: Page, engine: str, pw_timeout) -> None:
    """Wait for engine-specific result selectors. Suppress timeout."""
    selector = _RESULT_WAIT_SELECTORS.get(engine, _RESULT_WAIT_SELECTORS["duckduckgo"])
    try:
        await page.wait_for_selector(selector, timeout=5000)
    except pw_timeout:
        logger.warning("Results selector timed out on %s", engine)
        return None  # explicit recovery: best-effort wait; pw_timeout is non-fatal


def _parser_for(engine: str):
    if engine == "google":
        return parse_google_results
    if engine == "bing":
        return parse_bing_results
    return parse_ddg_results


async def _navigate_and_parse(
    page: Page,
    engine: str,
    search_url: str,
    search_timeout: int,
    pw_timeout,
) -> List[dict]:
    try:
        await page.goto(search_url, timeout=search_timeout, wait_until="networkidle")
    except pw_timeout:
        logger.warning("networkidle timeout on %s -- continuing", engine)
        return None  # explicit recovery: non-fatal: pw_timeout is recorded but does not abort the caller
    if engine == "google":
        await dismiss_google_consent(page)
    await _wait_for_results(page, engine, pw_timeout)
    parser = _parser_for(engine)
    return await parser(page)


async def do_search_playwright(
    query: str,
    engine: str,
    params: EngineSearchParams,
) -> dict:
    """Search via Playwright using the browser pool."""
    from playwright.async_api import TimeoutError as PWTimeout

    start = timer_ms()
    result = _empty_result(engine, query)
    search_url = _build_playwright_url(query, engine, params)

    browser_obj = None
    slot = None
    context = None
    page: Optional[Page] = None

    try:
        browser_obj, slot = await get_browser()
        locale = "%s-%s" % (params.language or "en", (params.country or "US").upper())
        context = await create_stealth_context(browser_obj, locale=locale)
        page = await context.new_page()
        search_timeout = min(params.timeout_ms, 30000)
        page.set_default_timeout(search_timeout)
        await random_delay()
        raw = await _navigate_and_parse(page, engine, search_url, search_timeout, PWTimeout)
        result["results"] = raw[: params.num_results]
        result["success"] = True
        logger.info(
            "%s Playwright returned %d results for '%s'",
            engine, len(result["results"]), query,
        )
    except PWTimeout as exc:
        result["error"] = "Timeout: %s" % exc
        logger.error("Search timeout '%s' on %s: %s", query, engine, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except SERP_BROWSER_EXCEPTIONS as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        logger.error("Search error '%s' on %s: %s", query, engine, type(exc).__name__)
        return None  # explicit recovery: non-fatal: SERP_BROWSER_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await safe_close_resources(logger, (page, "page"), (context, "context"))
        if slot is not None:
            await release_browser(slot)

    result["elapsed_ms"] = int(timer_ms() - start)
    return result


# ─────────────────────────────────────────────
# Engine retry runner
# ─────────────────────────────────────────────

MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 3, 9)


async def _dispatch_engine(
    engine: str,
    query: str,
    params: EngineSearchParams,
) -> dict:
    if engine == "searxng":
        return await asyncio.to_thread(
            do_search_searxng, query, params.num_results, params.language, params.country
        )
    if engine == "duckduckgo" and DDGS_AVAILABLE:
        return await do_search_ddgs(
            query, params.num_results, params.language, params.country
        )
    return await do_search_playwright(query, engine, params)


def _retry_wait_seconds(attempt: int) -> int:
    return BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]


async def _try_engine_attempt(
    engine: str, query: str, params: EngineSearchParams, attempt: int,
) -> dict:
    """Run one attempt; return engine result dict or a synthetic net-error record."""
    try:
        result = await _dispatch_engine(engine, query, params)
        if result is None:
            return {
                "success": False, "results": [], "engine": engine,
                "error": "engine returned no result",
            }
        return result
    except SEARCH_NET_EXCEPTIONS as exc:
        logger.warning(
            "Engine '%s' attempt %d net error '%s'",
            engine, attempt + 1, type(exc).__name__,
        )
        # explicit recovery: SEARCH_NET_EXCEPTIONS non-fatal; surface as last-error record
        return {
            "success": False, "results": [], "engine": engine,
            "error": "net error: %s" % type(exc).__name__,
        }


async def engine_search_with_retry(
    engine: str,
    query: str,
    params: EngineSearchParams,
) -> dict:
    """Run one engine with up to MAX_RETRIES attempts."""
    last: dict = {
        "success": False, "results": [], "engine": engine, "error": "not attempted",
    }
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            wait = _retry_wait_seconds(attempt)
            logger.warning(
                "Retry %d/%d for engine '%s' query '%s' -- waiting %ds",
                attempt + 1, MAX_RETRIES, engine, query, wait,
            )
            await asyncio.sleep(wait)
        last = await _try_engine_attempt(engine, query, params, attempt)
        if last.get("success") and last.get("results"):
            logger.info(
                "Engine '%s' succeeded on attempt %d for '%s'",
                engine, attempt + 1, query,
            )
            return last
        logger.warning(
            "Engine '%s' attempt %d returned empty/failed for '%s': %s",
            engine, attempt + 1, query, last.get("error"),
        )
    return last
