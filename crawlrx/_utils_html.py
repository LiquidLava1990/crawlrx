"""HTML utilities: cleanup, links/images, markdown conversion, readability.

BeautifulSoup-driven: every function falls back gracefully when ``bs4``
is unavailable. Splits cleanly between the static helpers (cleanup,
link/image extraction) and the conversion pipelines (markdown, readable).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from ._scraper_helpers import PARSE_EXCEPTIONS

# ---------------------------------------------------------------------------
# Optional deps -- BS4, markdownify, html2text
# ---------------------------------------------------------------------------

try:
    from bs4 import BeautifulSoup, Tag
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    BeautifulSoup = None  # type: ignore[assignment, misc]  because bs4 is optional; None sentinel gates BS4_AVAILABLE callers
    Tag = None  # type: ignore[assignment, misc]  because bs4 is optional; None sentinel gates BS4_AVAILABLE callers

try:
    from markdownify import markdownify as md
    MARKDOWNIFY_AVAILABLE = True
except ImportError:
    MARKDOWNIFY_AVAILABLE = False

try:
    import html2text
    HTML2TEXT_AVAILABLE = True
except ImportError:
    HTML2TEXT_AVAILABLE = False


_BS4_PARSE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, IndexError, LookupError,
)
_HTML_CONVERT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AttributeError, TypeError, ValueError, KeyError, RuntimeError, OSError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Collapse whitespace, strip empty lines, trim outer whitespace."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    text = '\n'.join(line for line in text.split('\n') if line.strip())
    return text


def count_words(text: str) -> int:
    """Count whitespace-separated words in ``text``."""
    if not text:
        return 0
    return len(text.split())


def truncate_text(text: str, max_length: int = 500) -> str:
    """Truncate ``text`` to ``max_length`` with an ellipsis at a word boundary."""
    if len(text) <= max_length:
        return text
    return text[:max_length].rsplit(' ', 1)[0] + "..."


# ---------------------------------------------------------------------------
# Noise stripping + link absolutising
# ---------------------------------------------------------------------------

STRIP_SELECTORS = [
    "nav", "header", "footer", "aside", "[class*='nav']", "[class*='header']",
    "[class*='footer']", "[class*='sidebar']", "[class*='cookie']", "[class*='popup']",
    "[id*='nav']", "[id*='header']", "[id*='footer']", "[id*='sidebar']",
    "script", "style", "noscript", "iframe", "object", "embed",
    ".advertisement", ".ad", "[class*='ad-']", "[class*='ads']",
    "[class*='social']", "[class*='share']", "[class*='newsletter']",
]


def strip_noise_elements(soup: "BeautifulSoup") -> "BeautifulSoup":
    """Remove navigation, ads, cookies, scripts, and other noise from ``soup``."""
    if not BS4_AVAILABLE:
        return soup

    for selector in STRIP_SELECTORS:
        try:
            for element in soup.select(selector):
                element.decompose()
        except _BS4_PARSE_EXCEPTIONS as exc:
            logger.debug("Suppressed error: %s", exc)

    for comment in soup.find_all(
        string=lambda text: isinstance(text, str) and text.strip().startswith('<!--')
    ):
        comment.extract()

    return soup


def _absolutise_links(soup: "BeautifulSoup", base_url: str) -> None:
    """Rewrite href/src on a soup tree to absolute URLs (in place)."""
    if not base_url:
        return
    for tag in soup.find_all(["a", "img"]):
        if tag.get("href"):
            tag["href"] = urljoin(base_url, tag["href"])
        if tag.get("src"):
            tag["src"] = urljoin(base_url, tag["src"])


# ---------------------------------------------------------------------------
# HTML -> Markdown pipeline
# ---------------------------------------------------------------------------


def _try_markdownify(html_clean: str) -> Optional[str]:
    """Convert via markdownify; None if unavailable or failure."""
    if not MARKDOWNIFY_AVAILABLE:
        return None
    try:
        markdown = md(
            html_clean,
            heading_style="ATX",
            bullets="-",
            strip=["script", "style", "nav", "header", "footer", "aside"],
        )
        return clean_text(markdown)
    except _HTML_CONVERT_EXCEPTIONS as exc:
        logger.warning("markdownify failed: %s", exc)
        return None


def _try_html2text(html_clean: str) -> Optional[str]:
    """Convert via html2text; None if unavailable or failure."""
    if not HTML2TEXT_AVAILABLE:
        return None
    try:
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.ignore_tables = False
        converter.body_width = 0
        converter.wrap_links = False
        return clean_text(converter.handle(html_clean))
    except _HTML_CONVERT_EXCEPTIONS as exc:
        logger.warning("html2text failed: %s", exc)
        return None


def _prepare_markdown_soup(html: str, base_url: str) -> "BeautifulSoup":
    """Parse, denoise, narrow to main content, absolutise links."""
    soup = BeautifulSoup(html, "html.parser")
    soup = strip_noise_elements(soup)
    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_="content")
    )
    if main_content:
        soup = BeautifulSoup(str(main_content), "html.parser")
    _absolutise_links(soup, base_url)
    return soup


def html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert HTML to clean Markdown (markdownify > html2text > BS4 fallback)."""
    if not html:
        return ""
    if not BS4_AVAILABLE:
        return re.sub(r"<[^>]+>", "", html)
    soup = _prepare_markdown_soup(html, base_url)
    html_clean = str(soup)
    via_md = _try_markdownify(html_clean)
    if via_md is not None:
        return via_md
    via_h2t = _try_html2text(html_clean)
    if via_h2t is not None:
        return via_h2t
    return _bs4_to_markdown(soup)


