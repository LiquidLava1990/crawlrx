"""Helpers for the Twitter/X scraper.

Split out of ``twitter.py`` for readability: the three scrape
functions (profile, tweets, search) shared a 90+ shingle scroll-and-
extract loop and identical Playwright bootstrap. Centralising it here
removes the ``duplication checks`` triplet between them.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ._scraper_helpers import (
    SELECTOR_EXTRACTION_EXCEPTIONS,
    playwright_exception_types,
)

logger = logging.getLogger("crawlrx.twitter._helpers")

# Tuple of exception types that may bubble out of a Playwright session,
# resolved once at import time. The endpoint envelope also catches
# asyncio cancellation + parse errors so callers see one tuple.
PLAYWRIGHT_FAILURES: Tuple[type[BaseException], ...] = playwright_exception_types()
SCRAPE_BUBBLE_FAILURES: Tuple[type[BaseException], ...] = (
    PLAYWRIGHT_FAILURES
    + (
        asyncio.TimeoutError,
        asyncio.CancelledError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    )
)


# ---------------------------------------------------------------------------
# Pacing helpers
# ---------------------------------------------------------------------------

async def random_delay() -> None:
    """Random stealth delay in the 500-1500ms window."""
    await asyncio.sleep(random.uniform(0.5, 1.5))


def parse_count(text: Optional[str]) -> str:
    """Strip whitespace + commas from a Twitter count-string ("1.2K" etc.)."""
    if not text:
        return ""
    return text.strip().replace(",", "").replace(" ", "")


def elapsed_ms_since(start_time: float) -> int:
    """Return wall-clock ms since ``start_time`` (``time.time()``)."""
    return int((time.time() - start_time) * 1000)


# ---------------------------------------------------------------------------
# Tweet extraction
# ---------------------------------------------------------------------------

# Shared JS payload used by every per-tweet extractor. Lives at module
# scope to remove duplication between the profile / tweets / search
# scrape paths.
TWEET_EXTRACT_JS = """
(el) => {
    const data = {
        text: '',
        likes: '',
        retweets: '',
        replies: '',
        views: '',
        posted_at: '',
        tweet_url: '',
        is_retweet: false,
        media_urls: []
    };

    const retweetLabel = el.querySelector('[data-testid="socialContext"]');
    if (retweetLabel && retweetLabel.textContent.includes('reposted')) {
        data.is_retweet = true;
    }

    const textEl = el.querySelector('[data-testid="tweetText"]');
    if (textEl) {
        data.text = textEl.textContent.trim();
    }

    const linkEl = el.querySelector('a[href*="/status/"]');
    if (linkEl) {
        const href = linkEl.getAttribute('href');
        if (href) {
            data.tweet_url = 'https://twitter.com' + href.split('?')[0];
            const timeEl = el.querySelector('time');
            if (timeEl) {
                data.posted_at = timeEl.getAttribute('datetime') || timeEl.textContent.trim();
            }
        }
    }

    const buttons = el.querySelectorAll('[role="button"]');
    buttons.forEach(btn => {
        const label = btn.getAttribute('aria-label') || '';
        if (label.includes('replies') || label.includes('reply')) {
            const match = label.match(/([\\d,.KMB]+)/);
            if (match) data.replies = match[1];
        } else if (label.includes('repost') || label.includes('Retweet')) {
            const match = label.match(/([\\d,.KMB]+)/);
            if (match) data.retweets = match[1];
        } else if (label.includes('likes') || label.includes('Like')) {
            const match = label.match(/([\\d,.KMB]+)/);
            if (match) data.likes = match[1];
        } else if (label.includes('views') || label.includes('View')) {
            const match = label.match(/([\\d,.KMB]+)/);
            if (match) data.views = match[1];
        }
    });

    const images = el.querySelectorAll('img[src*="pbs.twimg.com"]');
    images.forEach(img => {
        const src = img.getAttribute('src');
        if (src && !src.includes('profile') && !src.includes('emoji')) {
            data.media_urls.push(src);
        }
    });

    return data;
}
"""


def extract_tweet_dict(tweet_el: "ElementHandle") -> dict:
    """Run the tweet extractor JS against ``tweet_el`` and return the dict."""
    return tweet_el.evaluate(TWEET_EXTRACT_JS)


# ---------------------------------------------------------------------------
# Scroll + collect (shared between tweets/search)
# ---------------------------------------------------------------------------

def _max_scroll_attempts(limit: int) -> int:
    """Upper bound on scroll passes for a given ``limit``."""
    return min(limit // 3 + 5, 15)


async def _wait_for_networkidle(page: Any, timeout_ms: int) -> None:
    """Best-effort networkidle wait; swallow only Playwright timeouts."""
    try:
        await page.wait_for_load_state(
            "networkidle", timeout=min(timeout_ms // 2, 10000)
        )
    except PlaywrightTimeoutError:
        return


async def _try_extract_tweets(
    page: Any,
    seen: List[dict],
    limit: int,
    where: str,
    tweet_factory: Callable[[dict], Any],
) -> bool:
    """Pull all currently-visible tweet cards into ``seen``.

    Returns False if no new tweet was added (caller stops scrolling).
    Each per-card extraction failure is logged at DEBUG and skipped --
    only the narrow ``SELECTOR_EXTRACTION_EXCEPTIONS`` family is
    swallowed; Playwright errors bubble out so the outer try/except
    catches them.
    """
    elements = await page.query_selector_all('[data-testid="tweet"]')
    start_count = len(seen)
    for el in elements[len(seen):limit]:
        try:
            tweet_data = extract_tweet_dict(el)
        except SELECTOR_EXTRACTION_EXCEPTIONS as exc:
            logger.debug("[Twitter] %s extract failed (%s): %s",
                         where, type(exc).__name__, exc)
            continue
        if tweet_data.get("text") or tweet_data.get("tweet_url"):
            seen.append(tweet_factory(tweet_data))
    return len(seen) > start_count


async def collect_tweets_via_scroll(
    page: "Page",
    limit: int,
    where: str,
    tweet_factory: Callable[[dict], Any],
) -> List[Any]:
    """Scroll the page until ``limit`` tweets are gathered (or attempts max).

    The previous implementation was duplicated three times across the
    twitter.py scrape paths. ``tweet_factory`` lifts the
    ``TwitterTweet(**dict)`` step out of the helper to keep the helper
    free of any Pydantic model import.
    """
    tweets: List[Any] = []
    last_count = 0
    scroll_attempts = 0
    max_scrolls = _max_scroll_attempts(limit)
    while len(tweets) < limit and scroll_attempts < max_scrolls:
        await _try_extract_tweets(page, tweets, limit, where, tweet_factory)
        if len(tweets) == last_count:
            break
        last_count = len(tweets)
        await page.evaluate("window.scrollBy(0, 800)")
        await random_delay()
        scroll_attempts += 1
    return tweets


# ---------------------------------------------------------------------------
# Page-load helpers (called from `async with async_playwright()` blocks)
# ---------------------------------------------------------------------------

async def navigate_with_pacing(
    page: "Page",
    target_url: str,
    timeout_ms: int,
    log_prefix: str,
) -> None:
    """Goto ``target_url``, sleep stealth, then best-effort networkidle."""
    logger.info("%s navigating to %s", log_prefix, target_url)
    await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
    await random_delay()
    await _wait_for_networkidle(page, timeout_ms)


# ---------------------------------------------------------------------------
# Endpoint outer-handler error envelope
# ---------------------------------------------------------------------------

# Anything that could bubble out of a scrape coroutine: asyncio
# timeouts (wait_for), cancellation, narrow Playwright failures, plus
# the scrape-failure family from _scraper_helpers.
ENDPOINT_FAILURES: Tuple[type[BaseException], ...] = SCRAPE_BUBBLE_FAILURES + (
    OSError, RuntimeError,
)


@dataclass(frozen=True)
class EndpointEnvelope:
    """Bundled args for :func:`run_with_endpoint_envelope`."""

    timeout_seconds: int
    failure_factory: Callable[[str, int], Any]
    request_timeout_ms: int
    where: str


async def run_with_endpoint_envelope(
    coro: Awaitable[Any],
    envelope: EndpointEnvelope,
) -> object:
    """Run ``coro`` with a timeout + narrow exception envelope."""
    try:
        return await asyncio.wait_for(coro, timeout=envelope.timeout_seconds)
    except ENDPOINT_FAILURES as exc:
        logger.error("[Twitter] %s request failed (%s): %s",
                     envelope.where, type(exc).__name__, exc)
        return envelope.failure_factory(
            f"Request failed: {exc}", envelope.request_timeout_ms
        )
