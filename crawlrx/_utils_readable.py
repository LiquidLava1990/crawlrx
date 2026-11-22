"""Readable content extraction: readability-lxml > BS4 > regex fallback.

Lifted out of ``_utils_html`` to keep that module small.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

from ._utils_html import (
    BS4_AVAILABLE,
    BeautifulSoup,
    clean_text,
    count_words,
    html_to_markdown,
    strip_noise_elements,
)

_HTML_CONVERT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, RuntimeError, OSError,
)

logger = logging.getLogger(__name__)


def _readable_via_readability(html: str, result: Dict[str, Any]) -> bool:
    """Populate ``result`` via readability-lxml. True on success."""
    try:
        from readability import Document
    except ImportError:
        logger.debug("readability-lxml not available, using fallback extraction")
        return False
    try:
        doc = Document(html)
        result["title"] = doc.title() or ""
        readable_html = doc.summary()
        result["html"] = readable_html
        result["text"] = _strip_html_text(readable_html)
        result["word_count"] = count_words(result["text"])
        return True
    except _HTML_CONVERT_EXCEPTIONS as exc:
        logger.warning("readability-lxml extraction failed: %s", exc)
        return False


def _strip_html_text(readable_html: str) -> str:
    """Convert cleaned HTML to plain text (BS4, or regex fallback)."""
    if BS4_AVAILABLE:
        soup = BeautifulSoup(readable_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return clean_text(soup.get_text(separator="\n", strip=True))
    plain = re.sub(r"<[^>]+>", " ", readable_html)
    return clean_text(re.sub(r"\s+", " ", plain))


_READABLE_MAIN_SELECTORS = ("main", "article")
_READABLE_DIV_CLASSES = ("content", "post", "entry")


def _pick_main_content(soup: "BeautifulSoup") -> Any:
    """Find the most likely article body in a parsed soup."""
    for tag_name in _READABLE_MAIN_SELECTORS:
        found = soup.find(tag_name)
        if found is not None:
            return found
    for cls in _READABLE_DIV_CLASSES:
        found = soup.find("div", class_=cls)
        if found is not None:
            return found
    return soup.body


def _readable_via_bs4(html: str, result: Dict[str, Any]) -> None:
    """BS4 fallback: parse, strip noise, extract main content text."""
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    result["title"] = title_tag.get_text(strip=True) if title_tag else ""
    soup = strip_noise_elements(soup)
    main_content = _pick_main_content(soup)
    if main_content is None:
        result["text"] = clean_text(soup.get_text(separator="\n", strip=True))
        return
    for tag in main_content.find_all(["script", "style", "noscript"]):
        tag.decompose()
    result["html"] = str(main_content)
    result["text"] = clean_text(main_content.get_text(separator="\n", strip=True))


def _readable_ultimate_fallback(html: str, url: str, result: Dict[str, Any]) -> None:
    """Last-resort regex strip when BS4 unavailable or all else fails."""
    if not BS4_AVAILABLE:
        result["text"] = html_to_markdown(html, base_url=url)
        result["html"] = html
        return
    plain = re.sub(r"<[^>]+>", " ", html)
    result["text"] = clean_text(re.sub(r"\s+", " ", plain))


def _readable_bs4_or_fallback(html: str, url: str, result: Dict[str, Any]) -> None:
    """Run bs4 extraction (when available); fall back on parser exceptions."""
    if not BS4_AVAILABLE:
        _readable_ultimate_fallback(html, url, result)
        return
    try:
        _readable_via_bs4(html, result)
        return
    except _HTML_CONVERT_EXCEPTIONS as exc:
        logger.error("Fallback content extraction failed: %s", exc)
        _readable_ultimate_fallback(html, url, result)
        return  # explicit recovery: bs4 parser failed; ultimate-fallback fills result


def extract_readable_content(html: str, url: str = "") -> dict:
    """Extract clean readable content (title, text, html, word_count)."""
    if not html:
        return {"title": "", "text": "", "html": "", "word_count": 0}
    result: Dict[str, Any] = {"title": "", "text": "", "html": "", "word_count": 0}
    if _readable_via_readability(html, result):
        return result
    _readable_bs4_or_fallback(html, url, result)
    result["word_count"] = count_words(result["text"])
    return result
