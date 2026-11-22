"""SSRF guard on every route, redirect checks in smart_fetch, and auth on status endpoints."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, List
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from crawlrx import auth, smart_fetch, url_guard
from crawlrx.main import app


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# -- URL guard -------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "http://127.0.0.1:8080/admin"},
        {"url": "localhost:6379"},
        {"url": "file:///etc/passwd"},
        {"urls": ["https://example.com", "http://169.254.169.254/latest/meta-data/"]},
        {"store_url": "http://10.0.0.5/"},
        {"domain": "localhost"},
        {"options": {"next": "http://192.168.1.1/"}},
    ],
)
def test_unsafe_urls_in_request_body_are_rejected(payload: dict) -> None:
    assert url_guard.find_unsafe_url(payload) is not None


def test_safe_and_non_url_values_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_guard, "is_safe_url", lambda url: urlparse(url).hostname == "example.com")
    body = {"url": "https://example.com/a", "query": "best laptops", "inurl": "login", "limit": 5}
    assert url_guard.find_unsafe_url(body) is None


def test_platform_routes_refuse_private_urls(client: TestClient) -> None:
    for path in ("/scrape/wordpress/posts", "/scrape/woocommerce/products", "/scrape/platform"):
        response = client.post(path, json={"url": "http://127.0.0.1:9/"})
        assert response.status_code == 400, path
        assert response.json()["error_type"] == "blocked"


def test_query_string_urls_are_checked(client: TestClient) -> None:
    response = client.get("/metadata", params={"url": "http://127.0.0.1:9/"})
    assert response.status_code == 400


# -- smart_fetch redirects ---------------------------------------------------------


class _RedirectServer:
    """/start redirects to http://localhost:<port>/secret; records every path requested."""

    def __init__(self) -> None:
        self.paths: List[str] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib API)
                recorder.paths.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/secret")
                    self.end_headers()
                    return
                body = b"<html><body>internal secret</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_smart_fetch_refuses_redirect_into_private_space(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _RedirectServer()
    try:
        # Treat 127.0.0.1 as "public" for the test; localhost stays internal.
        monkeypatch.setattr(smart_fetch, "is_safe_url", lambda url: urlparse(url).hostname == "127.0.0.1")
        result = smart_fetch.smart_fetch(f"http://127.0.0.1:{server.port}/start", timeout=5)
    finally:
        server.close()
    assert result["success"] is False
    assert "redirect" in (result["error"] or "").lower()
    assert "/secret" not in server.paths


def test_smart_fetch_follows_safe_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _RedirectServer()
    try:
        monkeypatch.setattr(smart_fetch, "is_safe_url", lambda url: True)
        result = smart_fetch.smart_fetch(f"http://127.0.0.1:{server.port}/start", timeout=5)
    finally:
        server.close()
    assert result["success"] is True
    assert "internal secret" in result["html"]
    assert result["final_url"].endswith("/secret")


def test_smart_fetch_refuses_private_start_url() -> None:
    result = smart_fetch.smart_fetch("http://127.0.0.1:9/", timeout=2)
    assert result["success"] is False


# -- auth ----------------------------------------------------------------------------


def test_status_endpoints_need_the_api_key_when_keys_are_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth, "_API_KEYS", {"k1"})
    for path in ("/dashboard/status", "/stats"):
        assert client.get(path).status_code == 401, path
    assert client.get("/stats", headers={"X-API-Key": "k1"}).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/dashboard").status_code == 200


# -- browser guard -------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.outcome: str = ""

    async def fallback(self) -> None:
        self.outcome = "allowed"

    async def abort(self, error_code: str = "failed") -> None:
        self.outcome = f"aborted:{error_code}"


@pytest.mark.parametrize(
    ("url", "outcome"),
    [
        ("http://127.0.0.1:8080/", "aborted:blockedbyclient"),
        ("http://169.254.169.254/latest/meta-data/", "aborted:blockedbyclient"),
        ("ws://localhost:9222/devtools", "aborted:blockedbyclient"),
        ("data:text/html,hi", "allowed"),
    ],
)
def test_browser_guard_route_blocks_internal_requests(url: str, outcome: str) -> None:
    import asyncio

    from crawlrx import browser_guard

    route = _FakeRoute(url)
    asyncio.run(browser_guard.guard_route(route))
    assert route.outcome == outcome


def test_browser_guard_is_installed_on_playwright() -> None:
    from playwright.async_api import Browser, BrowserType

    for method in (Browser.new_context, Browser.new_page, BrowserType.launch_persistent_context,
                   BrowserType.connect_over_cdp):
        assert getattr(method, "__crawlrx_guarded__", False), method


def test_real_browser_cannot_follow_redirect_into_private_space(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from playwright.async_api import Error as PlaywrightError, async_playwright

    from crawlrx import browser_guard

    server = _RedirectServer()
    monkeypatch.setattr(browser_guard, "is_safe_url", lambda url: urlparse(url).hostname == "127.0.0.1")
    browser_guard.clear_cache()

    async def visit() -> str:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except PlaywrightError as exc:
                pytest.skip(f"Chromium not installed: {exc}")
            page = await browser.new_page()
            try:
                await page.goto(f"http://127.0.0.1:{server.port}/start", timeout=10_000)
            except PlaywrightError as exc:
                return str(exc)
            finally:
                await browser.close()
            return "navigated"

    try:
        outcome = asyncio.run(visit())
    finally:
        server.close()
        browser_guard.clear_cache()
    # Playwright cannot stop the redirected GET itself, but the page must never be readable.
    assert outcome != "navigated"
    assert any(marker in outcome.lower() for marker in ("closed", "err_aborted", "detached"))


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", '{"url": "http://127.0.0.1:9/"}'),
        ("APPLICATION/JSON", '{"url": "http://127.0.0.1:9/"}'),
        ("", '{"url": "http://127.0.0.1:9/"}'),
        ("application/x-www-form-urlencoded", "url=http%3A%2F%2F127.0.0.1%3A9%2F"),
    ],
)
def test_guard_scans_body_whatever_the_content_type(client: TestClient, content_type: str, body: str) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = client.post("/scrape/platform", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error_type"] == "blocked"


def test_primp_tier_survives_unknown_impersonation_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newer primp raises BuilderError for names like chrome_123 instead of falling back."""
    import sys
    import types

    class BuilderError(Exception):
        pass

    built: List[str] = []

    class Client:
        def __init__(self, impersonate: str = "", **kwargs: object) -> None:
            if impersonate.startswith("chrome_"):
                raise BuilderError(f'Invalid impersonate: "{impersonate}"')
            built.append(impersonate)

        def get(self, url: str, headers: object = None) -> object:
            return types.SimpleNamespace(status_code=200, text="<html>ok page</html>", headers={})

    fake = types.ModuleType("primp")
    fake.Client = Client
    fake.BuilderError = BuilderError
    monkeypatch.setitem(sys.modules, "primp", fake)
    get_once = smart_fetch._open_primp({}, 5, {"version": "123"})
    assert get_once("https://example.com/").status_code == 200
    assert built and not built[0].startswith("chrome_")
