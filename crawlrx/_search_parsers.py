"""SERP parsers (Google / Bing / DDG). Split from search.py."""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Awaitable, Callable, List, Optional

from ._search_helpers import SERP_PARSE_EXCEPTIONS, url_starts_with_http

logger = logging.getLogger("crawlrx.search.parsers")

# ``Page`` / ``PageElement`` typed as ``Any`` to avoid a hard Playwright import.
Page = Any
PageElement = Any
RecordExtractor = Callable[[PageElement], Awaitable[Optional[dict]]]


# ─────────────────────────────────────────────
# Generic helpers (shared across all engines)
# ─────────────────────────────────────────────

async def _first_match(
    container: PageElement,
    selectors: tuple,
) -> Optional[PageElement]:
    """Return the first child matched by any of ``selectors`` (or ``None``)."""
    for selector in selectors:
        elem = await container.query_selector(selector)
        if elem:
            return elem
    return None


async def _first_container_set(page: Page, selectors: tuple) -> list:
    """Return the first non-empty container set among ``selectors``."""
    for selector in selectors:
        containers = await page.query_selector_all(selector)
        if containers:
            return containers
    return []


async def _read_text(elem: Optional[PageElement]) -> Optional[str]:
    if elem is None:
        return None
    return (await elem.inner_text()).strip()


def _build_record(
    title: str, url: str, snippet: Optional[str],
) -> Optional[dict]:
    """Validate + assemble the canonical result dict."""
    if not url_starts_with_http(url) or not title:
        return None
    return {"title": title, "url": url, "snippet": snippet}


async def _run_parse_loop(
    page: Page,
    container_selectors: tuple,
    extractor: RecordExtractor,
    engine_label: str,
) -> List[dict]:
    """Generic loop: pull containers, extract, attach (position, content)."""
    try:
        containers = await _first_container_set(page, container_selectors)
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.warning("%s parse error: %s", engine_label, type(exc).__name__)
        return []
    out: List[dict] = []
    seen: set = set()
    for container in containers:
        record = await extractor(container)
        if record is None:
            continue
        if record["url"] in seen:
            continue
        seen.add(record["url"])
        record.update({"position": len(out) + 1, "content": None})
        out.append(record)
    return out


# ─────────────────────────────────────────────
# Google parser
# ─────────────────────────────────────────────

_GOOGLE_CONTAINER_SELECTORS = (
    "div.g",
    "div[data-hveid]:has(h3)",
    "#search div:has(h3):has(a[href])",
    "#rso > div, #rso > div > div",
)

_GOOGLE_TITLE_SELECTORS = ("h3", "a h3, [role='heading']")

_GOOGLE_SNIPPET_SELECTORS = (
    "div.VwiC3b",
    "span.aCOpRe",
    "div[data-sncf] span",
    ".hgKElc",
    "div[style*='-webkit-line-clamp']",
)


def _is_google_internal(url: str) -> bool:
    if not url_starts_with_http(url):
        return True
    if url.startswith("https://www.google.com"):
        return True
    return "google.com" in url and "/search" in url


async def _extract_google_record(container: PageElement) -> Optional[dict]:
    try:
        title_el = await _first_match(container, _GOOGLE_TITLE_SELECTORS)
        link_el = await container.query_selector("a[href]")
        if title_el is None or link_el is None:
            return None
        url = await link_el.get_attribute("href") or ""
        if _is_google_internal(url):
            return None
        title = await _read_text(title_el) or ""
        snippet_el = await _first_match(container, _GOOGLE_SNIPPET_SELECTORS)
        snippet = await _read_text(snippet_el)
        return _build_record(title, url, snippet)
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.debug("Google card parse failure: %s", type(exc).__name__)
        return None


async def parse_google_results(page: Page) -> List[dict]:
    """Extract organic results from Google SERP."""
    return await _run_parse_loop(
        page, _GOOGLE_CONTAINER_SELECTORS, _extract_google_record, "Google",
    )


# ─────────────────────────────────────────────
# Bing parser
# ─────────────────────────────────────────────

_BING_CONTAINER_SELECTORS = ("li.b_algo",)
_BING_SNIPPET_SELECTORS = (".b_caption p", "p.b_algoSlug")


async def _extract_bing_record(container: PageElement) -> Optional[dict]:
    try:
        title_el = await container.query_selector("h2 a")
        if title_el is None:
            return None
        title = await _read_text(title_el) or ""
        url = await title_el.get_attribute("href") or ""
        snippet_el = await _first_match(container, _BING_SNIPPET_SELECTORS)
        snippet = await _read_text(snippet_el)
        return _build_record(title, url, snippet)
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.debug("Bing card parse failure: %s", type(exc).__name__)
        return None


async def parse_bing_results(page: Page) -> List[dict]:
    """Extract organic results from Bing SERP."""
    return await _run_parse_loop(
        page, _BING_CONTAINER_SELECTORS, _extract_bing_record, "Bing",
    )


# ─────────────────────────────────────────────
# DuckDuckGo parser
# ─────────────────────────────────────────────

_DDG_CONTAINER_SELECTORS = (
    "[data-testid='result']",
    "article[data-testid='result']",
    ".result__body",
)
_DDG_TITLE_SELECTORS = (
    "h2 a, [data-testid='result-title-a'], .result__a",
)
_DDG_SNIPPET_SELECTORS = (
    "[data-result='snippet'], [data-testid='result-snippet'], .result__snippet",
)


def _decode_ddg_redirect(url: str) -> str:
    if "uddg=" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    return urllib.parse.unquote(params.get("uddg", [url])[0])


async def _extract_ddg_record(container: PageElement) -> Optional[dict]:
    try:
        title_el = await _first_match(container, _DDG_TITLE_SELECTORS)
        if title_el is None:
            return None
        title = await _read_text(title_el) or ""
        raw_url = await title_el.get_attribute("href") or ""
        url = _decode_ddg_redirect(raw_url)
        snippet_el = await _first_match(container, _DDG_SNIPPET_SELECTORS)
        snippet = await _read_text(snippet_el)
        return _build_record(title, url, snippet)
    except SERP_PARSE_EXCEPTIONS as exc:
        logger.debug("DDG card parse failure: %s", type(exc).__name__)
        return None


async def parse_ddg_results(page: Page) -> List[dict]:
    """Extract organic results from DuckDuckGo SERP."""
    return await _run_parse_loop(
        page, _DDG_CONTAINER_SELECTORS, _extract_ddg_record, "DDG",
    )
