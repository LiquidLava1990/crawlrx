"""Capture scrape.py public API before splitting into focused sibling modules.

RED test: every name listed below must remain importable from
``crawlrx.scrape`` after the split. The shim module
re-exports from sibling ``_scrape_*`` modules so callers
(``crawlrx.batch._do_scrape``, ``crawlrx.main.scrape_router``) keep
working unchanged.

Source-of-truth for the public API list: snapshot file
the original public-API snapshot under the key
``crawlrx.scrape``.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterable


_SCRAPE_MODULE: str = "crawlrx.scrape"
_SCRAPE_FILE: Path = Path(
    Path(__file__).resolve().parents[1] / "crawlrx/scrape.py"
)


# ---------------------------------------------------------------------------
# Public-API contract -- pinned by snapshots/public_api.json
# ---------------------------------------------------------------------------

# Re-exported aliases / data (already counted as "public" by the harness).
_PUBLIC_DATA: tuple[str, ...] = (
    "BROWSER_SCRAPE_EXCEPTIONS",
    "CRAWLRX_IGNORE_ROBOTS",
    "PARSE_EXCEPTIONS",
    "PLAYWRIGHT_EXCEPTIONS",
    "USER_AGENTS",
)

# Public callables + dataclass.
_PUBLIC_CALLABLES: tuple[str, ...] = (
    "ScrapeAttemptParams",
    "close_attempt",
    "scrape",
)

# Internal helpers other modules import (e.g. batch.py uses _do_scrape).
_INTERNAL_BUT_REEXPORTED: tuple[str, ...] = (
    "_do_scrape",
    "_do_scrape_attempt",
    "_try_one_scrape_attempt",
    "_make_scrape_context",
    "_scrape_via_smart_fetch",
    "_smart_fetch_eligible",
    "_check_scrape_robots",
    "_resolve_scrape_breaker",
    "_resolve_scrape_breaker_or_open",
    "_record_scrape_outcome",
    "_maybe_override_scrape_quality",
    "_resolve_pre_scrape_gates",
    "_execute_scrape_with_timeout",
    "_scrape_blocked_url_response",
    "_scrape_circuit_open_response",
    "router",
)


def _all_expected() -> Iterable[str]:
    yield from _PUBLIC_DATA
    yield from _PUBLIC_CALLABLES
    yield from _INTERNAL_BUT_REEXPORTED


def test_scrape_public_api_preserved() -> None:
    """Every name remains importable from the scrape shim post-split."""
    mod = importlib.import_module(_SCRAPE_MODULE)
    missing = [name for name in _all_expected() if not hasattr(mod, name)]
    assert not missing, (
        "scrape shim lost re-exports after split: " + ", ".join(missing)
    )


def test_scrape_public_callables_are_callable() -> None:
    """Every callable name resolves to a callable object (sanity check)."""
    mod = importlib.import_module(_SCRAPE_MODULE)
    not_callable = [
        name for name in _PUBLIC_CALLABLES
        if not callable(getattr(mod, name, None))
    ]
    assert not not_callable, (
        "scrape shim re-exports lost callability: "
        + ", ".join(not_callable)
    )


def test_scrape_router_has_post_scrape_route() -> None:
    """The FastAPI router must still expose POST /scrape after the split."""
    mod = importlib.import_module(_SCRAPE_MODULE)
    router = mod.router
    paths = {
        getattr(r, "path", None) for r in getattr(router, "routes", [])
    }
    assert "/scrape" in paths, (
        "router lost /scrape POST route after split; "
        f"current paths={sorted(p for p in paths if p)}"
    )


def test_scrape_attempt_params_is_dataclass() -> None:
    """``ScrapeAttemptParams`` must remain a frozen dataclass."""
    mod = importlib.import_module(_SCRAPE_MODULE)
    import dataclasses
    assert dataclasses.is_dataclass(mod.ScrapeAttemptParams)
    fields = {f.name for f in dataclasses.fields(mod.ScrapeAttemptParams)}
    for required in (
        "url", "selector", "selectors", "extract", "wait_for",
        "timeout", "javascript", "stealth",
        "js_inject", "wait_for_text", "wait_for_gone",
        "screenshot_selector", "shadow_selector",
    ):
        assert required in fields, (
            f"ScrapeAttemptParams lost field '{required}' after split"
        )


def test_scrape_orchestrator_is_thin() -> None:
    """Post-split ``scrape.py`` must stay <=300 LOC (was 641)."""
    loc = sum(1 for _ in _SCRAPE_FILE.read_text(encoding="utf-8").splitlines())
    assert loc <= 300, (
        f"scrape.py is still {loc} LOC; split into _scrape_*.py siblings."
    )


def test_scrape_split_siblings_exist_and_are_lean() -> None:
    """The three planned sibling modules must exist and stay <=500 LOC each."""
    pkg = _SCRAPE_FILE.parent
    siblings = (
        "_scrape_pre_gates.py",
        "_scrape_attempt.py",
        "_smart_fetch_runner.py",
    )
    for name in siblings:
        path = pkg / name
        assert path.exists(), f"missing sibling: {path}"
        loc = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        assert loc <= 500, f"sibling {name} is {loc} LOC; trim further."
