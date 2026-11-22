"""Capture crawl.py public API before splitting into focused sibling modules.

RED test: every name listed below must remain importable from
``crawlrx.crawl`` after the split. The shim module
re-exports from sibling ``_crawl_*`` modules so callers
(``crawlrx.batch._do_crawl``, ``crawlrx.main.crawl_router``) keep
working unchanged.

Source-of-truth for the public API list: snapshot file
the original public-API snapshot under the key
``crawlrx.crawl``.

Internal-but-re-exported entries are the helpers ``crawl()`` needs to
call across split-module boundaries. They MUST stay reachable through
the shim so monolith-era debugging / introspection still works.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterable


_CRAWL_MODULE: str = "crawlrx.crawl"
_CRAWL_FILE: Path = Path(
    Path(__file__).resolve().parents[1] / "crawlrx/crawl.py"
)


# ---------------------------------------------------------------------------
# Public-API contract -- pinned by snapshots/public_api.json
# ---------------------------------------------------------------------------

# Re-exported aliases / data (already counted as "public" by the harness).
_PUBLIC_DATA: tuple[str, ...] = (
    "BROWSER_SCRAPE_EXCEPTIONS",
    "PARSE_EXCEPTIONS",
    "PLAYWRIGHT_EXCEPTIONS",
    "USER_AGENTS",
)

# Public callables + dataclass.
_PUBLIC_CALLABLES: tuple[str, ...] = (
    "CrawlAttemptParams",
    "close_attempt",
    "crawl",
)

# Internal helpers that other modules import (e.g. batch.py uses _do_crawl,
# main.py uses ``router``). Splitting must keep these reachable.
_INTERNAL_BUT_REEXPORTED: tuple[str, ...] = (
    "_do_crawl",
    "_do_crawl_attempt",
    "_try_one_crawl_attempt",
    "_make_crawl_context",
    "_crawl_via_crawl4ai",
    "_crawl4ai_extract",
    "_crawl4ai_screenshot",
    "_crawl4ai_payload",
    "_crawl4ai_render_page",
    "_apply_learning_overrides",
    "_check_robots",
    "_resolve_breaker",
    "_resolve_breaker_or_circuit_open",
    "_record_breaker_outcome",
    "_maybe_override_quality",
    "_resolve_all_pre_crawl_gates",
    "_execute_crawl_with_timeout",
    "_blocked_url_response",
    "_circuit_open_response",
    "_cache_hit_response",
    "router",
)


def _all_expected() -> Iterable[str]:
    yield from _PUBLIC_DATA
    yield from _PUBLIC_CALLABLES
    yield from _INTERNAL_BUT_REEXPORTED


def test_crawl_public_api_preserved() -> None:
    """Every name remains importable from the crawl shim post-split."""
    mod = importlib.import_module(_CRAWL_MODULE)
    missing = [name for name in _all_expected() if not hasattr(mod, name)]
    assert not missing, (
        "crawl shim lost re-exports after split: " + ", ".join(missing)
    )


def test_crawl_public_callables_are_callable() -> None:
    """Every callable name resolves to a callable object (sanity check)."""
    mod = importlib.import_module(_CRAWL_MODULE)
    not_callable = [
        name for name in _PUBLIC_CALLABLES
        if not callable(getattr(mod, name, None))
    ]
    assert not not_callable, (
        "crawl shim re-exports lost callability: "
        + ", ".join(not_callable)
    )


def test_crawl_router_has_post_crawl_route() -> None:
    """The FastAPI router must still expose POST /crawl after the split."""
    mod = importlib.import_module(_CRAWL_MODULE)
    router = mod.router
    paths = {
        getattr(r, "path", None) for r in getattr(router, "routes", [])
    }
    assert "/crawl" in paths, (
        "router lost /crawl POST route after split; "
        f"current paths={sorted(p for p in paths if p)}"
    )


def test_crawl_attempt_params_is_dataclass() -> None:
    """``CrawlAttemptParams`` must remain a frozen dataclass."""
    mod = importlib.import_module(_CRAWL_MODULE)
    import dataclasses
    assert dataclasses.is_dataclass(mod.CrawlAttemptParams)
    fields = {f.name for f in dataclasses.fields(mod.CrawlAttemptParams)}
    # Spot-check load-bearing fields used by _execute_crawl_with_timeout.
    for required in ("url", "wait_for", "timeout", "javascript", "stealth"):
        assert required in fields, (
            f"CrawlAttemptParams lost field '{required}' after split"
        )


def test_crawl_orchestrator_is_thin() -> None:
    """Post-split ``crawl.py`` must stay <=300 LOC (was 803)."""
    loc = sum(1 for _ in _CRAWL_FILE.read_text(encoding="utf-8").splitlines())
    assert loc <= 300, (
        f"crawl.py is still {loc} LOC; split into _crawl_*.py siblings."
    )


def test_crawl_split_siblings_exist_and_are_lean() -> None:
    """The four planned sibling modules must exist and stay <=500 LOC each."""
    pkg = _CRAWL_FILE.parent
    siblings = (
        "_crawl_pre_gates.py",
        "_crawl_attempt.py",
        "_crawl4ai_runner.py",
    )
    for name in siblings:
        path = pkg / name
        assert path.exists(), f"missing sibling: {path}"
        loc = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        assert loc <= 500, f"sibling {name} is {loc} LOC; trim further."
