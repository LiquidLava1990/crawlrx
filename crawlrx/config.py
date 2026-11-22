"""
CrawlRX Configuration
======================
All configuration for the CrawlRX API service.
"""

import json
import os
from .paths import env_path
from pathlib import Path
from typing import Any, Dict, Optional

# Server Configuration
PORT: int = 7337
HOST: str = "127.0.0.1"

# Chrome DevTools (CDP) endpoints for attaching to an already-running browser.
# CRAWLRX_CDP_URL is the default; CRAWLRX_CDP_PORTS maps names to ports, e.g.
#   CRAWLRX_CDP_PORTS='{"work": 9222, "personal": 9223}'
CDP_HOST: str = os.getenv("CRAWLRX_CDP_HOST", "localhost")
DEFAULT_CDP_PORT: int = int(os.getenv("CRAWLRX_CDP_PORT", "9222"))


def _load_named_cdp_ports() -> Dict[str, int]:
    raw = os.getenv("CRAWLRX_CDP_PORTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k).lower(): int(v) for k, v in parsed.items()}
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "CRAWLRX_CDP_PORTS must be a JSON object of name -> port, e.g. '{\"work\": 9222}'"
        ) from None


AGENT_CDP_PORTS: Dict[str, int] = _load_named_cdp_ports()


def get_cdp_url(agent: Optional[str] = None) -> str:
    """CDP URL for a named browser (CRAWLRX_CDP_PORTS) or the default one."""
    override = os.getenv("CRAWLRX_CDP_URL", "").strip()
    if override and not agent:
        return override.rstrip("/")
    port = AGENT_CDP_PORTS.get(agent.lower(), DEFAULT_CDP_PORT) if agent else DEFAULT_CDP_PORT
    return f"http://{CDP_HOST}:{port}"

# Timeouts and Retries
DEFAULT_TIMEOUT: int = 30
MAX_TIMEOUT: int = 120
DEFAULT_RETRIES: int = 2
RETRY_BACKOFF_BASE: float = 1.5

# Output Directories
OUTPUT_DIR: str = "./output"
CACHE_DIR: str = "./cache"

# Scraping Defaults
DEFAULT_WAIT_FOR: str = "networkidle"  # networkidle, domcontentloaded, selector
DEFAULT_SCROLL: bool = True
DEFAULT_INCLUDE_LINKS: bool = True
DEFAULT_INCLUDE_IMAGES: bool = False

# Rate Limiting
DEFAULT_RATE_LIMIT_RPM: int = 30  # Requests per minute per domain
RATE_LIMIT_JITTER: float = 0.2  # +/-20% jitter

# Session Management
DEFAULT_SESSION_MAX_REQUESTS: int = 100
DEFAULT_SESSION_MAX_AGE_MINUTES: int = 30

# Logging
LOG_LEVEL: str = "INFO"
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Content Processing
MAX_CONTENT_LENGTH: int = 2_000_000  # 2MB max content
EXTRACTED_LINKS_MAX: int = 1000
EXTRACTED_IMAGES_MAX: int = 500

