"""Runtime hardening checks for the generic /scrape pipeline."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from crawlrx._scrape_attempt import (
    ScrapeAttemptParams,
    _do_scrape_dispatch,
)
from crawlrx._scrape_page_actions import (
    _await_extra_waits as _await_scrape_extra_waits,
    _maybe_shadow_content,
)
from crawlrx._crawl_attempt import (
    _await_extra_waits as _await_crawl_extra_waits,
)
from crawlrx import _smart_fetch_runner as smart_fetch_runner
from crawlrx import scrape_diagnostics
from crawlrx import scrape_site_health
from crawlrx._smart_fetch_runner import _smart_fetch_eligible
from crawlrx.scrape_policy import plan_scrape
from crawlrx._scrape_pre_gates import _resolve_pre_scrape_gates


def _params(**overrides: object) -> ScrapeAttemptParams:
    base = {
        "url": "https://example.com",
        "selector": None,
        "selectors": None,
        "extract": "text",
        "wait_for": None,
        "timeout": 30000,
        "javascript": True,
    }
    base.update(overrides)
    return ScrapeAttemptParams(**base)


class EmptySelectorPage:
    async def query_selector_all(self, selector: str) -> list[object]:
        return []


class MissingNamedSelectorPage:
    async def query_selector(self, selector: str) -> object | None:
        return None


def test_selector_scrape_fails_when_nothing_matches() -> None:
    result = {"success": False, "results": [], "data": {}, "error": None}
    params = _params(selector=".missing")

    asyncio.run(_do_scrape_dispatch(EmptySelectorPage(), params, "", result))

    assert result["success"] is False
    assert result["results"] == []
    assert result["error"] == "Selector matched no content: .missing"
    assert result["error_type"] == "selector_empty"


def test_named_selector_scrape_fails_when_all_selectors_are_empty() -> None:
    result = {"success": False, "results": [], "data": {}, "error": None}
    params = _params(selectors={"price": ".price", "title": "h1"})

    asyncio.run(_do_scrape_dispatch(MissingNamedSelectorPage(), params, "", result))

    assert result["success"] is False
    assert result["data"] == {"price": None, "title": None}
    assert result["error"] == "Named selectors matched no content"
    assert result["error_type"] == "selector_empty"


def test_smart_fetch_declines_requests_that_need_rendered_page_features() -> None:
    assert _smart_fetch_eligible(_params()) is True
    assert _smart_fetch_eligible(_params(javascript=False)) is True

    assert _smart_fetch_eligible(_params(javascript=False, wait_for_text="Ready")) is False
    assert _smart_fetch_eligible(_params(javascript=False, js_inject="() => 1")) is False
    assert _smart_fetch_eligible(_params(javascript=False, screenshot_selector="main")) is False
    assert _smart_fetch_eligible(_params(javascript=False, shadow_selector="my-widget")) is False


def test_scrape_policy_explains_http_first_and_browser_first() -> None:
    http_plan = plan_scrape(_params())
    browser_plan = plan_scrape(_params(js_inject="() => 1", screenshot_selector="h1"))

    assert http_plan.strategy == "http_first"
    assert "static extraction" in http_plan.reason
    assert browser_plan.strategy == "browser_first"
    assert browser_plan.required_features == ("js_inject", "screenshot_selector")


def _fake_http(html: str) -> dict:
    return {
        "success": True,
        "html": html,
        "status": 200,
        "tier": "requests",
        "final_url": "https://example.com/",
        "error": None,
    }


def _quarantine_for_test(url: str) -> None:
    for _ in range(3):
        scrape_site_health.record_scrape_site_outcome(
            url,
            {
                "success": False,
                "strategy": "browser_first",
                "source": "playwright",
                "error_type": "blocked",
                "error": "blocked",
            },
        )


async def _green_diagnostic_scrape_call(**kwargs: object) -> dict:
    if kwargs.get("selector") == ".crawlrx-definitely-missing":
        return {
            "success": False,
            "results": [],
            "error": "Selector matched no content",
            "source": "playwright",
            "rendered": True,
        }
    raw = {
        "success": True,
        "results": [{"text": "Example Domain"}],
        "error": None,
        "source": "requests",
        "rendered": False,
    }
    if kwargs.get("js_inject"):
        raw.update(
            {
                "source": "playwright",
                "rendered": True,
                "js_result": {"title": "Example Domain", "bodyLen": 120},
            }
        )
    if kwargs.get("shadow_selector"):
        raw["shadow_content"] = "Shadow OK"
    return raw


def test_smart_fetch_extracts_css_selector_before_browser(monkeypatch) -> None:
    monkeypatch.setattr(
        smart_fetch_runner,
        "smart_fetch",
        lambda url, timeout, headers: _fake_http("<main><h1>Example Domain</h1></main>"),
    )

    result = asyncio.run(
        smart_fetch_runner._scrape_via_smart_fetch(
            _params(selector="h1"), smart_fetch_runner.timer_ms(),
        )
    )

    assert result is not None
    assert result["success"] is True
    assert result["source"] == "requests"
    assert result["rendered"] is False
    assert result["strategy"] == "http_first"
    assert "plan_reason" in result["provenance"]
    assert result["provenance"]["http_attempted"] is True
    assert result["provenance"]["browser_attempted"] is False
    assert result["results"][0]["text"] == "Example Domain"


def test_smart_fetch_extracts_named_selectors_before_browser(monkeypatch) -> None:
    monkeypatch.setattr(
        smart_fetch_runner,
        "smart_fetch",
        lambda url, timeout, headers: _fake_http("<h1>Example Domain</h1><p>$10</p>"),
    )

    result = asyncio.run(
        smart_fetch_runner._scrape_via_smart_fetch(
            _params(selectors={"title": "h1", "price": "p"}),
            smart_fetch_runner.timer_ms(),
        )
    )

    assert result is not None
    assert result["success"] is True
    assert result["data"] == {"title": "Example Domain", "price": "$10"}
    assert result["results"] == []


def test_smart_fetch_declines_empty_selector_so_browser_can_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        smart_fetch_runner,
        "smart_fetch",
        lambda url, timeout, headers: _fake_http("<main><h1>Example Domain</h1></main>"),
    )

    result = asyncio.run(
        smart_fetch_runner._scrape_via_smart_fetch(
            _params(selector=".missing"), smart_fetch_runner.timer_ms(),
        )
    )

    assert result is None


class KeywordOnlyWaitPage:
    def __init__(self) -> None:
        self.wait_arg = None

    async def wait_for_function(self, expression: str, *, arg: str | None = None, timeout: int) -> None:
        self.wait_arg = arg


def _wait_params() -> SimpleNamespace:
    return SimpleNamespace(
        wait_for_text="Ready",
        wait_for_gone=None,
        wait_for_timeout_extra=0,
        timeout=30000,
        url="https://example.com",
    )


def test_scrape_wait_for_text_uses_current_playwright_keyword_arg() -> None:
    page = KeywordOnlyWaitPage()

    asyncio.run(_await_scrape_extra_waits(page, _wait_params()))

    assert page.wait_arg == "Ready"


def test_crawl_wait_for_text_uses_current_playwright_keyword_arg() -> None:
    page = KeywordOnlyWaitPage()

    asyncio.run(_await_crawl_extra_waits(page, _wait_params()))

    assert page.wait_arg == "Ready"


class ShadowEvalPage:
    def __init__(self) -> None:
        self.expression = ""

    async def eval_on_selector(self, selector: str, expression: str) -> str:
        self.expression = expression
        return "Shadow OK"


def test_shadow_content_uses_text_content_for_shadow_root() -> None:
    page = ShadowEvalPage()
    params = SimpleNamespace(shadow_selector="#host", url="https://example.com")
    result: dict = {}

    asyncio.run(_maybe_shadow_content(page, params, result))

    assert result["shadow_content"] == "Shadow OK"
    assert "textContent" in page.expression


def test_scrape_diagnostics_router_exposes_status_and_diagnostics() -> None:
    from crawlrx.scrape_diagnostics import router

    paths = {getattr(route, "path", "") for route in router.routes}

    assert "/scrape/diagnostics" in paths
    assert "/scrape/status" in paths


def test_scrape_site_health_records_success_and_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()

    scrape_site_health.record_scrape_site_outcome(
        "https://example.com/page",
        {"success": True, "strategy": "http_first", "source": "requests"},
    )
    scrape_site_health.record_scrape_site_outcome(
        "https://blocked.example/page",
        {
            "success": False,
            "strategy": "browser_first",
            "source": "playwright",
            "error_type": "blocked",
            "error": "blocked",
        },
    )

    health = scrape_site_health.get_scrape_site_health()
    summary = scrape_site_health.get_scrape_site_health_summary()

    assert health["example.com"]["successes"] == 1
    assert health["example.com"]["health_status"] == "healthy"
    assert health["blocked.example"]["failures"] == 1
    assert health["blocked.example"]["last_error_type"] == "blocked"
    assert summary["healthy"] >= 1


def test_selector_empty_does_not_quarantine_domain(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()

    for _ in range(5):
        scrape_site_health.record_scrape_site_outcome(
            "https://example.com/page",
            {
                "success": False,
                "strategy": "http_then_browser",
                "source": "playwright",
                "error_type": "selector_empty",
                "error": "Selector matched no content",
            },
        )

    state = scrape_site_health.get_scrape_site_health()["example.com"]
    assert state["health_status"] == "healthy"
    assert state["consecutive_failures"] == 0
    assert not scrape_site_health.is_scrape_site_quarantined("https://example.com/page")


def test_scrape_pre_gate_blocks_quarantined_domain(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()
    _quarantine_for_test("https://blocked.example/page")
    request = _params(url="https://blocked.example/page")

    response = asyncio.run(_resolve_pre_scrape_gates(request, cache=None, cache_key=None))

    assert response is not None
    assert response.success is False
    assert response.error_type == "quarantined"
    assert response.strategy == "blocked"


def test_scrape_quarantine_release_is_domain_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()
    _quarantine_for_test("https://example.com/page")
    _quarantine_for_test("https://other.example/page")

    scrape_site_health.release_scrape_site_quarantine(
        "https://example.com/page",
        source="test",
        reason="fresh_all_green",
    )

    health = scrape_site_health.get_scrape_site_health()
    example = health["example.com"]
    other = health["other.example"]
    assert example["health_status"] == "degraded"
    assert example["consecutive_failures"] == 0
    assert example["failures"] == 3
    assert example["last_error_type"] is None
    assert example["last_error"] is None
    assert example["release_source"] == "test"
    assert example["release_reason"] == "fresh_all_green"
    assert example["released_at"] > 0
    assert not scrape_site_health.is_scrape_site_quarantined("https://example.com/page")
    assert other["health_status"] == "quarantined"
    assert scrape_site_health.is_scrape_site_quarantined("https://other.example/page")


def test_fresh_green_scrape_diagnostics_release_diagnostic_domain(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()
    monkeypatch.setattr(scrape_diagnostics, "_DIAGNOSTIC_CACHE", None)
    monkeypatch.setattr(scrape_diagnostics, "_scrape_call", _green_diagnostic_scrape_call)
    _quarantine_for_test("https://example.com/page")
    _quarantine_for_test("https://other.example/page")

    response = asyncio.run(scrape_diagnostics.run_scrape_diagnostics(force=True))

    assert response.status == "ok"
    assert response.cache_hit is False
    assert not scrape_site_health.is_scrape_site_quarantined("https://example.com/page")
    assert scrape_site_health.is_scrape_site_quarantined("https://other.example/page")
    example = scrape_site_health.get_scrape_site_health()["example.com"]
    assert example["release_source"] == "scrape_diagnostics"
    assert example["release_reason"] == "fresh_all_green"


def test_cached_scrape_diagnostics_do_not_release_quarantine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()
    _quarantine_for_test("https://example.com/page")
    cached = scrape_diagnostics.ScrapeDiagnosticsResponse(
        status="ok",
        summary={"total": 1, "ok": 1, "warn": 0, "fail": 0},
        checks=[
            scrape_diagnostics.ScrapeDiagnosticCheck(
                name="cached",
                status="ok",
                detail="cached green",
                elapsed_ms=1,
            )
        ],
        elapsed_ms=1,
    )
    monkeypatch.setattr(scrape_diagnostics, "_DIAGNOSTIC_CACHE", (time.time(), cached))

    async def fail_if_called(**kwargs: object) -> dict:
        raise AssertionError("cached diagnostics should not scrape")

    monkeypatch.setattr(scrape_diagnostics, "_scrape_call", fail_if_called)

    response = asyncio.run(scrape_diagnostics.run_scrape_diagnostics(force=False))

    assert response.cache_hit is True
    assert scrape_site_health.is_scrape_site_quarantined("https://example.com/page")


def test_failed_scrape_diagnostics_do_not_release_quarantine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    scrape_site_health.reset_scrape_site_health_cache()
    monkeypatch.setattr(scrape_diagnostics, "_DIAGNOSTIC_CACHE", None)
    _quarantine_for_test("https://example.com/page")

    async def failed_scrape_call(**kwargs: object) -> dict:
        return {
            "success": False,
            "results": [],
            "error": "network timeout",
            "source": "requests",
            "rendered": False,
        }

    monkeypatch.setattr(scrape_diagnostics, "_scrape_call", failed_scrape_call)

    response = asyncio.run(scrape_diagnostics.run_scrape_diagnostics(force=True))

    assert response.status == "fail"
    assert scrape_site_health.is_scrape_site_quarantined("https://example.com/page")


def test_stale_scrape_quarantine_expires_to_degraded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(scrape_site_health, "_QUARANTINE_TTL_S", 10)
    monkeypatch.setattr(scrape_site_health, "_now", lambda: 1000.0)
    scrape_site_health.reset_scrape_site_health_cache()
    _quarantine_for_test("https://example.com/page")

    monkeypatch.setattr(scrape_site_health, "_now", lambda: 1005.0)
    assert scrape_site_health.is_scrape_site_quarantined("https://example.com/page")

    monkeypatch.setattr(scrape_site_health, "_now", lambda: 1011.0)
    assert not scrape_site_health.is_scrape_site_quarantined("https://example.com/page")
    state = scrape_site_health.get_scrape_site_health()["example.com"]
    assert state["health_status"] == "degraded"
    assert state["quarantine_expired_at"] == 1011.0


def test_legacy_iso_scrape_quarantine_timestamp_expires(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scrape_site_health, "_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(scrape_site_health, "_QUARANTINE_TTL_S", 10)
    monkeypatch.setattr(scrape_site_health, "_now", lambda: 1000.0)
    scrape_site_health.reset_scrape_site_health_cache()
    _quarantine_for_test("https://example.com/page")
    data = scrape_site_health.get_scrape_site_health()
    data["example.com"]["last_seen"] = "1970-01-01T00:00:01Z"
    data["example.com"].pop("quarantined_until_s", None)
    scrape_site_health._save_health(data)
    scrape_site_health.reset_scrape_site_health_cache()

    assert not scrape_site_health.is_scrape_site_quarantined("https://example.com/page")
