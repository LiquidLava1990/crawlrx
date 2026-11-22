"""Operator dashboard proof payload tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from crawlrx import (
    dashboard_ui,
    main,
    scrape_diagnostics,
    scrape_site_health,
)


def _ok_row(name: str) -> dict[str, object]:
    return main._service_row(name, "ok", "green", latency_ms=1, url="/proof", layer="app")


async def _async_ok_row(name: str) -> dict[str, object]:
    return _ok_row(name)


def test_scrape_quality_dashboard_row_includes_proof_counts(monkeypatch) -> None:
    monkeypatch.setattr(main, "_cached_dashboard_probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_set_dashboard_probe_cache", lambda _key, row: row)
    monkeypatch.setattr(
        scrape_site_health,
        "get_scrape_site_health_summary",
        lambda: {"healthy": 2, "degraded": 1, "quarantined": 0, "unknown": 0},
    )

    async def fake_run_scrape_diagnostics(force: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            status="ok",
            cache_hit=False,
            summary={"total": 3, "ok": 3, "fail": 0},
            checks=[
                SimpleNamespace(rendered=False, source="requests"),
                SimpleNamespace(rendered=False, source="primp"),
                SimpleNamespace(rendered=True, source="playwright"),
            ],
        )

    monkeypatch.setattr(
        scrape_diagnostics, "run_scrape_diagnostics", fake_run_scrape_diagnostics,
    )

    row = asyncio.run(main._probe_scrape_diagnostics())

    assert row["name"] == "Scrape Quality Proof"
    assert row["status"] == "ok"
    assert row["result_count"] == 3
    assert row["summary"] == {"total": 3, "ok": 3, "fail": 0}
    assert row["site_health_counts"]["healthy"] == 2
    assert "2 HTTP, 1 browser" in row["detail"]
    assert "2 domain(s) healthy" in row["detail"]
    assert "1 degraded" in row["detail"]
    assert row["engine_used"] == "playwright, primp, requests"


def test_dashboard_status_keeps_scrape_and_model_proofs_visible(monkeypatch) -> None:
    stats = SimpleNamespace(
        success_rate=1.0,
        total_requests=4,
        errors_today=0,
        requests_by_endpoint={"/scrape": 2, "/search": 2},
        avg_response_ms={"/scrape": 12.5, "/search": 8.0},
    )
    monkeypatch.setattr(main, "get_stats", lambda: stats)
    monkeypatch.setattr(main, "get_cache", lambda: SimpleNamespace(stats=lambda: {}))
    monkeypatch.setattr(main, "_probe_searxng_service", lambda: _async_ok_row("SearXNG"))
    monkeypatch.setattr(main, "_probe_search_router", lambda: _async_ok_row("Search API"))
    monkeypatch.setattr(
        main, "_probe_search_orchestrator",
        lambda: _async_ok_row("Search Orchestrator"),
    )
    monkeypatch.setattr(
        main, "_probe_search_diagnostics",
        lambda: _async_ok_row("Search Quality Proof"),
    )
    monkeypatch.setattr(
        main, "_probe_scrape_diagnostics",
        lambda: _async_ok_row("Scrape Quality Proof"),
    )
    monkeypatch.setattr(main, "_probe_model_backend", lambda: _async_ok_row("AI Model"))
    monkeypatch.setattr(main, "_probe_searxng", lambda: _async_ok_row("SearXNG Upstream"))
    monkeypatch.setattr(main, "get_all_circuit_breakers", lambda: {})

    payload = asyncio.run(main.dashboard_status())
    service_names = {str(item["name"]) for item in payload["services"]}

    assert payload["summary"]["overall"] == "ok"
    assert "Scrape Quality Proof" in service_names
    assert "Search Quality Proof" in service_names
    assert "AI Model" in service_names
    assert "forever" not in payload


def test_dashboard_html_prioritizes_simple_operator_view() -> None:
    html = dashboard_ui.OPERATOR_DASHBOARD_HTML

    assert 'id="proofGrid"' in html
    assert 'id="overallBadge"' in html
    assert 'id="serviceList"' in html
    assert '<details class="drawer">' in html
    assert 'CrawlRX Command' not in html
    assert 'External Signals' in html
