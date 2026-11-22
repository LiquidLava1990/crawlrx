"""HTTP-only smart_fetch tier for /scrape.

Owns the alternative cheap path that bypasses Playwright when the request
does not need browser-only actions. It can satisfy whole-page, CSS selector,
and named-selector extraction from HTTP HTML, then falls through to Playwright
when static HTML cannot produce useful content.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Optional

from .smart_fetch import smart_fetch
from .scrape_policy import plan_scrape
from .utils import (
    extract_images,
    extract_links,
    extract_structured_data,
    extract_title,
    detect_bot_block,
    html_to_markdown,
    timer_ms,
)
from ._scraper_helpers import PARSE_EXCEPTIONS

logger = logging.getLogger("crawlrx.scrape")

try:
    from soupsieve.util import SelectorSyntaxError
    _SELECTOR_EXCEPTIONS = (*PARSE_EXCEPTIONS, SelectorSyntaxError)
except ImportError:
    _SELECTOR_EXCEPTIONS = PARSE_EXCEPTIONS


def _html_to_text(html: str) -> str:
    """Extract plain text from HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except (ImportError, *PARSE_EXCEPTIONS):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


async def _extract_from_html(html_chunk: str, extract: str, url: str, selector: Optional[str]) -> dict:
    """Build a ScrapeItem dict from an HTML chunk."""
    sr: dict = {
        "selector": selector, "text": None, "html": None,
        "markdown": None, "links": None, "images": None, "count": 1,
    }
    if extract in ("all", "html"):
        sr["html"] = html_chunk
    if extract in ("all", "text"):
        sr["text"] = _html_to_text(html_chunk)
    if extract in ("all", "markdown"):
        sr["markdown"] = html_to_markdown(html_chunk, base_url=url)
    if extract in ("all", "links"):
        sr["links"] = extract_links(html_chunk, base_url=url)
    if extract in ("all", "images"):
        sr["images"] = extract_images(html_chunk, base_url=url)
    return sr


def _item_has_content(item: dict) -> bool:
    """True if a ScrapeItem-shaped dict contains useful payload."""
    for key in ("text", "html", "markdown"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("links", "images"):
        value = item.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _select_html_chunks(html: str, selector: str, limit: int = 10000) -> list[str]:
    """Extract matching HTML chunks with BeautifulSoup CSS selectors."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        return [str(el) for el in soup.select(selector)[:limit]]
    except (ImportError, *_SELECTOR_EXCEPTIONS) as exc:
        logger.debug("HTTP selector extraction declined for '%s': %s", selector, exc)
        return []


def _select_named_text(html: str, selectors: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Extract named selector text from static HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except (ImportError, *_SELECTOR_EXCEPTIONS) as exc:
        logger.debug("HTTP named selector extraction declined: %s", exc)
        return {key: None for key in selectors}
    out: Dict[str, Optional[str]] = {}
    for key, selector in selectors.items():
        try:
            match = soup.select_one(selector)
            out[key] = match.get_text(separator="\n", strip=True) if match else None
        except _SELECTOR_EXCEPTIONS as exc:
            logger.debug("HTTP named selector '%s' failed: %s", key, exc)
            out[key] = None
    return out


def _smart_fetch_eligible(params: object) -> bool:
    """True when a scrape can safely try static HTTP before browser render."""
    return plan_scrape(params).strategy == "http_first"


async def _scrape_via_smart_fetch(params: object, start: float) -> Optional[dict]:
    """Try HTTP-only smart_fetch tiers. Returns success result or None to fall through."""
    sf_timeout = params.timeout // 1000 if params.timeout > 1000 else 20
    http_result = await asyncio.to_thread(smart_fetch, params.url, sf_timeout, params.headers)
    if not http_result.get("success"):
        logger.debug("smart_fetch failed for %s: %s", params.url, http_result.get("error"))
        return None
    logger.info("smart_fetch succeeded (%s) for %s", http_result.get("tier"), params.url)
    html = http_result.get("html", "")
    title = extract_title(html)
    if detect_bot_block(html, title or ""):
        logger.debug("smart_fetch returned protected-page content for %s", params.url)
        return None
    results: list[dict] = []
    data: Dict[str, Optional[str]] = {}
    if params.selectors:
        data = _select_named_text(html, params.selectors)
        if not any(value for value in data.values()):
            logger.debug("smart_fetch named selectors matched no content for %s", params.url)
            return None
    elif params.selector:
        chunks = _select_html_chunks(html, params.selector)
        if not chunks:
            logger.debug("smart_fetch selector '%s' matched no content for %s", params.selector, params.url)
            return None
        results = [
            await _extract_from_html(chunk, params.extract, params.url, params.selector)
            for chunk in chunks
        ]
        if not any(_item_has_content(item) for item in results):
            logger.debug("smart_fetch selector '%s' produced empty content for %s", params.selector, params.url)
            return None
    else:
        extracted = await _extract_from_html(html, params.extract, params.url, None)
        if not _item_has_content(extracted):
            logger.debug("smart_fetch produced empty whole-page content for %s", params.url)
            return None
        results = [extracted]
    return {
        "success": True,
        "url": params.url,
        "title": title,
        "results": results,
        "data": data,
        "structured_data": extract_structured_data(html) if params.include_structured_data else None,
        "elapsed_ms": int(timer_ms() - start),
        "error": None,
        "status_code": http_result.get("status"),
        "final_url": http_result.get("final_url") or params.url,
        "source": http_result.get("tier") or "smart_fetch",
        "rendered": False,
        "strategy": "http_first",
        "provenance": {
            "plan_reason": plan_scrape(params).reason,
            "http_attempted": True,
            "browser_attempted": False,
            "http_tier": http_result.get("tier"),
            "http_status": http_result.get("status"),
        },
        "js_result": None,
        "screenshot_b64": None,
        "shadow_content": None,
    }
