"""Helpers for the Google search / news / images Playwright scrapers.

Splits the long ``_google_*_playwright`` orchestration functions in
``google_search.py`` into focused per-step extractors:

  - URL builders (web / news / images)
  - consent-popup dismissal
  - per-result extractors

Keeping these in a sibling module lets the orchestrators stay short and
keeps duplication checks from re-firing on the shared parsing scaffolding.
"""
from __future__ import annotations

import logging
import re
import types
import urllib.parse
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ._scraper_helpers import (
    PLAYWRIGHT_EXCEPTIONS,
    SELECTOR_EXTRACTION_EXCEPTIONS,
    parse_dimension_attr,
)

logger = logging.getLogger("crawlrx.google_search")


# Tuple used by per-card / per-element extraction sites: combine the
# normal selector-extraction failures with Playwright's runtime errors so
# a transient `Error` doesn't crash the whole result loop.
RESULT_EXTRACTION_EXCEPTIONS: tuple[type[BaseException], ...] = (
    SELECTOR_EXTRACTION_EXCEPTIONS + PLAYWRIGHT_EXCEPTIONS
)


_CONSENT_SELECTORS = (
    'button[aria-label*="Reject"]',
    'button:has-text("Reject all")',
    'button#L2AGLb',
)

_TIME_RANGE_TBS = {"day": "d", "week": "w", "month": "m", "year": "y"}


def build_search_url(query: str, limit: int, language: str, country: str, time_range: str) -> str:
    """Build the Google web-search URL for a query + filters."""
    q = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={q}&num={min(limit, 100)}"
    if language:
        url += f"&hl={language}"
    if country:
        url += f"&gl={country}"
    if time_range and time_range != "any":
        token = _TIME_RANGE_TBS.get(time_range)
        if token:
            url += f"&tbs=qdr:{token}"
    return url


def build_news_url(query: str) -> str:
    """Build the Google News search URL."""
    return f"https://news.google.com/search?q={urllib.parse.quote_plus(query)}"


def build_images_url(query: str, safe: bool) -> str:
    """Build the Google Images search URL."""
    q = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?tbm=isch&q={q}"
    if not safe:
        url += "&safe=off"
    return url


async def _try_consent_button(page: Any, button: Any) -> bool:
    """Click ``button`` if visible; True on click, False otherwise."""
    try:
        if not await button.is_visible():
            return False
        await button.click()
        await page.wait_for_timeout(500)
        return True
    except PLAYWRIGHT_EXCEPTIONS:
        return False


async def _consent_button_for(page: Any, selector: str) -> Any:
    """Resolve a consent-popup button handle for a selector, or ``None``."""
    try:
        return await page.query_selector(selector)
    except PLAYWRIGHT_EXCEPTIONS:
        return None


async def _click_visible_consent(page: Any) -> bool:
    """Try each consent selector; click and return True on first hit."""
    for selector in _CONSENT_SELECTORS:
        button = await _consent_button_for(page, selector)
        if button is None:
            continue
        if await _try_consent_button(page, button):
            return True
    return False


async def dismiss_consent(page: "Page") -> None:
    """Best-effort dismissal of Google's reject-all consent popup."""
    try:
        await _click_visible_consent(page)
    except PLAYWRIGHT_EXCEPTIONS as exc:
        logger.debug("Consent dismissal failed: %s", exc)


async def find_search_containers(page: "Page") -> list:
    """Try the known result selectors until one matches at least one card."""
    for selector in ("div.g", "div[data-hveid]:has(h3)", "#rso > div"):
        containers = await page.query_selector_all(selector)
        if containers:
            return containers
    return []


async def wait_for_results(page: "Page") -> None:
    """Wait for the Google web-search result cluster, swallowing timeout."""
    try:
        await page.wait_for_selector("div.g, #search, #rso", timeout=10000)
    except PlaywrightTimeoutError:
        pass


async def _extract_snippet(container: Any) -> Optional[str]:
    """Pull the result snippet text via the known Google selectors."""
    snippet_el = (
        container.query_selector("div.VwiC3b")
        or container.query_selector("span.aCOpRe")
        or container.query_selector(".hgKElc")
    )
    if not snippet_el:
        return None
    return (await snippet_el.inner_text()).strip()


async def _extract_displayed_url(container: Any) -> Optional[str]:
    """Pull the small grey URL line displayed under the title."""
    displayed_el = container.query_selector("cite, .byrV5b")
    if not displayed_el:
        return None
    return (await displayed_el.inner_text()).strip()


