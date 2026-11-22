"""Pre-flight gates for /crawl: URL safety, cache, robots, breaker, learning, quality.

Owns the side-effect-light decisions the orchestrator makes before opening
a browser context, plus post-crawl outcome recording.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .cache import get_cache  # noqa: F401  -- callers reach for this re-export
from .intelligence import assess_content_quality, transfer_domain_knowledge
from .learning import (
    get_recommended_timeout,
    get_recommended_wait_strategy,
)
from .models import CrawlRequest, CrawlResponse
from .robots import is_allowed, should_wait
from .utils import classify_error, get_circuit_breaker, is_safe_url
from ._crawl4ai_runner import _crawl_via_crawl4ai

logger = logging.getLogger("crawlrx.crawl")


# ---------------------------------------------------------------------------
# Learning overrides
# ---------------------------------------------------------------------------


def _learning_domain(url: str) -> str:
    """Bare domain (no ``www.``) used as the learning-engine key."""
    from urllib.parse import urlparse as _urlparse_learn
    domain = _urlparse_learn(url).netloc or url
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _apply_learning_overrides(request: CrawlRequest, timeout: int) -> int:
    """Mutate ``request`` in-place with learned wait_for / timeout overrides."""
    domain = _learning_domain(request.url)
    if request.wait_for is None:
        learned_wait = get_recommended_wait_strategy(domain)
        if learned_wait:
            request.wait_for = learned_wait
            logger.debug("Learning: wait_strategy '%s' for %s", learned_wait, domain)
    learned_timeout = get_recommended_timeout(domain)
    if learned_timeout != 30000 and request.timeout <= 30:
        timeout = learned_timeout
        logger.debug("Learning: timeout %dms for %s", learned_timeout, domain)
    if request.wait_for is None and learned_timeout == 30000:
        transferred = transfer_domain_knowledge(domain)
        if transferred:
            request.wait_for = transferred.get("recommended_wait_strategy")
            timeout = transferred.get("recommended_timeout_ms", timeout)
            logger.debug(
                "Intelligence: transferred from %s to %s (wait=%s, timeout=%d)",
                transferred.get("source"), domain, request.wait_for, timeout,
            )
    return timeout


# ---------------------------------------------------------------------------
# Robots + delays
# ---------------------------------------------------------------------------


def _check_robots(request: CrawlRequest) -> Optional[CrawlResponse]:
    """Robots.txt enforcement; returns a CrawlResponse to abort, or None."""
    if not request.respect_robots:
        return None
    if not is_allowed(request.url):
        return CrawlResponse(
            success=False, url=request.url, error="URL blocked by robots.txt",
            error_type="blocked", elapsed_ms=0,
        )
    return None


async def _delay_for_robots(url: str, respect: bool) -> None:
    """Honour robots crawl-delay before issuing a request."""
    if not respect:
        return
    delay = should_wait(url)
    if delay > 0:
        logger.debug("Crawl delay: %.2fs for %s", delay, url)
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Circuit-breaker outcome handling
# ---------------------------------------------------------------------------


def _resolve_breaker(url: str) -> tuple[str, object]:
    """Return (domain, circuit_breaker) for the given URL."""
    from urllib.parse import urlparse as _urlparse
    domain = _urlparse(url).netloc or url
    return domain, get_circuit_breaker(domain)


def _record_breaker_outcome(raw: dict, breaker: object) -> None:
    """Update the per-domain circuit breaker based on a crawl result."""
    if raw.get("success"):
        breaker.record_success()
        return
    err_type = classify_error(raw.get("error", ""))
    raw.setdefault("error_type", err_type)
    if err_type != "blocked":
        breaker.record_failure()


def _maybe_override_quality(raw: dict, request_url: str) -> None:
    """Drop the success flag when self-assessed content quality is too low."""
    if not raw.get("success"):
        return
    quality = assess_content_quality(
        html=raw.get("html"), text=raw.get("text"),
        markdown=raw.get("markdown"), url=request_url,
    )
    if quality["quality_score"] >= 0.2:
        return
    raw["success"] = False
    raw["error"] = "Content quality too low: %s" % ", ".join(quality.get("issues", ["unknown"]))
    raw["error_type"] = "content_quality"
    logger.warning(
        "Auto-rejected crawl result for %s: quality=%.1f (%s)",
        request_url, quality["quality_score"], quality.get("issues"),
    )


# ---------------------------------------------------------------------------
# Canonical early-exit responses
# ---------------------------------------------------------------------------


def _blocked_url_response(url: str) -> CrawlResponse:
    """Build the canonical blocked-URL CrawlResponse."""
    return CrawlResponse(
        success=False, url=url,
        error="URL blocked: internal/private network addresses are not allowed",
        error_type="blocked", elapsed_ms=0,
    )


def _circuit_open_response(url: str, domain: str) -> CrawlResponse:
    """Build the canonical circuit-open CrawlResponse."""
    return CrawlResponse(
        success=False, url=url,
        error="Circuit open for domain: %s" % domain,
        error_type="circuit_open", elapsed_ms=0,
    )


def _cache_hit_response(cached: dict) -> CrawlResponse:
    """Build a CrawlResponse from a cached payload."""
    return CrawlResponse(**{**cached, "cache_hit": True})


# ---------------------------------------------------------------------------
# Pre-flight gate orchestration
# ---------------------------------------------------------------------------


async def _resolve_all_pre_crawl_gates(
    request: CrawlRequest, cache: Any, cache_key: Optional[str],
) -> Optional[CrawlResponse]:
    """Run every pre-flight gate (URL safety, engine, cache, robots, breaker)."""
    if not is_safe_url(request.url):
        return _blocked_url_response(request.url)
    if request.engine == "crawl4ai":
        return await _crawl_via_crawl4ai(request)
    if request.cache and cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Cache hit for crawl: %s", request.url)
            return _cache_hit_response(cached)
    robots_block = _check_robots(request)
    if robots_block is not None:
        return robots_block
    await _delay_for_robots(request.url, request.respect_robots)
    return None


def _resolve_breaker_or_circuit_open(url: str) -> tuple[Optional[CrawlResponse], str, Any]:
    """Resolve circuit-breaker; return (early_response_or_None, domain, breaker)."""
    domain, breaker = _resolve_breaker(url)
    if breaker.is_open():
        logger.warning(
            "CircuitBreaker OPEN for %s -- rejecting crawl of %s", domain, url,
        )
        return _circuit_open_response(url, domain), domain, breaker
    return None, domain, breaker
