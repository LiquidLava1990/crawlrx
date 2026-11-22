"""Verify request validation and solver routing without contacting providers."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from crawlrx.solver_service import main
from crawlrx.solver_service.models import CaptchaMethod, Task
from crawlrx.solver_service.solver_router import SolverRouter


@pytest.mark.parametrize("method,expected", [
    ("funcaptcha", CaptchaMethod.FUNCAPTCHA),
    ("datadome", CaptchaMethod.DATADOME),
    ("geetest", CaptchaMethod.GEETEST),
])
def test_request_keeps_distinct_type_and_routes(monkeypatch, method, expected):
    submitted = []
    monkeypatch.setattr(main, "task_queue", SimpleNamespace(submit=lambda kind, params: (
        submitted.append((kind, params)) or SimpleNamespace(id="test-task")
    )))
    payload = {"method": method, "pageurl": "https://example.test/", "json": "1"}
    if method == "geetest":
        payload["gt"] = "test-gt"
    response = TestClient(main.app).post("/in.php", data=payload)
    assert response.json()["status"] == 1
    kind, params = submitted[0]
    assert kind == expected
    task = Task(id="test", method=kind, params=params)
    assert asyncio.run(SolverRouter()._resolve_solver_type(task)) == method


@pytest.mark.parametrize("method,extra,missing", [
    ("funcaptcha", {}, "pageurl"),
    ("datadome", {}, "pageurl"),
    ("geetest", {"pageurl": "https://example.test/"}, "gt"),
    ("hcaptcha", {"pageurl": "https://example.test/"}, "sitekey"),
])
def test_required_parameters_still_rejected(method, extra, missing):
    response = TestClient(main.app).post("/in.php", data={"method": method, "json": "1", **extra})
    assert response.json()["status"] == 0
    assert response.json()["request"] == "ERROR_BAD_PARAMETERS"
    payload = main.SubmitFormPayload(method=method, **extra)
    actual = main.missing_param_for_recaptcha_family(
        main.method_to_enum(method), method, payload.to_solver_params()
    )
    assert missing in actual
