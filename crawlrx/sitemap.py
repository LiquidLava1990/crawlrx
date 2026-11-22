"""
sitemap.py -- Sitemap link extraction endpoint
Endpoint: POST /map

Uses headless Playwright (fresh browser per request, NOT CDP)
to fetch a page and extract all same-domain links.
"""

import asyncio
import logging
from urllib.parse import urlparse, urljoin

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("crawlrx.sitemap")
sitemap_router = APIRouter(prefix="/map", tags=["map"])


class SitemapRequest(BaseModel):
    """Request model for POST /map"""
    url: str = Field(..., description="URL to extract links from")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum links to return")


class SitemapResponse(BaseModel):
    """Response model for POST /map"""
    success: bool = Field(..., description="Whether extraction succeeded")
    url: str = Field(..., description="Target URL")
    links: list[str] = Field(default_factory=list, description="Extracted same-domain links")
    count: int = Field(default=0, description="Number of links found")
    error: str | None = Field(default=None, description="Error message if failed")


def _extract_links(html: str, base_url: str, limit: int) -> list[str]:
    """Extract all unique same-domain links from HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links = set()
    base_domain = urlparse(base_url).netloc.lower()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        # Skip anchors, javascript, mailto, tel
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        # Convert to absolute URL
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Only http/https same-domain links
        if parsed.scheme in ("http", "https"):
            link_domain = parsed.netloc.lower()
            if link_domain == base_domain:
                links.add(full_url)

    # Return sorted list up to limit
    result = sorted(list(links))
    return result[:limit]


_SITEMAP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _wait_idle_or_continue(page, timeout: int) -> None:
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 20000))
    except PWTimeout:
        return  # parseable anyway


async def close_quietly(obj: object, name: str) -> None:
    if obj is None:
        return
    try:
        await obj.close()
    except (OSError, RuntimeError, AttributeError) as close_err:
        logger.warning("Error closing %s: %s", name, close_err)
        return None  # explicit recovery: cleanup is best-effort; (OSError, RuntimeError, AttributeError) is non-fatal


async def _open_browser_and_navigate(p, url: str, timeout: int):
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=_SITEMAP_USER_AGENT,
        viewport={"width": 1920, "height": 1080},
    )
    page = await context.new_page()
    logger.info("Navigating to: %s", url)
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    await _wait_idle_or_continue(page, timeout)
    return browser, context, page


async def _do_map_extraction(url: str, limit: int, timeout: int) -> dict:
    """Playwright extraction of same-domain links (one browser per call)."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    result: dict = {"success": False, "url": url, "links": [], "count": 0, "error": None}
    browser = context = page = None
    try:
        async with async_playwright() as p:
            browser, context, page = await _open_browser_and_navigate(p, url, timeout)
            html = await page.content()
            links = _extract_links(html, url, limit)
            result.update({"success": True, "links": links, "count": len(links)})
            logger.info("Extracted %d links from %s", len(links), url)
    except PWTimeout as e:
        result["error"] = "Timeout waiting for page: %s" % e
        logger.error("Timeout for URL '%s': %s", url, e)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except (OSError, RuntimeError, ValueError, AttributeError) as e:
        result["error"] = "Extraction failed: %s" % e
        logger.error("Error extracting links from '%s': %s", url, e, exc_info=True)
        return None  # explicit recovery: non-fatal: (OSError, RuntimeError, ValueError, AttributeError) is recorded but does not abort the caller
    finally:
        await close_quietly(page, "page")
        await close_quietly(context, "context")
        await close_quietly(browser, "browser")

    return result


@sitemap_router.post(
    "",
    response_model=SitemapResponse,
    summary="Extract same-domain links from a URL",
)
async def extract_sitemap(request: SitemapRequest) -> SitemapResponse:
    """
    Extract all same-domain links from a webpage.

    - `url`: Target URL to extract links from
    - `limit`: Maximum number of links to return (1-1000, default 100)

    Uses headless Playwright (fresh browser per request).
    Returns deduplicated, sorted list of absolute URLs from the same domain.
    """
    wall_timeout = 60  # seconds

    try:
        raw = await asyncio.wait_for(
            _do_map_extraction(request.url, request.limit, 30000),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for sitemap extraction")
        return SitemapResponse(
            success=False,
            url=request.url,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
        )

    return SitemapResponse(**raw)
