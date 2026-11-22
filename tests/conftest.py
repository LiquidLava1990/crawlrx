"""Shared fixtures: isolated config/data dirs and a fake OpenAI-compatible server."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

# Keep caches/logs written at import time out of the developer's real data dir.
os.environ.setdefault("CRAWLRX_DATA_DIR", tempfile.mkdtemp(prefix="crawlrx-test-"))

# The optional challenge-solving service lives at ``crawlrx/solver_service/``
# and is imported as a normal subpackage; no path aliasing is needed.
from crawlrx import captcha_handoff, model_backend  # noqa: E402

_MODEL_ENV = (
    "CRAWLRX_LLM_BASE_URL", "OPENAI_BASE_URL", "CRAWLRX_LLM_API_KEY", "OPENAI_API_KEY",
    "CRAWLRX_LLM_MODEL", "CRAWLRX_VISION_MODEL", "CRAWLRX_LLM_CANDIDATES", "CRAWLRX_LLM_TIMEOUT",
    "CRAWLRX_CAPTCHA_HANDOFF", "CRAWLRX_HEADLESS", "CRAWLRX_CAPTCHA_MODE", "CRAWLRX_SOLVERS",
    "CRAWLRX_FLARESOLVERR_URL", "CRAWLRX_SOLVER_SERVICE_URL", "CRAWLRX_SOLVER_SERVICE_KEY",
)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch) -> Iterator[None]:
    """Each test gets empty model env vars, its own config dir and fresh caches."""
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRAWLRX_HOME", str(tmp_path / "config"))
    # Point auto-detection at a closed port so a real local Ollama never leaks in.
    monkeypatch.setattr(model_backend, "LOCAL_SERVER_CANDIDATES", (("Ollama", "http://127.0.0.1:9/v1"),))
    model_backend._resolved_cache.clear()
    model_backend._negative_until[0] = 0.0
    model_backend._thinking_off.clear()
    captcha_handoff.set_handoff(None)
    yield
    model_backend._resolved_cache.clear()
    captcha_handoff.set_handoff(None)


class FakeModelServer:
    """Minimal OpenAI-compatible server: GET /v1/models, POST /v1/chat/completions."""

    def __init__(self, models: list[str], reply: str, thinking: bool = False) -> None:
        self.models = models
        self.reply = reply
        self.thinking = thinking  # emulate a reasoning model that needs reasoning_effort=none
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _send(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/v1/models":
                    self._send({"data": [{"id": m} for m in server.models]})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append(payload)
                server.auth_headers.append(self.headers.get("Authorization", ""))
                if server.thinking and payload.get("reasoning_effort") != "none":
                    message = {"role": "assistant", "content": "", "reasoning": "Thinking..."}
                else:
                    message = {"role": "assistant", "content": server.reply}
                self._send({"model": payload.get("model"), "choices": [{"message": message}]})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeModelServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def fake_model_server() -> Iterator[FakeModelServer]:
    with FakeModelServer(models=["llama3.2:3b", "qwen2.5vl:3b", "nomic-embed-text"], reply='{"title": "Example"}') as srv:
        yield srv
