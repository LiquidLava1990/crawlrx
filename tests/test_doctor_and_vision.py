"""First-run doctor, vision endpoint and CLI wiring."""
from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

from crawlrx import cli, doctor, vision


def _hw(ram: float, vram: float = 0.0, unified: bool = False) -> doctor.Hardware:
    return doctor.Hardware(ram_gb=ram, gpu_name="gpu" if vram else "", gpu_vram_gb=vram, unified_memory=unified)


def test_recommendation_tiers_scale_with_memory() -> None:
    assert doctor.recommend_model(_hw(4)).tier == "tiny"
    assert doctor.recommend_model(_hw(8)).tier == "small"
    assert doctor.recommend_model(_hw(16)).tier == "medium"
    assert doctor.recommend_model(_hw(16, vram=24)).tier == "large"
    assert doctor.recommend_model(_hw(64, unified=True)).tier == "large"


def test_detect_hardware_reports_ram() -> None:
    assert doctor.detect_hardware().ram_gb > 0


def test_doctor_without_model_is_ready_but_warns(monkeypatch) -> None:
    report = doctor.run_doctor(check_browser_binary=False)
    model_check = next(c for c in report.checks if c.name == "model")
    assert not model_check.ok and not model_check.required
    assert report.ok  # the model is optional
    text = doctor.format_report(report)
    assert "[WARN] model" in text and "ollama pull" in text


def test_doctor_with_model_reports_endpoint(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", fake_model_server.base_url)
    report = doctor.run_doctor(check_browser_binary=False)
    assert report.model_ready
    assert fake_model_server.base_url in doctor.format_report(report)


def test_vision_endpoint_round_trip(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", fake_model_server.base_url)
    from crawlrx.main import app

    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"1" * 8).decode()
    with TestClient(app) as client:
        resp = client.post("/vision/analyze", json={"image_base64": image, "prompt": "describe"})
        status = client.get("/model/status").json()
    body = resp.json()
    assert body["success"] is True and body["text"]
    assert status["ready"] is True


def test_vision_without_model_explains_setup() -> None:
    result = asyncio.run(vision.analyze_image(b"\x89PNGxx", "hi"))
    assert result.success is False
    assert "ollama" in (result.error or "").lower()


def test_vision_rejects_bad_base64() -> None:
    from crawlrx.main import app

    with TestClient(app) as client:
        body = client.post("/vision/analyze", json={"image_base64": "data:image/png;base64,"}).json()
    assert body["success"] is False


def test_extract_without_model_fails_fast() -> None:
    from crawlrx.main import app

    with TestClient(app) as client:
        body = client.post("/extract/", json={"url": "https://example.com", "prompt": "title"}).json()
    assert body["success"] is False
    assert "CRAWLRX_LLM_BASE_URL" in body["error"]


def test_cli_doctor_json(capsys) -> None:
    code = cli.main(["doctor", "--json"])
    out = capsys.readouterr().out
    assert '"checks"' in out
    assert code in (0, 1)


def test_cli_parser_has_entry_points() -> None:
    parser = cli.build_parser()
    for command in ("serve", "doctor", "setup", "crawl", "version"):
        assert parser.parse_args([command] + (["https://example.com"] if command == "crawl" else [])).command == command


def test_learning_store_works_without_app_startup(tmp_path, monkeypatch) -> None:
    """CLI / library callers skip the FastAPI lifespan; the schema must self-create."""
    from crawlrx import learning

    import sqlite3

    db = tmp_path / "fresh.db"
    monkeypatch.setattr(learning, "_DB_PATH", str(db))
    learning.get_recommended_timeout("example.com")  # raised "no such table" before the fix
    tables = {row[0] for row in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "domain_params" in tables


def test_intelligence_tables_self_create_without_app_startup(tmp_path, monkeypatch) -> None:
    """`crawlrx crawl` on a fresh data dir crashed with "no such table: content_assessments"."""
    import sqlite3

    from crawlrx import intelligence, learning

    db = tmp_path / "fresh-intel.db"
    monkeypatch.setattr(learning, "_DB_PATH", str(db))
    html = "<html><body><p>hello world</p></body></html>"
    intelligence.assess_content_quality(html, "hello world", "hello world", "https://example.com/")
    tables = {row[0] for row in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "content_assessments" in tables


def test_serve_refuses_public_bind_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLRX_BIND", raising=False)
    monkeypatch.delenv("CRAWLRX_API_KEYS", raising=False)
    monkeypatch.delenv("CRAWLRX_ALLOW_NO_AUTH", raising=False)
    assert cli.exposure_error("127.0.0.1") is None
    assert "CRAWLRX_API_KEYS" in (cli.exposure_error("0.0.0.0") or "")
    monkeypatch.setenv("CRAWLRX_BIND", "127.0.0.1")  # docker: container 0.0.0.0, host loopback
    assert cli.exposure_error("0.0.0.0") is None
    monkeypatch.setenv("CRAWLRX_BIND", "0.0.0.0")
    monkeypatch.setenv("CRAWLRX_API_KEYS", "k1")
    assert cli.exposure_error("0.0.0.0") is None


def test_serve_exits_before_starting_when_exposed(monkeypatch, capsys) -> None:
    for name in ("CRAWLRX_BIND", "CRAWLRX_API_KEYS", "CRAWLRX_ALLOW_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "Refusing" in capsys.readouterr().err


def test_cors_closed_by_default() -> None:
    from crawlrx.main import app

    with TestClient(app) as client:
        resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_route_timeouts_cover_model_and_captcha_waits(monkeypatch) -> None:
    from crawlrx import captcha_handoff
    from crawlrx.main import TimeoutMiddleware

    monkeypatch.setenv("CRAWLRX_LLM_TIMEOUT", "300")
    assert TimeoutMiddleware.route_timeout("/extract/") >= 2 * 300
    assert TimeoutMiddleware.route_timeout("/vision/analyze") >= 2 * 300
    assert TimeoutMiddleware.route_timeout("/health") == 30.0
    captcha_handoff.set_handoff(captcha_handoff.VisibleBrowserHandoff(wait_s=600))
    assert TimeoutMiddleware.route_timeout("/crawl") >= 180 + 600
