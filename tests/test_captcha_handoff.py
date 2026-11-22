"""Captchas are handed to a human, never solved automatically."""
from __future__ import annotations

import asyncio
import importlib
import pkgutil

import pytest

import crawlrx
from crawlrx import _scrape_attempt, captcha_handoff

CHALLENGE_HTML = '<html><title>Just a moment...</title><div class="cf-turnstile"></div></html>'


class FakePage:
    """Stands in for a Playwright page; becomes un-blocked after ``clear_after`` reads."""

    def __init__(self, clear_after: int = 10**9) -> None:
        self.reads = 0
        self.clear_after = clear_after
        self.url = "https://example.com/"

    async def content(self) -> str:
        self.reads += 1
        return "<html><body>ok</body></html>" if self.reads > self.clear_after else CHALLENGE_HTML

    async def title(self) -> str:
        return "Example" if self.reads > self.clear_after else "Just a moment..."

    async def bring_to_front(self) -> None:
        return None


def test_solver_code_lives_only_in_the_optional_solvers_package() -> None:
    names = {m.name for m in pkgutil.iter_modules(crawlrx.__path__)}
    stray = {n for n in names if any(word in n for word in ("solver", "challenge_resolver", "recaptcha", "funcaptcha"))}
    assert stray == {"solvers"}


def test_solvers_are_not_loaded_in_handoff_mode() -> None:
    import subprocess
    import sys

    code = (
        "import sys, crawlrx.main, crawlrx.captcha_handoff as h; h.get_handoff(); "
        "print('crawlrx.solvers' in sys.modules)"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("CRAWLRX_CAPTCHA")}
    env["CRAWLRX_CAPTCHA_MODE"] = "handoff"
    out = subprocess.run(
        [sys.executable, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=120,
    )
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr[-500:]


def test_classify_challenge() -> None:
    assert captcha_handoff.classify_challenge(CHALLENGE_HTML) == "turnstile"
    assert captcha_handoff.classify_challenge('<div class="g-recaptcha">') == "recaptcha"


def test_none_handoff_reports_to_caller(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", "handoff")
    captcha_handoff.set_handoff(None)
    cleared, info = asyncio.run(captcha_handoff.hand_off(FakePage(), "https://example.com/", CHALLENGE_HTML))
    assert cleared is False
    assert info["handoff"] == "none" and info["kind"] == "turnstile"
    assert "human" in info["action"]
    assert captcha_handoff.browser_headless() is True


def test_browser_handoff_waits_for_user(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLRX_CAPTCHA_MODE", "handoff")
    monkeypatch.setenv("CRAWLRX_CAPTCHA_HANDOFF", "browser")
    captcha_handoff.set_handoff(None)
    handoff = captcha_handoff.get_handoff()
    assert isinstance(handoff, captcha_handoff.VisibleBrowserHandoff)
    assert captcha_handoff.browser_headless() is False
    handoff.poll_s = 0.01
    page = FakePage(clear_after=2)
    cleared, _info = asyncio.run(captcha_handoff.hand_off(page, page.url, CHALLENGE_HTML))
    assert cleared is True


def test_browser_handoff_times_out() -> None:
    captcha_handoff.set_handoff(captcha_handoff.VisibleBrowserHandoff(wait_s=0.05, poll_s=0.01))
    cleared, _ = asyncio.run(captcha_handoff.hand_off(FakePage(), "https://example.com/", CHALLENGE_HTML))
    assert cleared is False


def test_custom_handoff_plugs_in_and_errors_are_contained() -> None:
    class Broken:
        name = "broken"
        needs_visible_browser = False

        async def resolve(self, page, challenge) -> bool:
            raise RuntimeError("boom")

    captcha_handoff.set_handoff(Broken())
    cleared, info = asyncio.run(captcha_handoff.hand_off(FakePage(), "https://example.com/", CHALLENGE_HTML))
    assert cleared is False and info["handoff"] == "broken"
    with pytest.raises(TypeError):
        captcha_handoff.set_handoff(object())  # type: ignore[arg-type]


def test_scrape_attempt_reports_captcha_required(monkeypatch) -> None:
    """The /scrape browser path routes bot blocks through the hand-off."""
    calls: list[str] = []

    async def fake_hand_off(page, url, html, title=""):
        calls.append(url)
        return False, {"url": url, "kind": "turnstile", "handoff": "none", "action": "solve it"}

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(captcha_handoff, "hand_off", fake_hand_off)
    for name in ("_wait_for_page", "_await_extra_waits", "_maybe_click", "_run_js_inject",
                 "human_mouse_movements", "random_scroll_pause"):
        monkeypatch.setattr(_scrape_attempt, name, noop)

    async def size_ok(_page):
        return True

    monkeypatch.setattr(_scrape_attempt, "check_content_size", size_ok)

    class Page(FakePage):
        async def goto(self, *_a, **_k):
            return None

        async def wait_for_timeout(self, _ms):
            return None

    params = _scrape_attempt.ScrapeAttemptParams(
        url="https://example.com/", selector=None, selectors=None, extract="text", wait_for=None, timeout=5000,
    )
    result: dict = {}
    out = asyncio.run(_scrape_attempt._do_scrape_attempt_body(Page(), params, result, 0.0))
    assert calls == ["https://example.com/"]
    assert out["error_type"] == "captcha_required"
    assert out["captcha"]["kind"] == "turnstile"


def test_crawl_attempt_wires_handoff() -> None:
    module = importlib.import_module("crawlrx._crawl_attempt")
    assert module.clear_or_flag is captcha_handoff.clear_or_flag
