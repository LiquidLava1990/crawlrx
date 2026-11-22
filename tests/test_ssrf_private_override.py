"""CRAWLRX_SSRF_ALLOW_PRIVATE: opt-in escape hatch for internal targets.

Default stays strict (loopback/private rejected). Setting the env var allows
self-hosted intranet crawls and local fixture testing.
"""
from __future__ import annotations

import pytest

from crawlrx._utils_safety import is_safe_url


LOOPBACK_CASES = [
    "http://127.0.0.1:8080/x",
    "http://localhost:9000/",
    "http://192.168.1.10/dashboard",
    "http://10.0.0.5/admin",
]


@pytest.mark.parametrize("url", LOOPBACK_CASES)
def test_default_blocks_internal_urls(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRAWLRX_SSRF_ALLOW_PRIVATE", raising=False)
    assert is_safe_url(url) is False


@pytest.mark.parametrize("url", LOOPBACK_CASES)
def test_override_allows_internal_urls(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLRX_SSRF_ALLOW_PRIVATE", "1")
    assert is_safe_url(url) is True


def test_public_urls_unaffected_by_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLRX_SSRF_ALLOW_PRIVATE", "1")
    assert is_safe_url("https://example.com/page") is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_override_requires_truthy_value(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLRX_SSRF_ALLOW_PRIVATE", value)
    assert is_safe_url("http://127.0.0.1:8080/x") is False
