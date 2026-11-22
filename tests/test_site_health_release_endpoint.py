"""Operator quarantine release: POST /scrape/site-health/release.

A host that goes quarantined (repeated captcha_required) must be releasable
once a human clears the challenge out of band — previously the only release
path was automatic diagnostics against a fixed URL, so a manually-cleared site
stayed blocked for the full TTL (caught live in protocol step 3).
"""
from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from crawlrx.scrape_diagnostics import router  # noqa: E402
from crawlrx.scrape_site_health import (  # noqa: E402
    is_scrape_site_quarantined,
    record_scrape_site_outcome,
    release_scrape_site_quarantine,
)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _quarantine_fixture_url() -> str:
    return "http://quarantine-fixture.test/blocked"


def test_quarantined_host_reports_as_quarantined() -> None:
    url = _quarantine_fixture_url()
    record_scrape_site_outcome(url, {"success": False, "error_type": "captcha_required"})
    record_scrape_site_outcome(url, {"success": False, "error_type": "captcha_required"})
    record_scrape_site_outcome(url, {"success": False, "error_type": "captcha_required"})
    assert is_scrape_site_quarantined(url)


def test_release_endpoint_clears_quarantine(client: TestClient) -> None:
    url = _quarantine_fixture_url()
    for _ in range(4):
        record_scrape_site_outcome(url, {"success": False, "error_type": "captcha_required"})
    assert is_scrape_site_quarantined(url)

    resp = client.post("/scrape/site-health/release", json={"url": url})
    assert resp.status_code == 200
    body = resp.json()
    assert body["released"] is True
    assert body["domain"] == "quarantine-fixture.test"
    assert not is_scrape_site_quarantined(url), "quarantine must be lifted after release"


def test_release_endpoint_is_lenient_with_bare_hostnames(client: TestClient) -> None:
    """_domain() treats bare strings as hostnames; release is a harmless no-op."""
    resp = client.post("/scrape/site-health/release", json={"url": "not a url"})
    assert resp.status_code == 200
    assert resp.json()["domain"] == "not a url"
