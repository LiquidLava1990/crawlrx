"""jimdo_scraper.py -- Jimdo site scraper for CrawlRX.

Detection and content extraction for Jimdo-hosted sites.
Strategy:
  1. Detection: Check page source for Jimdo markers
  2. Scraping: requests + BeautifulSoup (Jimdo exports clean HTML)
"""

import logging
from typing import Optional, List, Dict, Any

import requests
from bs4 import BeautifulSoup
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.jimdo_scraper")

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrawlRX/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def detect_jimdo(url: str, timeout: int = _TIMEOUT) -> bool:
    """Return True if URL is a Jimdo site.

    Checks page source for jimdo.com, jimdostatic.com, or jimdofree markers.
    """
    if not url:
        return False

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        src = resp.text.lower()

        markers = ["jimdo.com", "jimdostatic.com", "jimdofree"]
        for marker in markers:
            if marker in src:
                logger.info("Jimdo detected at %%s via marker: %%s", url, marker)
                return True
        return False
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("Jimdo detection failed for %%s: %%s", url, e)
        return False


_BLOCK_SELECTORS: tuple[str, ...] = (
    "section", "article", ".content-block", ".jimdo-section",
    ".j-content", ".j-text", ".cc-masonry-collection",
    ".j-html", ".cc-imagewrapper",
)
_TITLE_SELECTORS: tuple[str, ...] = (
    "h1", "h2", "h3", ".heading", ".title", ".j-title",
)
_PARAGRAPH_TAGS: tuple[str, ...] = (
    "p", ".text", ".j-text", ".description",
)


def _first_blocks(soup: BeautifulSoup) -> list[Any]:
    """Return first non-empty selector match across `_BLOCK_SELECTORS`."""
    for sel in _BLOCK_SELECTORS:
        blocks = soup.select(sel)
        if blocks:
            return list(blocks)
    return []


def _block_title(block: Any) -> str:
    """Pull a Jimdo block's heading via `_TITLE_SELECTORS`."""
    for tag in _TITLE_SELECTORS:
        el = block.select_one(tag)
        if not el:
            continue
        title = el.get_text(strip=True)
        if title:
            return title
    return ""


def _block_text(block: Any) -> str:
    """Pull joined paragraphs (>10 chars) or fallback block text."""
    parts: list[str] = []
    for p in block.find_all(list(_PARAGRAPH_TAGS)):
        p_text = p.get_text(strip=True)
        if p_text and len(p_text) > 10:
            parts.append(p_text)
    if parts:
        return " ".join(parts).strip()
    return block.get_text(separator=" ", strip=True).strip()


def _matches_query(title: str, text: str, query: Optional[str]) -> bool:
    """Case-insensitive query match against title or text."""
    if not query:
        return True
    q_lower = query.lower()
    return q_lower in title.lower() or q_lower in text.lower()


def _block_to_dict(block: Any, url: str, query: Optional[str]) -> Optional[Dict[str, Any]]:
    """Map one block to dict, applying query filter."""
    title = _block_title(block)
    text = _block_text(block)
    if not title and not text:
        return None
    if not _matches_query(title, text, query):
        return None
    return {"title": title, "text": text[:1000] if text else "", "url": url}


def _walk_blocks(soup: BeautifulSoup, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Walk structured blocks; return parsed dicts."""
    results: List[Dict[str, Any]] = []
    for block in _first_blocks(soup)[:limit]:
        try:
            entry = _block_to_dict(block, url, query)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Jimdo block parse error: %s", exc)
            continue
        if entry is not None:
            results.append(entry)
    return results


def _walk_paragraphs(soup: BeautifulSoup, url: str, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Fallback: walk top-level paragraphs >30 chars."""
    results: List[Dict[str, Any]] = []
    for p in soup.find_all("p")[:limit]:
        try:
            text = p.get_text(strip=True)
        except BROWSER_SCRAPE_EXCEPTIONS:
            continue
        if len(text) <= 30:
            continue
        if query and query.lower() not in text.lower():
            continue
        results.append({"title": "", "text": text[:1000], "url": url})
    return results


def scrape_jimdo_content(url: str, query: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Scrape content blocks from a Jimdo site (requests + BeautifulSoup)."""
    if not url:
        return []
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Jimdo scrape failed for %s: %s", url, exc)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = _walk_blocks(soup, url, query, limit)
    if not results:
        results = _walk_paragraphs(soup, url, query, limit)
    logger.info("Jimdo scrape: %d items from %s", len(results), url)
    return results[:limit]


