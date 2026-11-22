"""google_search.py -- Google Search, News, and Images scraper
Endpoints:
  POST /scrape/google/search -- Google web search
  POST /scrape/google/news -- Google News search
  POST /scrape/google/images -- Google Image search

Uses SearXNG if available (check localhost:8888), otherwise Playwright CDP 9223 with stealth.
SearXNG host is wired via :data:`crawlrx.config.SEARXNG_URL` (service registry pending).
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, List, Optional

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from ._google_search_helpers import (
    RESULT_EXTRACTION_EXCEPTIONS,
    build_images_url,
    build_news_url,
    build_search_url,
    dismiss_consent,
    extract_image_card,
    extract_news_link,
    extract_search_card_fields,
    find_search_containers,
    is_filtered_image_src,
    parse_image_urls_from_html,
    scroll_image_feed,
    scroll_news_feed,
    wait_for_images,
    wait_for_news,
    wait_for_results,
)
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    URLLIB_JSON_EXCEPTIONS,
    urllib_network_exception_types,
)
from .config import (
    SEARXNG_IMAGE_ENGINES,
    SEARXNG_NEWS_ENGINES,
    SEARXNG_URL,
    SEARXNG_VIDEO_ENGINES,
    SEARXNG_WEB_ENGINES,
)
from .stealth_context import close_stealth_context, make_stealth_context
from .utils import timer_ms

logger = logging.getLogger("crawlrx.google_search")
router = APIRouter(prefix="/scrape/google", tags=["google"])


# ─────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────

class GoogleSearchResult(BaseModel):
    """Single Google web search result"""
    title: str
    url: str
    snippet: Optional[str] = None
    displayed_url: Optional[str] = None
    position: int


class GoogleNewsArticle(BaseModel):
    """Single Google News article"""
    title: str
    url: str
    source: Optional[str] = None
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    thumbnail_url: Optional[str] = None


class GoogleImageResult(BaseModel):
    """Single Google Image search result"""
    title: Optional[str] = None
    image_url: str
    thumbnail_url: Optional[str] = None
    source_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class GoogleSearchRequest(BaseModel):
    """Request model for POST /scrape/google/search"""
    query: str = Field(..., description="Search query")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results to return")
    country: str = Field(default="us", description="Country code (e.g. 'us', 'gb')")
    language: str = Field(default="en", description="Language code (e.g. 'en', 'fr')")
    time_range: str = Field(default="any", description="Time filter: day, week, month, year, any")


class GoogleNewsRequest(BaseModel):
    """Request model for POST /scrape/google/news"""
    query: str = Field(..., description="Search query")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum articles to return")
    time_range: str = Field(default="week", description="Time filter: day, week, month")


class GoogleImagesRequest(BaseModel):
    """Request model for POST /scrape/google/images"""
    query: str = Field(..., description="Search query")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum images to return")
    safe: bool = Field(default=True, description="Enable safe search")


class GoogleSearchResponse(BaseModel):
    """Response model for POST /scrape/google/search"""
    success: bool
    results: List[GoogleSearchResult] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class GoogleNewsResponse(BaseModel):
    """Response model for POST /scrape/google/news"""
    success: bool
    articles: List[GoogleNewsArticle] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


class GoogleImagesResponse(BaseModel):
    """Response model for POST /scrape/google/images"""
    success: bool
    images: List[GoogleImageResult] = Field(default_factory=list)
    count: int = 0
    error: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# SearXNG Helper
# ─────────────────────────────────────────────

_SEARXNG_PROBE_PATHS: tuple[str, ...] = ("/healthz", "/")


def _probe_searxng_endpoint(path: str) -> bool:
    """HEAD ``SEARXNG_URL + path`` -- True if reachable within 2s."""
    try:
        req = urllib.request.Request(f"{SEARXNG_URL}{path}", method="HEAD")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except urllib_network_exception_types():
        return False


def _check_searxng_available() -> bool:
    """Check if SearXNG is running locally (any probe path responds)."""
    return any(_probe_searxng_endpoint(path) for path in _SEARXNG_PROBE_PATHS)


def _searxng_engines_for_category(category: str) -> str:
    """Return the hardened SearXNG engine set for a search category."""
    normalized = (category or "general").strip().lower()
    if normalized == "news":
        return SEARXNG_NEWS_ENGINES
    if normalized == "images":
        return SEARXNG_IMAGE_ENGINES
    if normalized == "videos":
        return SEARXNG_VIDEO_ENGINES
    return SEARXNG_WEB_ENGINES


def _build_searxng_url(query: str, language: str, time_range: Optional[str], category: str) -> str:
    """Build the SearXNG JSON search URL."""
    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "engines": _searxng_engines_for_category(category),
        "language": language,
    }
    if time_range and time_range != "any":
        params["time_range"] = time_range
    query_string = urllib.parse.urlencode(params)
    return f"{SEARXNG_URL}/search?{query_string}"


def _searxng_request(url: str) -> dict:
    """Issue the SearXNG JSON request and decode the response."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "X-Real-IP": "127.0.0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _searxng_results(data: dict, limit: int) -> List[GoogleSearchResult]:
    """Convert a SearXNG JSON payload into ``GoogleSearchResult`` rows."""
    return [
        GoogleSearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
            displayed_url=item.get("pretty_url") or item.get("url", ""),
            position=i,
        )
        for i, item in enumerate(data.get("results", [])[:limit], start=1)
    ]