# ---------------------------------------------------------------------------
# BS4 -> markdown fallback formatters
# ---------------------------------------------------------------------------

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_BLOCK_PARENT_TAGS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6")


def _format_heading(elem: "Tag", text: str) -> List[str]:
    level = int(elem.name[1])
    return [f"{'#' * level} {text}"]


def _format_blockquote(_: "Tag", text: str) -> List[str]:
    return [f"> {line}" for line in text.split("\n")]


def _format_paragraph(_: "Tag", text: str) -> List[str]:
    return [text]


def _format_listitem(_: "Tag", text: str) -> List[str]:
    return [f"- {text}"]


def _format_pre(elem: "Tag", _: str) -> List[str]:
    return [f"```\n{elem.get_text()}\n```"]


def _format_anchor(elem: "Tag", text: str) -> Optional[List[str]]:
    href = elem.get("href")
    if not href:
        return None
    return [f"[{text}]({href})"]


def _format_loose_text(elem: "Tag", text: str) -> Optional[List[str]]:
    parent = elem.parent
    if parent is None or parent.name in _BLOCK_PARENT_TAGS or len(text) <= 20:
        return None
    return [text]


_BS4_TAG_FORMATTERS: Dict[str, Callable[..., List[str]]] = {
    "p": _format_paragraph,
    "li": _format_listitem,
    "pre": _format_pre,
    "blockquote": _format_blockquote,
}


def _format_bs4_tag(elem: "Tag", text: str) -> List[str]:
    """Render one BS4 Tag to markdown lines (returns 0..N lines)."""
    name = elem.name
    if name in _HEADING_TAGS:
        return _format_heading(elem, text)
    direct = _BS4_TAG_FORMATTERS.get(name)
    if direct is not None:
        return direct(elem, text)
    if name == "a":
        anchor = _format_anchor(elem, text)
        if anchor is not None:
            return anchor
    loose = _format_loose_text(elem, text)
    return loose if loose is not None else []


def _bs4_to_markdown(soup: "BeautifulSoup") -> str:
    """Fallback: convert BeautifulSoup to markdown-like text."""
    lines: List[str] = []
    for elem in soup.descendants:
        if not isinstance(elem, Tag):
            continue
        text = elem.get_text(strip=True)
        if not text:
            continue
        lines.extend(_format_bs4_tag(elem, text))
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Link + image extraction
# ---------------------------------------------------------------------------


