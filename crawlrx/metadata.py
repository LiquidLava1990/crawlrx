"""metadata.py -- /metadata router for CrawlRX.

Extract page metadata (title, OG tags, canonical, etc.) from any URL via
Playwright. Launches a fresh headless browser per request and closes when done.
"""

import logging
import random
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter

from .models import MetadataRequest, MetadataResponse
from .utils import (
    extract_metadata,
    count_words,
    timer_ms,
    detect_bot_block,
    _get_stealth_init_script,
    STEALTH_TIMEZONES,
    STEALTH_VIEWPORTS,
)
from .config import USER_AGENTS
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.metadata")
router = APIRouter(prefix="", tags=["metadata"])

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_DEFAULT_HEADERS_BASE = {
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class StealthContextOptions:
    """Optional knobs for ``create_stealth_context_async`` (caps positional args)."""

    user_agent: Optional[str] = None
    locale: str = "en-US"
    viewport: Optional[dict] = None
    extra_http_headers: Optional[Dict[str, str]] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)


def _build_default_headers(locale: str, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Default Accept-Language headers merged with caller-supplied overrides."""
    headers = {
        "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9",
        **_DEFAULT_HEADERS_BASE,
    }
    if extra:
        headers.update(extra)
    return headers


def _build_context_kwargs(opts: StealthContextOptions) -> Dict[str, Any]:
    """Resolve stealth-context kwargs dict from ``opts`` (random fallbacks applied)."""
    ua = opts.user_agent or random.choice(USER_AGENTS)
    viewport = opts.viewport or random.choice(STEALTH_VIEWPORTS)
    timezone_id = random.choice(STEALTH_TIMEZONES)
    ctx_kwargs: Dict[str, Any] = {
        "user_agent": ua,
        "locale": opts.locale,
        "timezone_id": timezone_id,
        "viewport": viewport,
        "java_script_enabled": True,
        "extra_http_headers": _build_default_headers(opts.locale, opts.extra_http_headers),
    }
    for key, value in opts.extra_kwargs.items():
        if key not in ctx_kwargs:
            ctx_kwargs[key] = value
    return ctx_kwargs


async def create_stealth_context_async(
    browser: "Browser | BrowserContext | Page | None",
    options: Optional[StealthContextOptions] = None,
) -> "BrowserContext":
    """Create a Playwright browser context with anti-detection settings."""
    opts = options or StealthContextOptions()
    ctx_kwargs = _build_context_kwargs(opts)
    context = await browser.new_context(**ctx_kwargs)
    stealth_script = _get_stealth_init_script()
    if stealth_script:
        await context.add_init_script(stealth_script)
    ua = ctx_kwargs["user_agent"]
    vp = ctx_kwargs["viewport"]
    logger.debug(
        "Created async stealth context: UA=%s... viewport=%dx%d",
        ua[:50],
        vp["width"],
        vp["height"],
    )
    return context


def _html_to_text(html: str) -> str:
    """Extract plain text from HTML for word counting."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except BROWSER_SCRAPE_EXCEPTIONS:
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


def _empty_metadata_result(url: str) -> dict:
    """Skeleton result dict shared by every metadata fetch attempt."""
    return {
        "success": False,
        "url": url,
        "title": None,
        "description": None,
        "og_title": None,
        "og_description": None,
        "og_image": None,
        "canonical": None,
        "author": None,
        "keywords": None,
        "favicon": None,
        "word_count": None,
        "elapsed_ms": 0,
        "error": None,
    }


def _populate_metadata_fields(result: dict, meta: dict, page_title: str) -> None:
    """Copy ``meta`` keys into ``result`` (in-place)."""
    result["title"] = meta.get("title") or page_title or None
    for key in (
        "description",
        "og_title",
        "og_description",
        "og_image",
        "canonical",
        "author",
        "keywords",
        "favicon",
    ):
        result[key] = meta.get(key)


async def safe_close(target: "Page | BrowserContext | Browser | None") -> None:
    """Close ``target`` (page/context/browser) — log + swallow on any failure."""
    if target is None:
        return
    try:
        await target.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Cleanup failed (non-critical): %s", exc)


async def _wait_networkidle(page: Any, timeout: int) -> None:
    """Best-effort networkidle wait (Playwright Timeout is non-fatal)."""
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 10000))
    except PWTimeout:
        return


