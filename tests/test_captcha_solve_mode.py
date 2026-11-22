"""CRAWLRX_CAPTCHA_MODE: handoff (default) vs solve, and fallback from solvers to the hand-off."""
from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from crawlrx import captcha_handoff
from crawlrx.captcha_handoff import CaptchaChallenge, NoHandoff, VisibleBrowserHandoff


class _FakeSolver:
    def __init__(self, name: str, kinds: frozenset, result: Any, calls: List[str]) -> None:
        self.name = name
        self.kinds = kinds
        self._result = result
        self._calls = calls
        self.timeout_s = 30.0

    async def solve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        self._calls.append(self.name)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _RecordingFallback(NoHandoff):
    name = "recording"

    def __init__(self) -> None:
        self.called = False

    async def resolve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        self.called = True
        return False


@pytest.fixture(autouse=True)
def _reset_handoff(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in ("CRAWLRX_CAPTCHA_MODE", "CRAWLRX_CAPTCHA_HANDOFF", "CRAWLRX_SOLVERS", "CRAWLRX_FLARESOLVERR_URL"):
        monkeypatch.delenv(name, raising=False)
    captcha_handoff.set_handoff(None)
    yield
    captcha_handoff.set_handoff(None)


def _cloudflare_html() -> str:
    return "<html><title>Just a moment...</title><div id='challenge-platform'></div></html>"


def test_default_mode_is_solve_with_the_handoff_as_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from crawlrx import solvers

    fake = _FakeSolver("cf", frozenset({"cloudflare"}), True, [])
    monkeypatch.setattr(solvers, "load_solvers", lambda: [fake])
    assert captcha_handoff.captcha_mode() == "solve"
    handoff = captcha_handoff.get_handoff()
    assert isinstance(handoff, solvers.SolveThenHandoff)
    assert isinstance(handoff.fallback, NoHandoff)


@pytest.mark.parametrize("value", ["handoff", "off", "none", "HANDOFF"])
def test_handoff_mode_switches_solving_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from crawlrx import solvers

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("solvers must not load in handoff mode")

    monkeypatch.setattr(solvers, "load_solvers", explode)
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", value)
    assert captcha_handoff.captcha_mode() == "handoff"
    assert isinstance(captcha_handoff.get_handoff(), NoHandoff)


@pytest.mark.parametrize("value", ["solve", "on", " Solve "])
def test_solve_mode_aliases(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", value)
    assert captcha_handoff.captcha_mode() == "solve"


def test_unknown_mode_falls_back_to_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", "autosolve")
    assert captcha_handoff.captcha_mode() == "handoff"


def test_solve_mode_without_any_solver_uses_the_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from crawlrx import solvers

    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", "solve")
    monkeypatch.setattr(solvers, "load_solvers", lambda: [])
    assert isinstance(captcha_handoff.get_handoff(), NoHandoff)


def test_solver_success_clears_without_fallback() -> None:
    from crawlrx.solvers import SolveThenHandoff

    calls: List[str] = []
    fallback = _RecordingFallback()
    chain = SolveThenHandoff(
        [_FakeSolver("cf", frozenset({"cloudflare"}), True, calls)], fallback,
    )
    captcha_handoff.set_handoff(chain)
    cleared, info = asyncio.run(captcha_handoff.hand_off(object(), "https://example.com", _cloudflare_html()))
    assert cleared is True
    assert calls == ["cf"]
    assert fallback.called is False
    assert info["solved_by"] == "cf"


def test_failed_and_crashing_solvers_fall_back_to_handoff() -> None:
    from crawlrx.solvers import SolveThenHandoff

    calls: List[str] = []
    fallback = _RecordingFallback()
    chain = SolveThenHandoff(
        [
            _FakeSolver("broken", frozenset({"cloudflare"}), RuntimeError("boom"), calls),
            _FakeSolver("weak", frozenset({"*"}), False, calls),
        ],
        fallback,
    )
    captcha_handoff.set_handoff(chain)
    cleared, info = asyncio.run(captcha_handoff.hand_off(object(), "https://example.com", _cloudflare_html()))
    assert cleared is False
    assert calls == ["broken", "weak"]
    assert fallback.called is True
    assert info["handoff"] == "solve+recording"
    assert info["solvers_tried"] == ["broken", "weak"]


def test_solvers_only_run_for_their_challenge_kinds() -> None:
    from crawlrx.solvers import SolveThenHandoff

    calls: List[str] = []
    chain = SolveThenHandoff(
        [
            _FakeSolver("hc", frozenset({"hcaptcha"}), True, calls),
            _FakeSolver("cf", frozenset({"cloudflare"}), False, calls),
        ],
        _RecordingFallback(),
    )
    captcha_handoff.set_handoff(chain)
    asyncio.run(captcha_handoff.hand_off(object(), "https://example.com", _cloudflare_html()))
    assert calls == ["cf"]


def test_solve_mode_budget_covers_solvers_and_visible_fallback() -> None:
    from crawlrx.solvers import SolveThenHandoff

    chain = SolveThenHandoff(
        [_FakeSolver("cf", frozenset({"cloudflare"}), False, [])], VisibleBrowserHandoff(wait_s=100),
    )
    captcha_handoff.set_handoff(chain)
    assert chain.needs_visible_browser is True
    assert captcha_handoff.handoff_wait_seconds() >= 130


def test_solver_selection_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from crawlrx import solvers

    monkeypatch.setenv("CRAWLRX_FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
    monkeypatch.setenv("CRAWLRX_SOLVERS", "flaresolverr")
    names = [solver.name for solver in solvers.load_solvers()]
    assert names == ["flaresolverr"]
    monkeypatch.setenv("CRAWLRX_SOLVERS", "nonexistent")
    assert solvers.load_solvers() == []


def test_flaresolverr_needs_a_url() -> None:
    from crawlrx import solvers

    assert "flaresolverr" not in [solver.name for solver in solvers.load_solvers()]


# -- FlareSolverr client -------------------------------------------------------------


class _FakeContext:
    def __init__(self) -> None:
        self.cookies: List[dict] = []

    async def add_cookies(self, cookies: List[dict]) -> None:
        self.cookies.extend(cookies)


class _FakePage:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.headers: dict = {}
        self.reloaded = False

    async def set_extra_http_headers(self, headers: dict) -> None:
        self.headers.update(headers)

    async def reload(self, wait_until: str = "load") -> None:
        self.reloaded = True

    async def content(self) -> str:
        cleared = any(c["name"] == "cf_clearance" for c in self.context.cookies)
        return "<html><body>real page</body></html>" if cleared else _cloudflare_html()

    async def title(self) -> str:
        return "Shop" if self.context.cookies else "Just a moment..."


def _fake_flaresolverr(response: dict) -> Any:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests: List[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (stdlib API)
            length = int(self.headers.get("Content-Length", 0))
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def test_flaresolverr_solver_applies_clearance_and_reloads() -> None:
    from crawlrx.solvers import FlareSolverrSolver

    server, requests = _fake_flaresolverr({
        "status": "ok",
        "solution": {
            "userAgent": "Mozilla/5.0 Test",
            "cookies": [{"name": "cf_clearance", "value": "abc", "domain": ".example.com", "path": "/",
                         "expires": 1999999999, "httpOnly": True, "secure": True, "sameSite": "None"}],
        },
    })
    try:
        solver = FlareSolverrSolver(f"127.0.0.1:{server.server_port}", timeout_s=5)
        page = _FakePage()
        solved = asyncio.run(solver.solve(page, CaptchaChallenge(url="https://example.com/", kind="cloudflare")))
    finally:
        server.shutdown()
        server.server_close()
    assert solved is True
    assert requests == [{"cmd": "request.get", "url": "https://example.com/", "maxTimeout": 5000}]
    assert page.reloaded and page.headers["User-Agent"] == "Mozilla/5.0 Test"
    assert page.context.cookies[0]["sameSite"] == "None"


def test_flaresolverr_error_falls_back_to_handoff() -> None:
    from crawlrx.solvers import FlareSolverrSolver, SolveThenHandoff

    server, _requests = _fake_flaresolverr({"status": "error", "message": "Challenge not solved"})
    fallback = _RecordingFallback()
    try:
        chain = SolveThenHandoff([FlareSolverrSolver(f"http://127.0.0.1:{server.server_port}", 5)], fallback)
        captcha_handoff.set_handoff(chain)
        cleared, info = asyncio.run(captcha_handoff.hand_off(_FakePage(), "https://example.com/", _cloudflare_html()))
    finally:
        server.shutdown()
        server.server_close()
    assert cleared is False and fallback.called
    assert info["solvers_tried"] == ["flaresolverr"]


def test_unreachable_flaresolverr_falls_back_to_handoff() -> None:
    from crawlrx.solvers import FlareSolverrSolver, SolveThenHandoff

    fallback = _RecordingFallback()
    chain = SolveThenHandoff([FlareSolverrSolver("http://127.0.0.1:9", 3)], fallback)
    captcha_handoff.set_handoff(chain)
    cleared, _info = asyncio.run(captcha_handoff.hand_off(_FakePage(), "https://example.com/", _cloudflare_html()))
    assert cleared is False and fallback.called


# -- token solvers -------------------------------------------------------------------


class _TokenPage(_FakePage):
    """A page with a reCAPTCHA widget; clears once a token is injected."""

    def __init__(self) -> None:
        super().__init__()
        self.injected: List[Any] = []

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "getAttribute('data-sitekey')" in script:
            return "site-key-123"
        self.injected.append(arg)
        return "callback"

    async def content(self) -> str:
        if self.injected:
            return "<html><body>logged in</body></html>"
        return "<html><div class='g-recaptcha' data-sitekey='site-key-123'></div></html>"

    async def title(self) -> str:
        return "Account" if self.injected else "Please verify"


def _fake_2captcha() -> Any:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    seen: List[dict] = []
    polls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            seen.append({k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()})
            self._reply({"status": 1, "request": "task-1"})

        def do_GET(self) -> None:  # noqa: N802
            seen.append({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            polls["count"] += 1
            if polls["count"] < 2:
                self._reply({"status": 0, "request": "CAPCHA_NOT_READY"})
            else:
                self._reply({"status": 1, "request": "TOKEN-XYZ"})

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_solver_service_submits_sitekey_polls_and_injects_token() -> None:
    from crawlrx.solvers import SolverServiceSolver

    server, seen = _fake_2captcha()
    try:
        solver = SolverServiceSolver(f"127.0.0.1:{server.server_port}", api_key="", timeout_s=10, poll_s=0.01)
        page = _TokenPage()
        solved = asyncio.run(solver.solve(page, CaptchaChallenge(url="https://example.com/login", kind="recaptcha")))
    finally:
        server.shutdown()
        server.server_close()
    assert solved is True
    assert seen[0] == {"json": "1", "method": "userrecaptcha", "pageurl": "https://example.com/login",
                       "googlekey": "site-key-123"}
    assert "key" not in seen[0]  # no key is ever sent unless the user configured one
    assert page.injected[0][2] == "TOKEN-XYZ"


def test_recaptcha_audio_solver_uses_audio_only_and_injects_token() -> None:
    from crawlrx.solvers import RecaptchaAudioSolver

    calls: dict = {}

    class FakeAsyncSolver:
        def __init__(self, page: Any, attempts: int = 5, **kwargs: Any) -> None:
            calls["kwargs"] = kwargs

        async def __aenter__(self) -> "FakeAsyncSolver":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def solve_recaptcha(self, **kwargs: Any) -> str:
            calls["solve"] = kwargs
            return "AUDIO-TOKEN"

    fake_module = type("recaptchav2", (), {"AsyncSolver": FakeAsyncSolver})
    page = _TokenPage()
    solved = asyncio.run(RecaptchaAudioSolver(fake_module, timeout_s=5).solve(
        page, CaptchaChallenge(url="https://example.com/", kind="recaptcha")))
    assert solved is True
    assert calls["solve"]["image_challenge"] is False
    assert "capsolver_api_key" not in calls["kwargs"]
    assert page.injected[0][2] == "AUDIO-TOKEN"


def test_recaptcha_audio_needs_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from crawlrx import solvers

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("CRAWLRX_SOLVERS", "recaptcha-audio")
    assert solvers.load_solvers() == []
    row = next(r for r in solvers.solver_status() if r["name"] == "recaptcha-audio")
    assert "ffmpeg" in row["state"]
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert [s.name for s in solvers.load_solvers()] == ["recaptcha-audio"]


def test_recaptcha_audio_is_skipped_when_the_library_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from crawlrx import solvers

    monkeypatch.setitem(sys.modules, "playwright_recaptcha", None)  # simulate "not installed"
    monkeypatch.setenv("CRAWLRX_SOLVERS", "recaptcha-audio")
    assert solvers.load_solvers() == []
    assert any(row["name"] == "recaptcha-audio" and "missing" in row["state"] for row in solvers.solver_status())


def test_doctor_reports_captcha_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from crawlrx import doctor

    monkeypatch.setattr(shutil, "which", lambda name: None)  # no ffmpeg -> recaptcha-audio unusable
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", "handoff")
    check = doctor._captcha_check()
    assert check.ok and "handoff" in check.detail and not check.required
    monkeypatch.delenv("CRAWLRX_CAPTCHA_MODE")  # default: solve
    check = doctor._captcha_check()
    assert not check.ok and "no solver is usable" in check.detail
    assert "ffmpeg" in check.detail
    assert "CRAWLRX_FLARESOLVERR_URL" in check.fix
    monkeypatch.setenv("CRAWLRX_FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
    check = doctor._captcha_check()
    assert check.ok and "flaresolverr" in check.detail
