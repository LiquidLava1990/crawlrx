"""
robots.py -- robots.txt checking for CrawlRX
============================================
Standard-compliant robots.txt parsing via urllib.robotparser.
In-memory caching with 1-hour TTL. Thread-safe.
"""

import logging
import random
import threading
import time
import urllib.robotparser
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse

from .config import RATE_LIMIT_JITTER, CRAWLRX_IGNORE_ROBOTS

logger = logging.getLogger("crawlrx.robots")

_ROBOTS_CACHE: Dict[str, Tuple[urllib.robotparser.RobotFileParser, float]] = {}
_ROBOTS_CACHE_LOCK = threading.Lock()
_ROBOTS_CACHE_TTL = 3600  # 1 hour


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _robots_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _get_or_fetch(url: str) -> Optional[urllib.robotparser.RobotFileParser]:
    """Return cached RobotFileParser or fetch fresh. Returns None on failure."""
    domain = _domain(url)
    now = time.time()

    with _ROBOTS_CACHE_LOCK:
        if domain in _ROBOTS_CACHE:
            parser, expires_at = _ROBOTS_CACHE[domain]
            if now < expires_at:
                return parser
            del _ROBOTS_CACHE[domain]

        try:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(_robots_url(url))
            rp.read()
            _ROBOTS_CACHE[domain] = (rp, now + _ROBOTS_CACHE_TTL)
            logger.info("robots.txt cached for %s (TTL %ds)", domain, _ROBOTS_CACHE_TTL)
            return rp
        except (OSError, ValueError, UnicodeDecodeError, AttributeError) as e:
            logger.warning("robots.txt fetch failed for %s: %s", domain, e)
            return None


def clear_robots_cache() -> None:
    with _ROBOTS_CACHE_LOCK:
        count = len(_ROBOTS_CACHE)
        _ROBOTS_CACHE.clear()
    logger.info("robots.txt cache cleared (%d entries)", count)


def get_robots_cache_stats() -> dict:
    with _ROBOTS_CACHE_LOCK:
        return {"cached_domains": len(_ROBOTS_CACHE), "ttl_seconds": _ROBOTS_CACHE_TTL}


def is_allowed(url: str, user_agent: str = "*") -> bool:
    """True if URL is allowed by robots.txt (or robots.txt is unavailable).
    
    If CRAWLRX_IGNORE_ROBOTS env var is set to true, always returns True
    (skips robots check entirely).
    """
    if CRAWLRX_IGNORE_ROBOTS:
        return True
    try:
        rp = _get_or_fetch(url)
        return rp is None or rp.can_fetch(user_agent, url)
    except (OSError, ValueError, AttributeError, UnicodeDecodeError) as e:
        logger.warning("robots.txt check failed for %s: %s -- allowing", url, e)
        return True


def get_crawl_delay(url: str, user_agent: str = "*") -> float:
    """Crawl delay in seconds from robots.txt, or 0 if not specified."""
    try:
        rp = _get_or_fetch(url)
        if rp is None:
            return 0.0
        delay = rp.crawl_delay(user_agent) or rp.crawl_delay("*")
        return float(delay) if delay is not None else 0.0
    except (OSError, ValueError, AttributeError, TypeError) as e:
        logger.warning("crawl_delay lookup failed for %s: %s", url, e)
        return 0.0


def should_wait(url: str, user_agent: str = "*") -> float:
    """
    Seconds to wait before crawling, with +/-RATE_LIMIT_JITTER applied.
    Returns 0.0 if no crawl delay is specified.
    Caller should: await asyncio.sleep(should_wait(url))
    """
    delay = get_crawl_delay(url, user_agent)
    if delay <= 0:
        return 0.0
    jitter = 1.0 + random.uniform(-RATE_LIMIT_JITTER, RATE_LIMIT_JITTER)
    return max(0.0, delay * jitter)


# Public alias kept for import compatibility
get_robots_url = _robots_url

