"""Search-path safety hardening tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from crawlrx import _search_engines, _search_content
from crawlrx._search_engines import EngineSearchParams


def test_search_content_fetch_blocks_unsafe_urls_before_browser(monkeypatch) -> None:
    async def fail_get_browser() -> object:
        raise AssertionError("browser should not be opened for unsafe URL")

    monkeypatch.setattr(_search_content, "get_browser", fail_get_browser)

    result = asyncio.run(_search_content.fetch_url_content("http://127.0.0.1/admin"))

    assert result is None


def test_engine_attempt_converts_none_result_to_failure(monkeypatch) -> None:
    async def none_dispatch(engine: str, query: str, params: object) -> None:
        return None

    monkeypatch.setattr(_search_engines, "_dispatch_engine", none_dispatch)
    params = EngineSearchParams(num_results=3, timeout_ms=1000)

    result = asyncio.run(
        _search_engines._try_engine_attempt("google", "crawlrx", params, 0)
    )

    assert result == {
        "success": False,
        "results": [],
        "engine": "google",
        "error": "engine returned no result",
    }