def _searxng_search(
    query: str,
    limit: int,
    language: str = "en",
    time_range: Optional[str] = None,
    category: str = "general",
) -> Optional[GoogleSearchResponse]:
    """Search using SearXNG if available."""
    start = timer_ms()
    url = _build_searxng_url(query, language, time_range, category)
    try:
        data = _searxng_request(url)
    except URLLIB_JSON_EXCEPTIONS as exc:
        logger.debug("SearXNG search failed: %s", exc)
        return None
    results = _searxng_results(data, limit)
    elapsed = int(timer_ms() - start)
    logger.info("SearXNG search returned %d results in %dms", len(results), elapsed)
    return GoogleSearchResponse(
        success=True,
        results=results,
        count=len(results),
        elapsed_ms=elapsed,
    )


# ─────────────────────────────────────────────
# Playwright Scraping
# ─────────────────────────────────────────────


async def _build_search_results(page, limit: int) -> List[GoogleSearchResult]:
    """Walk Google's result cards and build ``GoogleSearchResult`` rows."""
    seen_urls: set[str] = set()
    results: List[GoogleSearchResult] = []
    containers = await find_search_containers(page)
    position = 1
    for container in containers[:limit]:
        try:
            fields = await extract_search_card_fields(container)
        except RESULT_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Error parsing Google result: %s", exc)
            continue
        if fields is None or fields["url"] in seen_urls:
            continue
        seen_urls.add(fields["url"])
        results.append(GoogleSearchResult(position=position, **fields))
        position += 1
    return results


async def _run_playwright_scrape(label: str, runner) -> tuple[Any, int, Optional[str]]:
    """Run ``runner(page)`` inside a stealth Playwright context.

    Returns ``(payload, elapsed_ms, error)``. ``payload`` is whatever
    ``runner`` returned on success, ``None`` on a known browser failure.
    """
    start = timer_ms()
    browser = None
    ctx = None
    page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await make_stealth_context(p)
            payload = await runner(page)
            return payload, int(timer_ms() - start), None
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("%s error: %s", label, exc)
        return None, int(timer_ms() - start), str(exc)
    finally:
        await close_stealth_context(browser, ctx, page)


@dataclass(frozen=True)
class _WebSearchParams:
    """Bundle of web-search args, kept under the param-count threshold."""

    query: str
    limit: int
    language: str
    country: str
    time_range: str


async def _drive_web_search(params: _WebSearchParams, page: Any) -> List[GoogleSearchResult]:
    """Driver: load Google web search and pull the cards."""
    search_url = build_search_url(
        params.query, params.limit, params.language, params.country, params.time_range
    )
    logger.info("[Google] Navigating to search: %s", search_url)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await dismiss_consent(page)
    await wait_for_results(page)
    return await _build_search_results(page, params.limit)


async def _google_web_search_playwright(
    query: str,
    limit: int,
    language: str,
    country: str,
    time_range: str,
) -> GoogleSearchResponse:
    """Google web search using Playwright CDP."""
    params = _WebSearchParams(query, limit, language, country, time_range)
    payload, elapsed, error = await _run_playwright_scrape(
        "Google web search",
        lambda page: _drive_web_search(params, page),
    )
    if error is not None:
        return GoogleSearchResponse(success=False, error=error, elapsed_ms=elapsed)
    results = payload or []
    logger.info("Google web search returned %d results in %dms", len(results), elapsed)
    return GoogleSearchResponse(success=True, results=results, count=len(results), elapsed_ms=elapsed)


async def _build_news_articles(page, limit: int) -> List[GoogleNewsArticle]:
    """Walk News article links and build ``GoogleNewsArticle`` rows."""
    article_links = await page.query_selector_all("article a[href], a[jslog][href]")
    seen_urls: set[str] = set()
    articles: List[GoogleNewsArticle] = []
    for link in article_links[: limit * 2]:
        try:
            payload = await extract_news_link(link)
        except RESULT_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("Error parsing Google News article: %s", exc)
            continue
        if payload is None or payload["url"] in seen_urls:
            continue
        seen_urls.add(payload["url"])
        articles.append(GoogleNewsArticle(**payload))
        if len(articles) >= limit:
            break
    return articles


async def _drive_news_search(query: str, limit: int, page: Any) -> List[GoogleNewsArticle]:
    """Driver: load Google News and pull the article cards."""
    search_url = build_news_url(query)
    logger.info("[Google News] Navigating to: %s", search_url)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await wait_for_news(page)
    await scroll_news_feed(page, asyncio)
    return await _build_news_articles(page, limit)


