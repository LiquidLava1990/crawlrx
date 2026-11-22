"""Deep-search Playwright helpers (split from search_deep.py).

Wraps a fresh-browser Playwright session for Google dork search and
Google News scraping. Used by ``search_deep.py``.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from ._search_helpers import (
    SERP_BROWSER_EXCEPTIONS,
    SERP_PARSE_EXCEPTIONS,
    safe_close_resources,
)
from .utils import create_stealth_context

logger = logging.getLogger("crawlrx.search_deep.browser")

Page = Any


# ─────────────────────────────────────────────
# Generic browser session lifecycle
# ─────────────────────────────────────────────

_GOOGLE_CONSENT_SELECTORS = (
    "button#L2AGLb",
    'button[aria-label="Reject all"]',
    'button:has-text("Reject all")',
)


async def _try_consent_click(page: Page, selector: str) -> bool:
    try:
        btn = await page.query_selector(selector)
        if not btn or not await btn.is_visible():
            return False
        await btn.click()
        await page.wait_for_timeout(400)
        return True
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.debug("Consent click skip (%s): %s", selector, type(exc).__name__)
        return False


async def dismiss_consent(page: Page) -> None:
    """Try the standard Google consent rejectors."""
    for selector in _GOOGLE_CONSENT_SELECTORS:
        if await _try_consent_click(page, selector):
            return


ParserFn = Callable[..., Awaitable[List[dict]]]


@dataclass(frozen=True)
class BrowserSerpJob:
    """Bundled args for ``run_browser_serp``."""

    url: str
    timeout: int
    label: str
    wait_selector: str
    parser: ParserFn
    parser_args: Tuple[Any, ...] = field(default_factory=tuple)


async def _navigate_safe(
    page: Page, url: str, timeout: int, pw_timeout, label: str,
) -> None:
    """Navigate, suppressing only the ``networkidle`` deadline timeout."""
    try:
        await page.goto(url, timeout=timeout, wait_until="networkidle")
    except pw_timeout as exc:
        logger.warning(
            "networkidle timeout on %s (%s) -- using available content",
            label, type(exc).__name__,
        )
        return None  # explicit recovery: non-fatal: pw_timeout is recorded but does not abort the caller


async def _wait_selector_safe(page: Page, selector: str, pw_timeout) -> None:
    """Wait for a selector, swallowing the deadline-timeout."""
    try:
        await page.wait_for_selector(selector, timeout=10000)
    except pw_timeout as exc:
        logger.debug("Selector wait timed out: %s", type(exc).__name__)


async def run_browser_serp(job: BrowserSerpJob) -> list:
    """Open fresh Playwright browser, navigate, dismiss consent, parse."""
    from playwright.async_api import (
        TimeoutError as PWTimeout,
        async_playwright,
    )

    browser = None
    context = None
    page: Optional[Page] = None
    out: list = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await create_stealth_context(browser, locale="en-US")
            page = await context.new_page()
            await _navigate_safe(page, job.url, job.timeout, PWTimeout, job.label)
            await dismiss_consent(page)
            await _wait_selector_safe(page, job.wait_selector, PWTimeout)
            out = await job.parser(page, *job.parser_args)
            logger.info("%s returned %d results", job.label, len(out))
    except SERP_BROWSER_EXCEPTIONS as exc:
        logger.error("%s search failed: %s", job.label, type(exc).__name__)
        return None  # explicit recovery: non-fatal: SERP_BROWSER_EXCEPTIONS is recorded but does not abort the caller
    finally:
        await safe_close_resources(logger, (page, "page"), (context, "context"))
        if browser is not None:
            await safe_close_resources(logger, (browser, "browser"))
    return out


# ─────────────────────────────────────────────
# Google organic SERP parser (deep mode -- with date)
# ─────────────────────────────────────────────

_GOOGLE_DEEP_CONTAINER_SELECTORS = (
    "div.g",
    "div[data-hveid]:has(h3)",
    "#rso > div",
)


async def _first_containers(page: Page, selectors: tuple) -> list:
    for selector in selectors:
        out = await page.query_selector_all(selector)
        if out:
            return out
    return []


async def _read_text(elem) -> Optional[str]:
    if elem is None:
        return None
    return (await elem.inner_text()).strip()


_GOOGLE_DEEP_SNIPPET_SELECTORS = (
    "div.VwiC3b",
    "span.aCOpRe",
    "div[data-sncf] span",
    "div[style*='-webkit-line-clamp']",
)
_GOOGLE_DEEP_DATE_SELECTORS = (
    "span.MUxGbd",
    "span[class*='date']",
    ".LEwnzc span",
)


async def _query_first(container, selectors: tuple):
    for selector in selectors:
        elem = await container.query_selector(selector)
        if elem:
            return elem
    return None


async def _extract_external_link_url(container) -> Optional[str]:
    """First ``a[href]`` whose URL is external (not google.com) and HTTP."""
    link_el = await container.query_selector("a[href]")
    if link_el is None:
        return None
    url = await link_el.get_attribute("href") or ""
    if not url.startswith("http") or "google.com" in url:
        return None
    return url


async def _extract_google_deep_record(container) -> Optional[dict]:
    try:
        title_el = await container.query_selector("h3")
        url = await _extract_external_link_url(container)
        if title_el is None or url is None:
            return None
        title = await _read_text(title_el) or ""
        if not title:
            return None
        snippet_el = await _query_first(container, _GOOGLE_DEEP_SNIPPET_SELECTORS)
        date_el = await _query_first(container, _GOOGLE_DEEP_DATE_SELECTORS)
        return {
            "title": title,
            "url": url,
            "snippet": await _read_text(snippet_el),
            "date": await _read_text(date_el),
            "source": "google",
        }
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.debug("Google deep card parse failure: %s", type(exc).__name__)
        return None


async def _collect_records(
    containers: list,
    extractor: Callable[[Any], Awaitable[Optional[dict]]],
    num_results: int,
) -> List[dict]:
    """Iterate containers, extract records, dedupe by url, cap at num_results."""
    out: List[dict] = []
    seen: set = set()
    for container in containers:
        if len(out) >= num_results:
            break
        record = await extractor(container)
        if record is None or record["url"] in seen:
            continue
        seen.add(record["url"])
        out.append(record)
    return out


async def parse_google_deep(page: Page, num_results: int) -> List[dict]:
    """Parse Google organic results with date extraction."""
    containers = await _first_containers(page, _GOOGLE_DEEP_CONTAINER_SELECTORS)
    return await _collect_records(containers, _extract_google_deep_record, num_results)


# ─────────────────────────────────────────────
# Google News parser
# ─────────────────────────────────────────────

_NEWS_CONTAINER_SELECTORS = (
    "div.SoaBEf",
    "div.WlydOe",
    "div.Gx5Zad",
    "div.g",
    "#rso > div",
)
_NEWS_TITLE_SELECTORS = (
    "div.mCBkyc",
    "div.n0jPhd",
    "h3",
    "a[href] div[role='heading']",
)
_NEWS_SOURCE_SELECTORS = (
    "div.CEMjEf span",
    "div.UPmit",
    ".NUnG9d span",
    "cite",
)
_NEWS_DATE_SELECTORS = (
    "div.OSrXXb span",
    "span.WG9SHc",
    "time",
    ".LfVVr",
)
_NEWS_SNIPPET_SELECTORS = (
    "div.GI74Re",
    "div.Y3v8qd",
    "div[class*='snippet']",
    "span.st",
)


async def _extract_news_record(container) -> Optional[dict]:
    try:
        url = await _extract_external_link_url(container)
        if url is None:
            return None
        title_el = await _query_first(container, _NEWS_TITLE_SELECTORS)
        headline = await _read_text(title_el)
        if not headline:
            return None
        return {
            "headline": headline,
            "source": await _read_text(
                await _query_first(container, _NEWS_SOURCE_SELECTORS),
            ) or "",
            "date": await _read_text(
                await _query_first(container, _NEWS_DATE_SELECTORS),
            ) or "",
            "url": url,
            "snippet": await _read_text(
                await _query_first(container, _NEWS_SNIPPET_SELECTORS),
            ) or "",
        }
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.debug("News card parse failure: %s", type(exc).__name__)
        return None


async def parse_google_news(page: Page, num_results: int) -> List[dict]:
    """Parse Google News result cards (multi-layout selectors)."""
    containers = await _first_containers(page, _NEWS_CONTAINER_SELECTORS)
    return await _collect_records(containers, _extract_news_record, num_results)


# ─────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────

def build_google_dork_url(dork_query: str, num_results: int) -> str:
    q = urllib.parse.quote_plus(dork_query)
    return "https://www.google.com/search?q=%s&num=%d" % (
        q, min(num_results + 5, 100),
    )


def build_google_news_url(query: str, num_results: int) -> str:
    q = urllib.parse.quote_plus(query)
    # tbs=qdr:w = past week
    return "https://www.google.com/search?q=%s&tbm=nws&tbs=qdr:w&num=%d" % (
        q, min(num_results + 5, 100),
    )


async def search_google_dork(
    dork_query: str, num_results: int, timeout: int,
) -> List[dict]:
    """Run a Google dork SERP scrape (fresh browser per call)."""
    return await run_browser_serp(BrowserSerpJob(
        url=build_google_dork_url(dork_query, num_results),
        timeout=timeout,
        label="Google dork",
        wait_selector="div.g, #search, #rso",
        parser=parse_google_deep,
        parser_args=(num_results,),
    ))


async def search_google_news(
    query: str, num_results: int, timeout: int,
) -> List[dict]:
    """Scrape Google News tab (last 7 days)."""
    return await run_browser_serp(BrowserSerpJob(
        url=build_google_news_url(query, num_results),
        timeout=timeout,
        label="Google News",
        wait_selector="div.SoaBEf, div.WlydOe, div.Gx5Zad, #rso",
        parser=parse_google_news,
        parser_args=(num_results,),
    ))