def _normalize_link(href: str, base_url: str) -> Optional[str]:
    """Resolve ``href`` against ``base_url`` and filter unsupported schemes."""
    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    full_url = urljoin(base_url, href)
    if urlparse(full_url).scheme not in ("http", "https"):
        return None
    return full_url


def extract_links(
    html: str,
    base_url: str,
    same_domain: bool = False,
    max_count: int = 500,
) -> List[str]:
    """Extract unique absolute links (optionally same-domain only)."""
    if not BS4_AVAILABLE or not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    links: set[str] = set()
    base_domain = urlparse(base_url).netloc.lower() if base_url else ""

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'].strip()
        candidate = _normalize_link(href, base_url)
        if candidate is None:
            continue
        if same_domain and urlparse(candidate).netloc.lower() != base_domain:
            continue
        links.add(candidate)

    result = sorted(list(links))
    if len(result) > max_count:
        logger.debug("extract_links: truncated %d links to %d (max_count)", len(result), max_count)
        result = result[:max_count]
    return result


def _normalize_image(raw: str, base_url: str) -> Optional[str]:
    """Resolve image src/data-src against ``base_url``, drop ``data:`` URIs."""
    src = (raw or "").strip()
    if not src or src.startswith("data:"):
        return None
    full_url = urljoin(base_url, src)
    if urlparse(full_url).scheme not in ("http", "https"):
        return None
    return full_url


def _collect_images_from_soup(soup: "BeautifulSoup", base_url: str) -> set[str]:
    """Walk soup for ``img[src]`` plus lazy-load attribute variants."""
    images: set[str] = set()
    for img in soup.find_all('img', src=True):
        candidate = _normalize_image(img['src'], base_url)
        if candidate is not None:
            images.add(candidate)
    for img in soup.find_all('img'):
        for attr in ('data-src', 'data-original', 'data-lazy-src'):
            raw = img.get(attr)
            if not raw:
                continue
            candidate = _normalize_image(raw, base_url)
            if candidate is not None:
                images.add(candidate)
    return images


def extract_images(html: str, base_url: str, max_count: int = 500) -> List[str]:
    """Extract unique absolute image URLs (lazy-load attributes included)."""
    if not BS4_AVAILABLE or not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    images = _collect_images_from_soup(soup, base_url)
    result = sorted(list(images))
    if len(result) > max_count:
        logger.debug(
            "extract_images: truncated %d images to %d (max_count)",
            len(result), max_count,
        )
        result = result[:max_count]
    return result


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


def extract_title(html: str) -> Optional[str]:
    """Extract page title from HTML (BS4 first, regex fallback)."""
    if not html:
        return None
    if BS4_AVAILABLE:
        try:
            soup = BeautifulSoup(html, "html.parser")
            tag = soup.find("title")
            return tag.get_text(strip=True) if tag else None
        except _BS4_PARSE_EXCEPTIONS as exc:
            logger.debug("Suppressed error: %s", exc)
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Structured data (JSON-LD, Open Graph, Schema.org)
# ---------------------------------------------------------------------------


def extract_structured_data(html: str) -> dict:
    """Extract JSON-LD, Open Graph, and Schema.org data from HTML."""
    import json
    result: dict = {"json_ld": [], "open_graph": {}, "schema_org": []}
    if not BS4_AVAILABLE:
        return result

    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            result["json_ld"].append(data)
        except PARSE_EXCEPTIONS as exc:
            logger.debug("JSON-LD parse error: %s", exc)

    open_graph: dict = result["open_graph"]
    for meta in soup.find_all("meta", property=lambda x: x and x.startswith("og:")):
        key = meta.get("property", "").replace("og:", "")
        open_graph[key] = meta.get("content", "")

    return result


# Readability extraction lives in ``_utils_readable`` to keep this file
# under the 500 LOC ceiling.
from ._utils_readable import extract_readable_content  # noqa: E402, F401
