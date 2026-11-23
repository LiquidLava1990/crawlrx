"""crawlrx-stealth: our own browser engine behind the stealth solver.

Replaces the pip botright facade with an in-house engine:
- coherent identity (browserforge UA + brands, our fingerprint pools)
- CDP user-agent override with brands/fullVersionList/platformVersion
- solve hooks (solve_hcaptcha / solve_recaptcha) backed by CrawlRX bridges
- engine lane chosen by CRAWLRX_STEALTH_ENGINE=own|botright (strangler fig)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from crawlrx.solver_service.solvers import _botright_browser  # noqa: E402


class _FakeCDP:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> None:
        self.sent.append((method, params or {}))


class _FakePage:
    def __init__(self) -> None:
        self.cdp = _FakeCDP()
        self.init_scripts: list[str] = []

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


def _engine_modules() -> tuple[Any, Any]:
    from crawlrx.solver_service._stealth_engine import fingerprint, page as page_mod

    return fingerprint, page_mod


def test_identity_is_coherent_ua_brands_version() -> None:
    """UA version, brands, and fullVersionList must agree."""
    browserforge = pytest.importorskip("browserforge")
    fingerprint, _ = _engine_modules()
    ident = fingerprint.build_identity()

    ua = ident.user_agent
    version = ua.split("Chrome/")[1].split(" ")[0]
    assert version, "UA must carry Chrome/<version>"
    assert any(ident.brand_brand in b for b in [ident.brand_brand]), "brand recorded"
    # brands include the same major version as the UA
    brands = {name: ver for name, ver in ident.brands}
    majors = {ver.split(".")[0] for ver in brands.values()}
    assert version.split(".")[0] in majors, f"brand majors {majors} must match UA {version}"
    assert ident.full_version_list, "fullVersionList required for CDP override"


def test_cdp_override_carries_brands_and_platform() -> None:
    fingerprint, page_mod = _engine_modules()
    ident = fingerprint.build_identity()
    fake_page = _FakePage()

    import asyncio

    asyncio.run(page_mod.apply_identity_cdp(fake_page, ident))

    methods = [m for m, _ in fake_page.cdp.sent]
    assert "Emulation.setUserAgentOverride" in methods
    assert "Network.setUserAgentOverride" in methods
    params = dict(fake_page.cdp.sent)["Emulation.setUserAgentOverride"]
    meta = params["userAgentMetadata"]
    assert meta["brands"], "brands list required"
    assert meta["fullVersionList"], "fullVersionList required"
    assert params["userAgent"] == ident.user_agent
    assert params["platform"] in ("Win32", "MacIntel", "Linux x86_64")


def test_stealth_page_exposes_solve_hooks() -> None:
    page_mod = pytest.importorskip("crawlrx.solver_service._stealth_engine.page")
    from crawlrx.solver_service._stealth_engine import page as engine_page

    assert hasattr(engine_page.StealthPage, "solve_hcaptcha")
    assert hasattr(engine_page.StealthPage, "solve_recaptcha")


def test_engine_flag_selects_own_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_botright_client must build the own engine by default, botright when asked."""
    pytest.importorskip("browserforge")

    class _Host:
        _botright_client = None
        headless = True

    import asyncio

    host = _Host()

    monkeypatch.delenv("CRAWLRX_STEALTH_ENGINE", raising=False)
    client = asyncio.run(_botright_browser.get_botright_client(host, None, False))
    assert client is not None
    assert type(client).__module__.startswith("crawlrx.solver_service._stealth_engine"), (
        f"default lane must be the own engine, got {type(client).__module__}"
    )

    # fresh host for the legacy-lane probe
    host2 = _Host()
    monkeypatch.setenv("CRAWLRX_STEALTH_ENGINE", "botright")
    with pytest.raises(Exception) as excinfo:
        asyncio.run(_botright_browser.get_botright_client(host2, None, False))
    assert "botright" in str(excinfo.value).lower()


def test_engine_new_browser_uses_persistent_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine launches a persistent context with automation flag removed."""
    from crawlrx.solver_service._stealth_engine import engine as engine_mod

    captured: dict[str, Any] = {}

    class _FakePlaywrightChromium:
        async def launch_persistent_context(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return _FakeContext()

    class _FakeChromium:
        def __init__(self) -> None:
            self.launch_persistent_context = _FakePlaywrightChromium().launch_persistent_context

    class _FakePlaywright:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()

    class _FakeContext:
        async def new_page(self) -> Any:
            return _FakePage()

        async def close(self) -> None:
            pass

    import asyncio

    async def scenario() -> Any:
        eng = engine_mod.StealthEngine(headless=True)
        eng._playwright = _FakePlaywright()  # type: ignore[attr-defined]
        return await eng.new_browser()

    ctx = asyncio.run(scenario())
    assert captured.get("ignore_default_args") == ["--enable-automation"]
    assert captured.get("user_data_dir"), "persistent context needs a user data dir"
    assert ctx is not None
