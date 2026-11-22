"""Capture ecommerce_search.py public API before splitting into siblings.

RED test: every name listed below must remain importable from
``crawlrx.ecommerce_search`` after the split. The shim
re-exports from ``_ecommerce_search_*.py`` siblings so callers
(``crawlrx.main`` -> ``ecommerce_search_router``,
``_search_google_shopping_playwright``, ``_google_shopping_to_product``)
keep working unchanged.

Source-of-truth for the public API list: snapshot file
the original public-API snapshot under the key
``crawlrx.ecommerce_search``.

Internal-but-re-exported entries are the helpers ``main.py`` reaches
across split-module boundaries. They MUST stay reachable through the
shim so monolith-era debugging / introspection still works.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterable


_ES_MODULE: str = "crawlrx.ecommerce_search"
_ES_FILE: Path = Path(
    Path(__file__).resolve().parents[1] / "crawlrx/ecommerce_search.py"
)


# ---------------------------------------------------------------------------
# Public-API contract -- pinned by snapshots/public_api.json
# ---------------------------------------------------------------------------

# Re-exported aliases / data (already counted as "public" by the harness).
_PUBLIC_DATA: tuple[str, ...] = (
    "BROWSER_SCRAPE_EXCEPTIONS",
    "PARSE_EXCEPTIONS",
    "SCRAPE_FAILURE_EXCEPTIONS",
)

# Public callables + pydantic models.
_PUBLIC_CALLABLES: tuple[str, ...] = (
    "EcommerceSearchRequest",
    "EcommerceSearchResponse",
    "ProductResult",
    "search_ecommerce",
)

# Internal helpers that other modules import (main.py uses ``router``,
# ``_search_google_shopping_playwright``, ``_google_shopping_to_product``).
_INTERNAL_BUT_REEXPORTED: tuple[str, ...] = (
    "_google_shopping_to_product",
    "_search_google_shopping_playwright",
    "router",
)


def _all_expected() -> Iterable[str]:
    yield from _PUBLIC_DATA
    yield from _PUBLIC_CALLABLES
    yield from _INTERNAL_BUT_REEXPORTED


def test_ecommerce_search_public_api_preserved() -> None:
    """Every name remains importable from the ecommerce_search shim post-split."""
    mod = importlib.import_module(_ES_MODULE)
    missing = [name for name in _all_expected() if not hasattr(mod, name)]
    assert not missing, (
        "ecommerce_search shim lost re-exports after split: "
        + ", ".join(missing)
    )


def test_ecommerce_search_public_callables_are_callable() -> None:
    """Every callable name resolves to a callable object (sanity check)."""
    mod = importlib.import_module(_ES_MODULE)
    not_callable = [
        name for name in _PUBLIC_CALLABLES
        if not callable(getattr(mod, name, None))
    ]
    assert not not_callable, (
        "ecommerce_search shim re-exports lost callability: "
        + ", ".join(not_callable)
    )


def test_ecommerce_search_router_has_post_ecommerce_route() -> None:
    """The FastAPI router must still expose POST /search/ecommerce."""
    mod = importlib.import_module(_ES_MODULE)
    router = mod.router
    paths = {
        getattr(r, "path", None) for r in getattr(router, "routes", [])
    }
    assert "/search/ecommerce" in paths, (
        "router lost /search/ecommerce POST route after split; "
        f"current paths={sorted(p for p in paths if p)}"
    )


def test_ecommerce_search_request_has_required_fields() -> None:
    """``EcommerceSearchRequest`` must keep its load-bearing fields."""
    mod = importlib.import_module(_ES_MODULE)
    fields = set(mod.EcommerceSearchRequest.model_fields.keys())
    for required in (
        "query", "num_results", "platforms", "max_price", "min_price",
    ):
        assert required in fields, (
            f"EcommerceSearchRequest lost field '{required}' after split"
        )


def test_product_result_has_required_fields() -> None:
    """``ProductResult`` must keep its load-bearing fields."""
    mod = importlib.import_module(_ES_MODULE)
    fields = set(mod.ProductResult.model_fields.keys())
    for required in (
        "title", "price", "price_original", "currency", "url",
        "store_url", "platform", "image", "in_stock", "discount_pct",
    ):
        assert required in fields, (
            f"ProductResult lost field '{required}' after split"
        )


def test_ecommerce_search_orchestrator_is_thin() -> None:
    """Post-split ``ecommerce_search.py`` must stay <=200 LOC (was 811)."""
    loc = sum(1 for _ in _ES_FILE.read_text(encoding="utf-8").splitlines())
    assert loc <= 200, (
        f"ecommerce_search.py is still {loc} LOC; "
        f"split into _ecommerce_search_*.py siblings."
    )


def test_ecommerce_search_split_siblings_exist_and_are_lean() -> None:
    """Sibling modules must exist and stay <=500 LOC each."""
    pkg = _ES_FILE.parent
    siblings = (
        "_ecommerce_search_filters.py",
        "_ecommerce_search_models.py",
        "_ecommerce_search_runner.py",
        "_ecommerce_search_sources.py",
    )
    for name in siblings:
        path = pkg / name
        assert path.exists(), f"missing sibling: {path}"
        loc = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        assert loc <= 500, f"sibling {name} is {loc} LOC; trim further."
