"""CrawlRX utilities -- thin re-export shim over focused sibling modules.

The implementation lives in focused ``_utils_*`` siblings; this shim
re-exports the public surface so callers import from one place.

Sibling modules:
  - ``_utils_async``     -- ``run_sync``, ``run_sync_with_timeout``, ``timer_ms``
  - ``_utils_retry``     -- ``with_retry`` decorator + retry primitives
  - ``_utils_cdp``       -- ``ensure_cdp_browser``, ``get_cdp_page``,
                            ``safe_goto``, ``extract_text``
  - ``_utils_safety``    -- ``check_content_size``, ``detect_bot_block``,
                            ``is_safe_url``, ``classify_error``
  - ``_utils_circuit``   -- ``CircuitBreaker``, ``circuit_protected``,
                            ``get_circuit_breaker``, ``get_all_circuit_breakers``
  - ``_utils_stealth``   -- ``create_stealth_context``, ``stealth_viewport``
  - ``_utils_html``      -- HTML cleanup, links/images, markdown conversion
  - ``_utils_readable``  -- ``extract_readable_content`` pipeline
  - ``_utils_metadata``  -- ``extract_metadata``, ``METADATA_SELECTORS``
  - ``_utils_scraper``   -- mouse / scroll / infinite-scroll / screenshots
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export the shared exception aliases used by sibling crawlrx modules.
from ._scraper_helpers import (
    BROWSER_SCRAPE_EXCEPTIONS,
    ELEMENT_PROBE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    PLAYWRIGHT_EXCEPTIONS,
    SCRAPE_FAILURE_EXCEPTIONS,
)

# Async / sync bridge + timing helpers
from ._utils_async import run_sync, run_sync_with_timeout, timer_ms

# Retry primitives + decorator
from ._utils_retry import (
    _RetryPolicy,
    _retry_one_attempt,
    _run_with_retry,
    with_retry,
)

# CDP + Playwright helpers
from ._utils_cdp import (
    ensure_cdp_browser,
    extract_text,
    get_cdp_page,
    safe_goto,
)

# Safety guards (page-size, bot-block, SSRF, error classification)
from ._utils_safety import (
    BOT_DETECTION_SIGNALS,
    MAX_PAGE_BYTES,
    check_content_size,
    classify_error,
    detect_bot_block,
    is_safe_url,
)

# Circuit breaker
from ._utils_circuit import (
    CircuitBreaker,
    circuit_protected,
    get_all_circuit_breakers,
    get_circuit_breaker,
)

# Stealth context + viewport
from ._utils_stealth import (
    STEALTH_DEVICE_MEMORY,
    STEALTH_HARDWARE_CONCURRENCY,
    STEALTH_TIMEZONES,
    STEALTH_VIEWPORTS,
    _get_stealth_init_script,
    create_stealth_context,
    stealth_viewport,
)

# HTML utilities (cleanup, links/images, markdown, structured data, title)
from ._utils_html import (
    BS4_AVAILABLE,
    HTML2TEXT_AVAILABLE,
    MARKDOWNIFY_AVAILABLE,
    STRIP_SELECTORS,
    clean_text,
    count_words,
    extract_images,
    extract_links,
    extract_readable_content,
    extract_structured_data,
    extract_title,
    html_to_markdown,
    strip_noise_elements,
    truncate_text,
)

# Metadata extraction
from ._utils_metadata import METADATA_SELECTORS, extract_metadata

# Scraper hardening (human-like behavior + screenshots)
from ._utils_scraper import (
    SCREENSHOT_DIR,
    human_mouse_movements,
    infinite_scroll,
    random_scroll_pause,
    save_failure_screenshot,
)

if TYPE_CHECKING:
    # Kept for backward-compat: the original utils.py had this guard.
    from playwright.async_api import (  # noqa: F401
        Browser,
        BrowserContext,
        ElementHandle,
        Page,
        Playwright,
    )

__all__ = [
    # exceptions
    "BROWSER_SCRAPE_EXCEPTIONS",
    "ELEMENT_PROBE_EXCEPTIONS",
    "PARSE_EXCEPTIONS",
    "PLAYWRIGHT_EXCEPTIONS",
    "SCRAPE_FAILURE_EXCEPTIONS",
    # async / timing
    "run_sync",
    "run_sync_with_timeout",
    "timer_ms",
    # retry
    "with_retry",
    "_RetryPolicy",
    "_retry_one_attempt",
    "_run_with_retry",
    # cdp + playwright
    "ensure_cdp_browser",
    "get_cdp_page",
    "safe_goto",
    "extract_text",
    # safety
    "MAX_PAGE_BYTES",
    "BOT_DETECTION_SIGNALS",
    "check_content_size",
    "detect_bot_block",
    "is_safe_url",
    "classify_error",
    # circuit
    "CircuitBreaker",
    "circuit_protected",
    "get_circuit_breaker",
    "get_all_circuit_breakers",
    # stealth
    "STEALTH_TIMEZONES",
    "STEALTH_HARDWARE_CONCURRENCY",
    "STEALTH_DEVICE_MEMORY",
    "STEALTH_VIEWPORTS",
    "stealth_viewport",
    "create_stealth_context",
    "_get_stealth_init_script",
    # html
    "BS4_AVAILABLE",
    "MARKDOWNIFY_AVAILABLE",
    "HTML2TEXT_AVAILABLE",
    "STRIP_SELECTORS",
    "clean_text",
    "count_words",
    "truncate_text",
    "strip_noise_elements",
    "html_to_markdown",
    "extract_links",
    "extract_images",
    "extract_title",
    "extract_structured_data",
    "extract_readable_content",
    # metadata
    "METADATA_SELECTORS",
    "extract_metadata",
    # scraper
    "SCREENSHOT_DIR",
    "human_mouse_movements",
    "random_scroll_pause",
    "infinite_scroll",
    "save_failure_screenshot",
]