async def extract_search_card_fields(container: "ElementHandle") -> Optional[dict]:
    """Return ``{title, url, snippet, displayed_url}`` or ``None`` if invalid."""
    title_el = container.query_selector("h3")
    if not title_el:
        return None
    title = (await title_el.inner_text()).strip()
    link_el = container.query_selector("a[href]")
    if not link_el:
        return None
    url = await link_el.get_attribute("href") or ""
    if not url.startswith("http") or "google.com" in url:
        return None
    snippet = await _extract_snippet(container)
    displayed_url = await _extract_displayed_url(container)
    return {"title": title, "url": url, "snippet": snippet, "displayed_url": displayed_url}


async def scroll_news_feed(page: "Page", asyncio_module: types.ModuleType) -> None:
    """Scroll a few times to nudge Google News into loading more articles."""
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio_module.sleep(0.5)


def normalize_news_url(url: str) -> str:
    """Resolve relative news.google.com URLs to absolute."""
    if url.startswith("./"):
        return f"https://news.google.com{url[1:]}"
    if url.startswith("/"):
        return f"https://news.google.com{url}"
    return url


async def wait_for_news(page: "Page") -> None:
    """Wait for News result cluster, swallowing timeout."""
    try:
        await page.wait_for_selector("article, a[jslog]", timeout=10000)
    except PlaywrightTimeoutError:
        pass


async def _walk_to_article_parent(link: Any) -> Any:
    """Walk up to three parent steps to find the enclosing article element."""
    article_el = link
    for _ in range(3):
        if article_el is None:
            break
        article_el = article_el.query_selector("..")
    return article_el


async def _extract_news_metadata(article_el: Any) -> tuple[Optional[str], Optional[str]]:
    """Pull (source, published_at) from a Google News article wrapper."""
    if article_el is None:
        return (None, None)
    source: Optional[str] = None
    published_at: Optional[str] = None
    try:
        source_el = article_el.query_selector("[data-n-tid]")
        if source_el:
            source = (await source_el.inner_text()).strip()
        time_el = article_el.query_selector("time")
        if time_el:
            published_at = await time_el.get_attribute("datetime")
    except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Suppressed news metadata error: %s", exc)
    return (source, published_at)


async def extract_news_link(link: "ElementHandle") -> Optional[dict]:
    """Build a normalized news-article dict from one anchor element."""
    url = link.get_attribute("href") or ""
    url = normalize_news_url(url)
    if not url.startswith("http"):
        return None
    if "news.google.com" in url and "/articles/" not in url:
        return None
    title = link.inner_text().strip()
    if not title or len(title) < 10:
        return None
    article_el = await _walk_to_article_parent(link)
    source, published_at = await _extract_news_metadata(article_el)
    return {
        "url": url,
        "title": title,
        "source": source,
        "published_at": published_at,
        "snippet": None,
    }


async def scroll_image_feed(page: "Page", asyncio_module: types.ModuleType) -> None:
    """Trigger lazy-loaded thumbnails on the Google Images grid."""
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, 1000)")
        await asyncio_module.sleep(0.5)


async def wait_for_images(page: "Page") -> None:
    """Wait for at least one image element to appear, swallowing timeout."""
    try:
        await page.wait_for_selector("img[data-src], img[src]", timeout=10000)
    except PlaywrightTimeoutError:
        pass


_IMAGE_URL_PATTERN = re.compile(r'"(https://[^"]+\.(?:jpg|jpeg|png|gif|webp))"')


def parse_image_urls_from_html(html: str) -> List[str]:
    """Pull plausible image URLs out of the embedded JSON in page HTML."""
    return _IMAGE_URL_PATTERN.findall(html)


def is_filtered_image_src(src: str, seen: set[str]) -> bool:
    """True if an image src should be skipped (gstatic / dup / non-http)."""
    if not src or "gstatic.com" in src or "google" in src:
        return True
    if not src.startswith("http"):
        return True
    return src in seen


async def _walk_image_to_link(img: Any) -> Optional[str]:
    """Walk up the DOM looking for an enclosing ``http`` href."""
    parent = img
    for _ in range(3):
        parent = parent.query_selector("..")
        if not parent:
            return None
        href = parent.get_attribute("href")
        if href and href.startswith("http"):
            return href
    return None


async def extract_image_card(img: "ElementHandle", seen: set[str]) -> Optional[dict]:
    """Build an image-result dict for one ``img`` element."""
    src = img.get_attribute("src") or ""
    if is_filtered_image_src(src, seen):
        return None
    seen.add(src)
    width = parse_dimension_attr(img.get_attribute("width"))
    height = parse_dimension_attr(img.get_attribute("height"))
    source_url = await _walk_image_to_link(img)
    return {
        "image_url": src,
        "thumbnail_url": src,
        "source_url": source_url,
        "width": width,
        "height": height,
    }