async def _google_news_playwright(query: str, limit: int, time_range: str) -> GoogleNewsResponse:
    """Google News search using Playwright CDP."""
    payload, elapsed, error = await _run_playwright_scrape(
        "Google News search",
        lambda page: _drive_news_search(query, limit, page),
    )
    if error is not None:
        return GoogleNewsResponse(success=False, error=error, elapsed_ms=elapsed)
    articles = (payload or [])[:limit]
    logger.info("Google News search returned %d articles in %dms", len(articles), elapsed)
    return GoogleNewsResponse(success=True, articles=articles, count=len(articles), elapsed_ms=elapsed)


async def _images_from_html(page, limit: int, seen: set[str]) -> List[GoogleImageResult]:
    """First-pass image extraction: scrape JSON URLs out of page HTML."""
    images: List[GoogleImageResult] = []
    try:
        html = await page.content()
    except RESULT_EXTRACTION_EXCEPTIONS as exc:
        logger.debug("Error reading page content for images: %s", exc)
        return images
    for url in parse_image_urls_from_html(html)[:limit]:
        if url in seen or "gstatic.com" in url:
            continue
        seen.add(url)
        images.append(GoogleImageResult(image_url=url, thumbnail_url=url))
    return images


async def _images_from_dom(page, limit: int, seen: set[str], existing: int) -> List[GoogleImageResult]:
    """Second-pass image extraction: walk live ``img`` elements."""
    images: List[GoogleImageResult] = []
    img_elements = await page.query_selector_all("img[src]")
    for img in img_elements:
        try:
            payload = await extract_image_card(img, seen)
        except RESULT_EXTRACTION_EXCEPTIONS:
            continue
        if payload is None:
            continue
        images.append(GoogleImageResult(**payload))
        if existing + len(images) >= limit:
            break
    return images


async def _build_images(page, limit: int) -> List[GoogleImageResult]:
    """Combine HTML-JSON and DOM image extraction passes."""
    seen: set[str] = set()
    images = await _images_from_html(page, limit, seen)
    if len(images) >= limit:
        return images
    images.extend(await _images_from_dom(page, limit, seen, len(images)))
    return images


async def _drive_images_search(query: str, limit: int, safe: bool, page: Any) -> List[GoogleImageResult]:
    """Driver: load Google Images and pull the image cards."""
    search_url = build_images_url(query, safe)
    logger.info("[Google Images] Navigating to: %s", search_url)
    await page.goto(search_url, wait_until="networkidle", timeout=30000)
    await wait_for_images(page)
    await scroll_image_feed(page, asyncio)
    return await _build_images(page, limit)


async def _google_images_playwright(query: str, limit: int, safe: bool) -> GoogleImagesResponse:
    """Google Images search using Playwright CDP."""
    payload, elapsed, error = await _run_playwright_scrape(
        "Google Images search",
        lambda page: _drive_images_search(query, limit, safe, page),
    )
    if error is not None:
        return GoogleImagesResponse(success=False, error=error, elapsed_ms=elapsed)
    images = (payload or [])[:limit]
    logger.info("Google Images search returned %d images in %dms", len(images), elapsed)
    return GoogleImagesResponse(success=True, images=images, count=len(images), elapsed_ms=elapsed)


# Re-export the rarely-used helper so external callers (and the static test
# layer) keep a stable surface even though the orchestrators no longer call it.
__all__ = [
    "GoogleImageResult",
    "GoogleImagesRequest",
    "GoogleImagesResponse",
    "GoogleNewsArticle",
    "GoogleNewsRequest",
    "GoogleNewsResponse",
    "GoogleSearchRequest",
    "GoogleSearchResponse",
    "GoogleSearchResult",
    "google_images",
    "google_news",
    "google_search",
    "is_filtered_image_src",
    "router",
]


# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────

@router.post("/search", response_model=GoogleSearchResponse)
async def google_search(request: GoogleSearchRequest) -> GoogleSearchResponse:
    """Google web search via SearXNG (preferred) with Playwright CDP fallback."""
    if _check_searxng_available():
        result = _searxng_search(
            query=request.query,
            limit=request.limit,
            language=request.language,
            time_range=request.time_range,
        )
        if result and result.success:
            return result

    return await _google_web_search_playwright(
        request.query,
        request.limit,
        request.language,
        request.country,
        request.time_range,
    )


@router.post("/news", response_model=GoogleNewsResponse)
async def google_news(request: GoogleNewsRequest) -> GoogleNewsResponse:
    """Google News search via Playwright CDP against news.google.com."""
    return await _google_news_playwright(request.query, request.limit, request.time_range)


@router.post("/images", response_model=GoogleImagesResponse)
async def google_images(request: GoogleImagesRequest) -> GoogleImagesResponse:
    """Google Images search via Playwright CDP against Google Images."""
    return await _google_images_playwright(request.query, request.limit, request.safe)
