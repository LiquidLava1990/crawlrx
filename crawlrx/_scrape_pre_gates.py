"""Pre-flight gates for /scrape: URL safety, cache, robots, breaker, quality.

Owns the side-effect-light decisions the orchestrator makes before opening
a browser context, plus post-scrape outcome recording.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import CRAWLRX_IGNORE_ROBOTS  # noqa: F401  -- pinned by public API snapshot
from .intelligence import assess_content_quality
from .models import ScrapeRequest, ScrapeResponse
from .robots import is_allowed, should_wait
from .scrape_site_health import is_scrape_site_quarantined
from .utils import classify_error, get_circuit_breaker, is_safe_url

logger = logging.getLogger("crawlrx.scrape")


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------


async def _check_scrape_robots(request: ScrapeRequest, respect_robots: bool) -> Optional[ScrapeResponse]:
    """Robots.txt enforcement for /scrape; returns abort response or None."""
    if not respect_robots:
        return None
    if not is_allowed(request.url):
        return ScrapeResponse(
            success=False, url=request.url, error="URL blocked by robots.txt",
            error_type="blocked", elapsed_ms=0,
        )
    delay = should_wait(request.url)
    if delay > 0:
        logger.debug("Crawl delay: %.2fs for %s", delay, request.url)
        await asyncio.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Circuit-breaker outcome handling
# ---------------------------------------------------------------------------


def _resolve_scrape_breaker(url: str) -> tuple[str, object]:
    """Return (domain, circuit_breaker) for the given URL."""
    from urllib.parse import urlparse as _urlparse
    domain = _urlparse(url).netloc or url
    return domain, get_circuit_breaker(domain)


def _record_scrape_outcome(raw: dict, breaker: object) -> None:
    """Update breaker based on scrape result."""
    if raw.get("success"):
        breaker.record_success()
        return
    err_type = classify_error(raw.get("error", ""))
    raw.setdefault("error_type", err_type)
    if err_type != "blocked":
        breaker.record_failure()


def _maybe_override_scrape_quality(raw: dict, request_url: str) -> None:
    """Drop success when content quality is too low."""
    if not (raw.get("success") and raw.get("results")):
        return
    first = raw["results"][0] if raw["results"] else {}
    quality = assess_content_quality(
        html=first.get("html"), text=first.get("text"),
        markdown=first.get("markdown"), url=request_url,
    )
    if quality["quality_score"] >= 0.2:
        return
    raw["success"] = False
    raw["error"] = "Content quality too low: %s" % ", ".join(quality.get("issues", ["unknown"]))
    raw["error_type"] = "content_quality"
    logger.warning(
        "Auto-rejected scrape result for %s: quality=%.1f",
        request_url, quality["quality_score"],
    )


# ---------------------------------------------------------------------------
# Canonical early-exit responses
# ---------------------------------------------------------------------------


def _scrape_blocked_url_response(url: str) -> ScrapeResponse:
    """Build the canonical blocked-URL ScrapeResponse."""
    return ScrapeResponse(
        success=False, url=url,
        error="URL blocked: internal/private network addresses are not allowed",
        error_type="blocked", elapsed_ms=0,
    )


def _scrape_circuit_open_response(url: str, domain: str) -> ScrapeResponse:
    """Build the canonical circuit-open ScrapeResponse."""
    return ScrapeResponse(
        success=False, url=url,
        error="Circuit open for domain: %s" % domain,
        error_type="circuit_open", elapsed_ms=0,
    )


def _scrape_quarantined_site_response(url: str) -> ScrapeResponse:
    """Build a response for domains paused by the scrape site-health ledger."""
    return ScrapeResponse(
        success=False, url=url,
        error="Scrape site health is quarantined for this domain",
        error_type="quarantined", elapsed_ms=0,
        strategy="blocked",
        provenance={"site_health_quarantined": True},
    )


# ---------------------------------------------------------------------------
# Pre-flight gate orchestration
# ---------------------------------------------------------------------------


async def _resolve_pre_scrape_gates(
    request: ScrapeRequest, cache: Any, cache_key: Optional[str],
) -> Optional[ScrapeResponse]:
    """Run every pre-flight gate (URL safety, cache, robots) for /scrape."""
    if not is_safe_url(request.url):
        return _scrape_blocked_url_response(request.url)
    if is_scrape_site_quarantined(request.url):
        return _scrape_quarantined_site_response(request.url)
    if request.cache and cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for scrape: %s", request.url)
            return ScrapeResponse(**{**cached, "cache_hit": True})
    respect_robots = request.respect_robots and not CRAWLRX_IGNORE_ROBOTS
    robots_block = await _check_scrape_robots(request, respect_robots)
    if robots_block is not None:
        return robots_block
    return None


def _resolve_scrape_breaker_or_open(url: str) -> tuple[Optional[ScrapeResponse], Any]:
    """Resolve circuit-breaker for /scrape; return (early_response_or_None, breaker)."""
    domain, breaker = _resolve_scrape_breaker(url)
    if breaker.is_open():
        logger.warning(
            "CircuitBreaker OPEN for %s -- rejecting scrape of %s", domain, url,
        )
        return _scrape_circuit_open_response(url, domain), breaker
    return None, breaker
