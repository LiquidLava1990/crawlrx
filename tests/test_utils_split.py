"""Capture utils.py public API before splitting into focused sibling modules.

RED test: every name listed below must remain importable from
``crawlrx.utils`` after the split. The shim module
re-exports from sibling ``_utils_*`` modules, so callers in 42 sibling
files keep working unchanged.

Source-of-truth for the API list: snapshot file
the original public-API snapshot under the key
``crawlrx.utils``.
"""
from __future__ import annotations

import importlib
from typing import Iterable


# ---------------------------------------------------------------------------
# Public-API contract -- every entry MUST resolve via the utils shim
# ---------------------------------------------------------------------------

# Module-level constants / re-exported aliases (data + flags + selector tables).
_PUBLIC_DATA: tuple[str, ...] = (
    "BOT_DETECTION_SIGNALS",
    "BROWSER_SCRAPE_EXCEPTIONS",
    "BS4_AVAILABLE",
    "ELEMENT_PROBE_EXCEPTIONS",
    "HTML2TEXT_AVAILABLE",
    "MARKDOWNIFY_AVAILABLE",
    "MAX_PAGE_BYTES",
    "METADATA_SELECTORS",
    "PARSE_EXCEPTIONS",
    "PLAYWRIGHT_EXCEPTIONS",
    "SCRAPE_FAILURE_EXCEPTIONS",
    "SCREENSHOT_DIR",
    "STEALTH_DEVICE_MEMORY",
    "STEALTH_HARDWARE_CONCURRENCY",
    "STEALTH_TIMEZONES",
    "STEALTH_VIEWPORTS",
    "STRIP_SELECTORS",
)

# Public callables (functions + class).
_PUBLIC_CALLABLES: tuple[str, ...] = (
    "CircuitBreaker",
    "check_content_size",
    "circuit_protected",
    "classify_error",
    "clean_text",
    "count_words",
    "create_stealth_context",
    "detect_bot_block",
    "ensure_cdp_browser",
    "extract_images",
    "extract_links",
    "extract_metadata",
    "extract_readable_content",
    "extract_structured_data",
    "extract_text",
    "extract_title",
    "get_all_circuit_breakers",
    "get_cdp_page",
    "get_circuit_breaker",
    "html_to_markdown",
    "human_mouse_movements",
    "infinite_scroll",
    "is_safe_url",
    "random_scroll_pause",
    "run_sync",
    "run_sync_with_timeout",
    "safe_goto",
    "save_failure_screenshot",
    "stealth_viewport",
    "strip_noise_elements",
    "timer_ms",
    "truncate_text",
    "with_retry",
)

# Private helpers depended on by sibling crawlrx modules
# (e.g. ``crawlrx.metadata`` imports ``_get_stealth_init_script``).
_INTERNAL_BUT_REEXPORTED: tuple[str, ...] = (
    "_get_stealth_init_script",
    "_RetryPolicy",
    "_retry_one_attempt",
    "_run_with_retry",
)


def _all_expected() -> Iterable[str]:
    yield from _PUBLIC_DATA
    yield from _PUBLIC_CALLABLES
    yield from _INTERNAL_BUT_REEXPORTED


def test_utils_public_api_preserved() -> None:
    """Every name remains importable from the utils shim post-split."""
    mod = importlib.import_module("crawlrx.utils")
    missing = [name for name in _all_expected() if not hasattr(mod, name)]
    assert not missing, (
        "utils shim lost re-exports after split: "
        + ", ".join(missing)
    )


def test_utils_public_callables_callable() -> None:
    """Every callable name resolves to a callable object (sanity check)."""
    mod = importlib.import_module("crawlrx.utils")
    not_callable = [
        name for name in _PUBLIC_CALLABLES
        if not callable(getattr(mod, name, None))
    ]
    assert not not_callable, (
        "utils shim re-exports lost callability: "
        + ", ".join(not_callable)
    )


def test_utils_max_page_bytes_value() -> None:
    """Value-bearing constants survive the split with their actual values."""
    mod = importlib.import_module("crawlrx.utils")
    assert mod.MAX_PAGE_BYTES == 10 * 1024 * 1024
    assert "captcha" in mod.BOT_DETECTION_SIGNALS
    assert isinstance(mod.METADATA_SELECTORS, dict)
    assert "title" in mod.METADATA_SELECTORS
