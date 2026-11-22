import asyncio

from crawlrx import search_orchestrator as orchestrator
from crawlrx.search_orchestrator import (
    OrchestratedResult,
    RouteOutcome,
    SearchDiagnosticCheck,
    SearchOrchestratorRequest,
    SearchOrchestratorResponse,
    _ORCHESTRATOR_CACHE,
    _ROUTE_HEALTH,
    _cache_get,
    _cache_key,
    _cache_set,
    _cached_response,
    _classify_request,
    _dedupe_and_rank,
    _normalize_url_key,
    _preflight_warnings,
    _record_route_health,
    _diagnostic_status,
    _diagnostic_summary,
    _route_backoff_outcome,
    _route_plan,
    _searxng_golden_checks,
    _source_health_snapshot,
    _update_source_health,
    run_search_diagnostics,
    search_source_health,
    search_source_registry,
    orchestrator_runtime_status,
)


def test_classify_request_selects_specialized_intents() -> None:
    cases = [
        (SearchOrchestratorRequest(query="pizza 10001"), "local"),
        (SearchOrchestratorRequest(query="site:openai.com research"), "deep"),
        (SearchOrchestratorRequest(query="latest ai news"), "news"),
        (SearchOrchestratorRequest(query="wireless headphones deals"), "ecommerce"),
        (SearchOrchestratorRequest(query="crawlrx health"), "fusion"),
    ]

    for request, expected in cases:
        intent, confidence = _classify_request(request)
        assert intent == expected
        assert 0 < confidence <= 1


def test_route_plan_keeps_fallbacks_bounded() -> None:
    request = SearchOrchestratorRequest(query="site:openai.com research")
    intent, _confidence = _classify_request(request)
    assert _route_plan(request, intent) == ["deep", "fusion", "web"]

    explicit = SearchOrchestratorRequest(query="logo", mode="images")
    assert _route_plan(explicit, "images") == ["images", "fusion", "web"]

    no_zip = SearchOrchestratorRequest(query="coffee near me")
    assert _route_plan(no_zip, "local") == ["fusion", "web"]
    assert _preflight_warnings(no_zip, "local") == [
        "local map search needs zip_code; using web/fusion fallback"
    ]

    explicit_no_zip = SearchOrchestratorRequest(query="coffee near me", mode="local")
    assert _route_plan(explicit_no_zip, "local") == ["fusion", "web"]


def test_url_normalization_removes_tracking_params() -> None:
    raw = "HTTPS://Example.com/a/?utm_source=x&b=2&gclid=abc#frag"
    assert _normalize_url_key(raw) == "https://example.com/a?b=2"


def test_dedupe_and_rank_keeps_best_duplicate() -> None:
    records = [
        {
            "title": "Old",
            "url": "https://example.com/a?utm_source=x",
            "source": "web",
            "kind": "web",
            "score": 0.5,
            "metadata": {"route": "web"},
        },
        {
            "title": "New",
            "url": "https://example.com/a",
            "source": "fusion",
            "kind": "web",
            "score": 0.9,
            "metadata": {"route": "fusion"},
        },
    ]

    results = _dedupe_and_rank(records, 10)

    assert len(results) == 1
    assert results[0].title == "New"
    assert results[0].metadata["routes"] == ["fusion", "web"]


def test_cache_round_trip_marks_hit_and_stale() -> None:
    _ORCHESTRATOR_CACHE.clear()
    request = SearchOrchestratorRequest(query="cache proof")
    key = _cache_key(request)
    response = SearchOrchestratorResponse(
        success=True,
        query=request.query,
        mode="auto",
        intent="fusion",
        confidence=0.8,
        results=[
            OrchestratedResult(
                title="Proof",
                url="https://example.com",
                source="test",
                score=0.9,
            )
        ],
        total=1,
        routes=[RouteOutcome(route="web", status="ok", result_count=1)],
    )

    _cache_set(key, response)

    cached = _cache_get(key, 60)
    assert cached is not None
    fresh = _cached_response(cached, stale=False)
    stale = _cached_response(cached, stale=True)

    assert fresh.cache_hit is True
    assert fresh.stale is False
    assert stale.cache_hit is True
    assert stale.stale is True
    assert any("stale" in warning for warning in stale.warnings)