async def _extract_one(p: Any, url: str, timeout: int, result: dict, start: float) -> bool:
    """One Playwright attempt — populates ``result``, returns True iff success."""
    browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    context = page = None
    try:
        context = await create_stealth_context_async(
            browser,
            StealthContextOptions(user_agent=USER_AGENTS[0]),
        )
        page = await context.new_page()
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        await _wait_networkidle(page, timeout)
        html = await page.content()
        page_title = await page.title()
        if detect_bot_block(html, page_title or ""):
            result["error"] = "Bot/Cloudflare block detected"
            result["elapsed_ms"] = int(timer_ms() - start)
            logger.warning("Metadata: Bot/Cloudflare block detected for %s", url)
            return False
        _populate_metadata_fields(result, extract_metadata(html, base_url=url), page_title)
        result["word_count"] = count_words(_html_to_text(html))
        result["success"] = True
        result["elapsed_ms"] = int(timer_ms() - start)
        logger.info("Metadata extracted for %s - title: %s", url, result["title"])
        return True
    finally:
        await safe_close(page)
        await safe_close(context)
        await safe_close(browser)


async def _try_one_attempt(url: str, timeout: int, result: dict, start: float) -> bool:
    """Single Playwright attempt — returns True iff result was populated."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await _extract_one(p, url, timeout, result, start)


@dataclass(frozen=True)
class _MetadataAttemptCtx:
    """Per-attempt parameter bundle for metadata fetch retries."""
    url: str
    timeout: int
    start: float
    attempt: int
    max_attempts: int


async def _attempt_metadata_fetch(
    ctx: _MetadataAttemptCtx, result: dict,
) -> tuple[bool, Optional[str]]:
    """Run one metadata attempt. Returns (succeeded, error_text)."""
    from playwright.async_api import TimeoutError as PWTimeout

    try:
        await _try_one_attempt(ctx.url, ctx.timeout, result, ctx.start)
        return True, None
    except PWTimeout as exc:
        logger.warning(
            "Metadata fetch timeout for %s (attempt %d/%d): %s",
            ctx.url, ctx.attempt, ctx.max_attempts, exc,
        )
        # explicit recovery: PWTimeout non-fatal; caller decides on retry
        return False, f"Timeout: {exc}"
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error(
            "Metadata fetch error for %s (attempt %d/%d): %s",
            ctx.url, ctx.attempt, ctx.max_attempts, exc,
        )
        # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS non-fatal; caller decides on retry
        return False, str(exc)


async def _do_metadata_fetch(url: str, agent: Optional[str], timeout: int) -> dict:
    """Async Playwright metadata extraction with bounded retry loop."""
    start = timer_ms()
    result = _empty_metadata_result(url)
    max_attempts = 3
    last_error: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        ctx = _MetadataAttemptCtx(
            url=url, timeout=timeout, start=start,
            attempt=attempt, max_attempts=max_attempts,
        )
        ok, err = await _attempt_metadata_fetch(ctx, result)
        if ok:
            return result
        last_error = err
        if attempt < max_attempts:
            await asyncio.sleep(1.5 ** attempt)

    result["error"] = last_error
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


@router.post("/metadata", response_model=MetadataResponse, summary="Extract page metadata")
async def metadata_endpoint(request: MetadataRequest) -> MetadataResponse:
    """Extract metadata from a URL via Playwright."""
    try:
        raw = await _do_metadata_fetch(request.url, request.agent, request.timeout)
        return MetadataResponse(**raw)
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error(
            "metadata_endpoint unhandled exception for %s: %s",
            request.url, e, exc_info=True,
        )
        return MetadataResponse(
            success=False,
            url=request.url,
            elapsed_ms=0,
            error="Internal error: %s" % e,
        )