# User Agents (rotated for stealth - 12+ real Chrome UA strings across Windows/Mac/Linux)
USER_AGENTS: list = [
    # Windows Chrome 131-127
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Mac Chrome 131-127
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

# Elements to strip from HTML (noise reduction)
STRIP_SELECTORS: list = [
    "nav", "header", "footer", "aside", "[role='navigation']",
    ".cookie-banner", ".cookie-consent", "#cookie-banner",
    ".ads", ".advertisement", ".ad-container", "[class*='ad-']",
    ".sidebar", ".widget", ".popup", ".modal",
    "script", "style", "noscript", "iframe",
    ".social-share", ".share-buttons", ".comments",
]

# Metadata extraction selectors
METADATA_SELECTORS: Dict[str, str] = {
    "title": "title",
    "description": "meta[name='description']",
    "og_title": "meta[property='og:title']",
    "og_description": "meta[property='og:description']",
    "og_image": "meta[property='og:image']",
    "og_url": "meta[property='og:url']",
    "twitter_title": "meta[name='twitter:title']",
    "twitter_description": "meta[name='twitter:description']",
    "twitter_image": "meta[name='twitter:image']",
    "canonical": "link[rel='canonical']",
    "author": "meta[name='author']",
    "keywords": "meta[name='keywords']",
    "favicon": "link[rel='icon'], link[rel='shortcut icon']",
}

# Docker service/container name for the SearXNG backend.
SEARXNG_CONTAINER_NAME = os.getenv("SEARXNG_CONTAINER_NAME", "crawlrx-searxng")

# SearXNG Configuration (CrawlRX self-hosted meta-search backend)
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")
SEARXNG_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "10"))
SOURCE_REGISTRY_PATH = Path(
    os.getenv(
        "CRAWLRX_SOURCE_REGISTRY",
        str(Path(__file__).with_name("source_registry.json")),
    )
)
_SOURCE_REGISTRY_FALLBACK: Dict[str, Any] = {
    "version": 1,
    "searxng": {
        "groups": {
            "web": {"engines": "bing,wikipedia"},
            "news": {"engines": "bing news,reuters,wikinews"},
            "images": {"engines": "bing images,openverse,wikicommons.images,lucide"},
            "videos": {"engines": "youtube,dailymotion,sepiasearch,wikicommons.videos"},
        }
    },
    "diagnostics": {
        "checks": [
            {"name": "web-core-openai", "source": "searxng", "query": "openai", "group": "web", "min_results": 3},
            {"name": "web-core-niche", "source": "searxng", "query": "playwright python", "group": "web", "min_results": 1},
            {"name": "news-core-openai", "source": "searxng", "query": "openai", "group": "news", "min_results": 1},
            {"name": "images-core-openai", "source": "searxng", "query": "openai", "group": "images", "min_results": 3},
            {"name": "videos-core-openai", "source": "searxng", "query": "openai", "group": "videos", "min_results": 3},
        ]
    },
}


def _load_source_registry() -> Dict[str, Any]:
    try:
        payload = json.loads(SOURCE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return dict(_SOURCE_REGISTRY_FALLBACK)
    return payload if isinstance(payload, dict) else dict(_SOURCE_REGISTRY_FALLBACK)


SOURCE_REGISTRY: Dict[str, Any] = _load_source_registry()


def source_group_engines(group: str, fallback: str = "") -> str:
    groups = SOURCE_REGISTRY.get("searxng", {}).get("groups", {})
    item = groups.get(group, {}) if isinstance(groups, dict) else {}
    value = item.get("engines") if isinstance(item, dict) else None
    return str(value or fallback)


def source_groups() -> Dict[str, str]:
    return {
        "web": source_group_engines("web", "bing,wikipedia"),
        "news": source_group_engines("news", "bing news,reuters,wikinews"),
        "images": source_group_engines(
            "images", "bing images,openverse,wikicommons.images,lucide",
        ),
        "videos": source_group_engines(
            "videos", "youtube,dailymotion,sepiasearch,wikicommons.videos",
        ),
    }


def source_diagnostic_checks() -> list[Dict[str, Any]]:
    checks = SOURCE_REGISTRY.get("diagnostics", {}).get("checks", [])
    if not isinstance(checks, list):
        checks = _SOURCE_REGISTRY_FALLBACK["diagnostics"]["checks"]
    return [dict(item) for item in checks if isinstance(item, dict)]


SEARXNG_WEB_ENGINES = os.getenv(
    "SEARXNG_WEB_ENGINES",
    source_group_engines("web", "bing,wikipedia"),
)
SEARXNG_NEWS_ENGINES = os.getenv(
    "SEARXNG_NEWS_ENGINES",
    source_group_engines("news", "bing news,reuters,wikinews"),
)
SEARXNG_IMAGE_ENGINES = os.getenv(
    "SEARXNG_IMAGE_ENGINES",
    source_group_engines("images", "bing images,openverse,wikicommons.images,lucide"),
)
SEARXNG_VIDEO_ENGINES = os.getenv(
    "SEARXNG_VIDEO_ENGINES",
    source_group_engines("videos", "youtube,dailymotion,sepiasearch,wikicommons.videos"),
)

# Persistent Cache Configuration
CACHE_DB_PATH = env_path("CRAWLRX_CACHE_DB", "crawlrx_cache.db")
CACHE_TTL: int = int(os.getenv("CRAWLRX_CACHE_TTL", "600"))

# Robots.txt bypass mode -- default TRUE for a scraping API (opt-in to robots.txt via request param)
CRAWLRX_IGNORE_ROBOTS: bool = os.getenv("CRAWLRX_IGNORE_ROBOTS", "false").lower() in ("true", "1", "yes")