def test_cache_persists_to_disk(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "orchestrator-cache.json"
    monkeypatch.setattr(orchestrator, "_ORCHESTRATOR_CACHE_PATH", cache_path)
    monkeypatch.setattr(orchestrator, "_ORCHESTRATOR_CACHE_LOADED", False)
    _ORCHESTRATOR_CACHE.clear()
    request = SearchOrchestratorRequest(query="durable proof")
    key = _cache_key(request)
    response = SearchOrchestratorResponse(
        success=True,
        query=request.query,
        mode="auto",
        intent="fusion",
        confidence=0.8,
        results=[
            OrchestratedResult(
                title="Durable",
                url="https://example.com/durable",
            )
        ],
        total=1,
        routes=[RouteOutcome(route="web", status="ok", result_count=1)],
    )

    _cache_set(key, response)
    _ORCHESTRATOR_CACHE.clear()
    monkeypatch.setattr(orchestrator, "_ORCHESTRATOR_CACHE_LOADED", False)

    cached = _cache_get(key, 60)

    assert cached is not None
    assert cached["results"][0]["title"] == "Durable"


def test_route_backoff_skips_after_repeated_failures() -> None:
    _ROUTE_HEALTH.clear()

    for _ in range(orchestrator._ROUTE_FAILURE_LIMIT):
        _record_route_health(RouteOutcome(route="fusion", status="fail"))

    skipped = _route_backoff_outcome("fusion")
    assert skipped is not None
    assert skipped.status == "skipped"

    _record_route_health(RouteOutcome(route="fusion", status="ok"))
    assert _route_backoff_outcome("fusion") is None


def test_runtime_status_reports_cache_and_route_health(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_ORCHESTRATOR_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(orchestrator, "_ORCHESTRATOR_CACHE_LOADED", False)
    _ORCHESTRATOR_CACHE.clear()
    _ROUTE_HEALTH.clear()
    _record_route_health(RouteOutcome(route="deep", status="fail"))

    status = orchestrator_runtime_status()

    assert status.cache_entries == 0
    assert status.cache_path.endswith("cache.json")
    assert status.route_health["deep"]["failures"] == 1


def test_diagnostic_summary_sets_overall_status() -> None:
    checks = [
        SearchDiagnosticCheck(
            name="ok",
            source="searxng",
            query="openai",
            engines="bing",
            status="ok",
            result_count=10,
        ),
        SearchDiagnosticCheck(
            name="warn",
            source="searxng",
            query="openai",
            engines="bing",
            status="warn",
            result_count=10,
            warning_count=1,
        ),
    ]

    summary = _diagnostic_summary(checks)

    assert summary == {"total": 2, "ok": 1, "warn": 1, "fail": 0}
    assert _diagnostic_status(summary) == "warn"


def test_run_search_diagnostics_uses_cached_green_proof(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_CACHE", None)
    monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_LOG_PATH", tmp_path / "diagnostics.jsonl")
    monkeypatch.setattr(orchestrator, "_SOURCE_HEALTH_PATH", tmp_path / "source-health.json")
    monkeypatch.setattr(
        orchestrator,
        "_searxng_golden_checks",
        lambda: [
            {
                "name": "proof",
                "source": "searxng",
                "query": "openai",
                "engines": "bing,wikipedia",
                "min_results": 1,
            }
        ],
    )

    calls = {"count": 0}

    def fake_check(spec: dict) -> SearchDiagnosticCheck:
        calls["count"] += 1
        return SearchDiagnosticCheck(
            name=spec["name"],
            source=spec["source"],
            query=spec["query"],
            engines=spec["engines"],
            min_results=spec["min_results"],
            status="ok",
            result_count=3,
            detail="quality threshold met",
        )

    monkeypatch.setattr(orchestrator, "_searxng_check_sync", fake_check)

    first = asyncio.run(run_search_diagnostics(force=True))
    second = asyncio.run(run_search_diagnostics(force=False))

    assert first.status == "ok"
    assert first.cache_hit is False
    assert second.status == "ok"
    assert second.cache_hit is True
    assert calls["count"] == 1


def test_golden_checks_are_loaded_from_source_registry(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator._cfg,
        "source_diagnostic_checks",
        lambda: [
            {
                "name": "custom-web",
                "source": "searxng",
                "query": "custom",
                "group": "web",
                "min_results": 2,
            }
        ],
    )

    checks = _searxng_golden_checks()

    assert checks == [
        {
            "name": "custom-web",
            "source": "searxng",
            "query": "custom",
            "group": "web",
            "min_results": 2,
            "engines": "bing,wikipedia",
            "health_key": "searxng:web",
        }
    ]


def test_source_registry_endpoint_exposes_active_groups() -> None:
    payload = search_source_registry()

    assert payload["active_groups"]["web"] == "bing,wikipedia"
    assert "registry" in payload
    assert payload["path"].endswith("source_registry.json")


def test_source_health_scores_and_quarantines_repeated_failures(tmp_path, monkeypatch) -> None:
    health_path = tmp_path / "source-health.json"
    monkeypatch.setattr(orchestrator, "_SOURCE_HEALTH_PATH", health_path)

    failing = SearchDiagnosticCheck(
        name="proof",
        source="searxng",
        group="web",
        health_key="searxng:web",
        query="openai",
        engines="bing,wikipedia",
        status="fail",
        result_count=0,
        detail="no results",
    )

    for idx in range(orchestrator._SOURCE_QUARANTINE_FAILURE_LIMIT):
        health = _update_source_health([failing], now=1000.0 + idx)

    state = health["searxng:web"]

    assert health_path.exists()
    assert state["consecutive_failures"] == orchestrator._SOURCE_QUARANTINE_FAILURE_LIMIT
    assert state["health_status"] == "quarantined"
    assert state["quarantine_remaining_s"] > 0


def test_source_health_recovers_on_green_check(tmp_path, monkeypatch) -> None:
    health_path = tmp_path / "source-health.json"
    monkeypatch.setattr(orchestrator, "_SOURCE_HEALTH_PATH", health_path)
    fail = SearchDiagnosticCheck(
        name="proof",
        source="searxng",
        group="web",
        health_key="searxng:web",
        query="openai",
        engines="bing,wikipedia",
        status="fail",
        result_count=0,
    )
    ok = SearchDiagnosticCheck(
        name="proof",
        source="searxng",
        group="web",
        health_key="searxng:web",
        query="openai",
        engines="bing,wikipedia",
        status="ok",
        result_count=10,
    )

    _update_source_health([fail], now=1000.0)
    health = _update_source_health([ok], now=1001.0)

    state = health["searxng:web"]
    assert state["last_status"] == "ok"
    assert state["consecutive_failures"] == 0
    assert state["consecutive_successes"] == 1
    assert state["quarantined_until_s"] == 0.0


def test_source_health_endpoint_exposes_ledger(tmp_path, monkeypatch) -> None:
    health_path = tmp_path / "source-health.json"
    monkeypatch.setattr(orchestrator, "_SOURCE_HEALTH_PATH", health_path)
    _update_source_health([
        SearchDiagnosticCheck(
            name="proof",
            source="searxng",
            group="web",
            health_key="searxng:web",
            query="openai",
            engines="bing,wikipedia",
            status="ok",
            result_count=10,
        )
    ], now=1000.0)

    payload = search_source_health()

    assert payload["path"].endswith("source-health.json")
    assert payload["sources"]["searxng:web"]["last_status"] == "ok"
