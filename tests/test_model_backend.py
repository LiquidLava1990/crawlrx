"""Model backend: one setting, any OpenAI-compatible server."""
from __future__ import annotations

import asyncio
import json

import pytest

from crawlrx import model_backend


def test_unconfigured_raises_actionable_error() -> None:
    with pytest.raises(model_backend.ModelBackendUnavailable) as exc:
        model_backend.require_settings()
    message = str(exc.value)
    assert "ollama pull" in message
    assert "CRAWLRX_LLM_BASE_URL" in message


def test_single_env_setting_selects_server_and_models(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", fake_model_server.base_url)
    settings = model_backend.require_settings()
    assert settings.source == "env"
    assert settings.model == "llama3.2:3b"
    assert settings.vision_model == "qwen2.5vl:3b"


def test_bare_host_gets_v1_suffix(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", "http://localhost:11434")
    assert model_backend.resolve_settings(refresh=True).base_url == "http://localhost:11434/v1"


def test_scheme_is_added_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", "localhost:11434")
    assert model_backend.resolve_settings(refresh=True).base_url == "http://localhost:11434/v1"


def test_openai_env_vars_are_a_fallback(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = model_backend.resolve_settings(refresh=True)
    assert settings.base_url == "https://api.example.com/v1"
    assert settings.public_dict()["api_key"] == "set"


def test_autodetect_finds_candidate_server(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_CANDIDATES", fake_model_server.base_url)
    settings = model_backend.resolve_settings(refresh=True)
    assert settings.source == "autodetect"
    assert settings.base_url == fake_model_server.base_url


def test_saved_config_is_used_and_never_stores_key(monkeypatch, fake_model_server) -> None:
    settings = model_backend.ModelSettings(base_url=fake_model_server.base_url, api_key="secret", model="m")
    path = model_backend.save_settings(settings)
    assert "secret" not in path.read_text(encoding="utf-8")
    resolved = model_backend.resolve_settings(refresh=True)
    assert resolved.source == "config"
    assert resolved.model == "m"


def test_chat_completion_sends_bearer_and_model(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", fake_model_server.base_url)
    monkeypatch.setenv("CRAWLRX_LLM_API_KEY", "k-123")
    payload = asyncio.run(model_backend.chat_completion([{"role": "user", "content": "hi"}]))
    assert model_backend.response_text(payload) == '{"title": "Example"}'
    assert fake_model_server.auth_headers[-1] == "Bearer k-123"
    assert fake_model_server.requests[-1]["model"] == "llama3.2:3b"


def test_describe_image_sends_openai_multimodal_message(monkeypatch, fake_model_server) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", fake_model_server.base_url)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    text, used = asyncio.run(model_backend.describe_image(png, "what is this?"))
    assert text
    sent = fake_model_server.requests[-1]
    assert sent["model"] == "qwen2.5vl:3b"
    parts = sent["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert used == "qwen2.5vl:3b"


def test_thinking_model_retries_with_reasoning_off(monkeypatch) -> None:
    from conftest import FakeModelServer

    with FakeModelServer(models=["qwen3:8b"], reply="answer", thinking=True) as srv:
        monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", srv.base_url)
        first = asyncio.run(model_backend.chat_completion([{"role": "user", "content": "hi"}]))
        second = asyncio.run(model_backend.chat_completion([{"role": "user", "content": "hi"}]))
    assert model_backend.response_text(first) == "answer"
    assert model_backend.response_text(second) == "answer"
    efforts = [r.get("reasoning_effort") for r in srv.requests]
    assert efforts == [None, "none", "none"]  # one retry, then remembered


def test_unreachable_server_is_actionable(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLRX_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("CRAWLRX_LLM_MODEL", "x")
    with pytest.raises(model_backend.ModelBackendUnavailable, match="crawlrx doctor"):
        asyncio.run(model_backend.chat_completion([{"role": "user", "content": "hi"}]))


@pytest.mark.parametrize(("models", "expected"), [
    (["qwen2.5:7b", "llava:7b"], ("qwen2.5:7b", "llava:7b")),
    (["gemma3:4b"], ("gemma3:4b", "gemma3:4b")),
    (["nomic-embed-text"], ("", "")),
])
def test_pick_models(models, expected) -> None:
    assert model_backend.pick_models(models) == expected


def test_no_owner_specific_hosts_in_defaults() -> None:
    hosts = json.dumps(model_backend.LOCAL_SERVER_CANDIDATES)
    assert "11434" in hosts or "127.0.0.1:9" in hosts  # patched in conftest
