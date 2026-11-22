"""model_backend.py -- one setting, any OpenAI-compatible model server.

CrawlRX only needs a model for two features: ``POST /extract`` (LLM
structured extraction) and ``POST /vision/analyze`` (describe a screenshot
or image). Scraping, crawling and search work with no model at all.

Point CrawlRX at a model with a single setting::

    CRAWLRX_LLM_BASE_URL=http://localhost:11434/v1     # Ollama
    CRAWLRX_LLM_BASE_URL=http://localhost:1234/v1      # LM Studio
    CRAWLRX_LLM_BASE_URL=http://my-vllm-box:8000/v1    # vLLM / llama.cpp server
    CRAWLRX_LLM_BASE_URL=https://api.openai.com/v1     # any cloud API (+ CRAWLRX_LLM_API_KEY)

Optional overrides: ``CRAWLRX_LLM_API_KEY``, ``CRAWLRX_LLM_MODEL``,
``CRAWLRX_VISION_MODEL``, ``CRAWLRX_LLM_TIMEOUT``. ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY`` are honoured as fallbacks so existing setups just work.

Resolution order: environment -> saved config file (written by
``crawlrx doctor --save``) -> auto-detect a local Ollama / LM Studio.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx

logger = logging.getLogger("crawlrx.model_backend")

# Well-known default ports of local model servers. These are probed only when
# the user has not configured an endpoint; nothing here is a required host.
LOCAL_SERVER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Ollama", "http://localhost:11434/v1"),
    ("LM Studio", "http://localhost:1234/v1"),
)

# Substrings that identify a vision-capable model by name.
VISION_NAME_HINTS: tuple[str, ...] = (
    "vl", "vision", "llava", "minicpm-v", "moondream", "gemma3", "pixtral",
    "gpt-4o", "gpt-4.1", "gpt-5", "claude", "gemini", "bakllava", "granite3.2-vision",
)

_DEFAULT_TIMEOUT_S = 120.0
_PROBE_TIMEOUT_S = 2.5


class ModelBackendUnavailable(RuntimeError):
    """No model endpoint is reachable; the message says how to fix it."""


def setup_hint() -> str:
    """Actionable instructions for connecting a model."""
    return (
        "No AI model is reachable. Scraping and search still work; only /extract and "
        "/vision need a model. Pick one:\n"
        "  1. Ollama (free, local): install from https://ollama.com/download, then run\n"
        "       ollama pull qwen2.5vl:3b\n"
        "  2. LM Studio (free, local): load a model and click 'Start Server'.\n"
        "  3. Docker: docker compose --profile ollama up -d   (starts Ollama + a small model)\n"
        "  4. Any OpenAI-compatible server or cloud API:\n"
        "       CRAWLRX_LLM_BASE_URL=https://api.example.com/v1\n"
        "       CRAWLRX_LLM_API_KEY=<your key>\n"
        "Then run `crawlrx doctor` to confirm."
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSettings:
    """Resolved model endpoint settings."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    vision_model: str = ""
    timeout_s: float = _DEFAULT_TIMEOUT_S
    source: str = "unset"  # env | config | autodetect | unset

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to show in logs / API responses (key redacted)."""
        data = asdict(self)
        data["api_key"] = "set" if self.api_key else ""
        return data


def config_dir() -> Path:
    """Per-user CrawlRX config directory (``CRAWLRX_HOME`` overrides)."""
    override = os.getenv("CRAWLRX_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "crawlrx"
    xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crawlrx"


def config_path() -> Path:
    return config_dir() / "config.json"


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _normalise_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url  # "localhost:11434" -> "http://localhost:11434"
    if url and not url.endswith("/v1") and "/v1/" not in url and url.count("/") <= 2:
        # Bare host:port of a local server -> OpenAI-compatible path.
        url = url + "/v1"
    return url


def llm_timeout_seconds() -> float:
    """Per-call model timeout (``CRAWLRX_LLM_TIMEOUT``, default 120 s)."""
    return _timeout_from_env()


def _timeout_from_env() -> float:
    raw = _env("CRAWLRX_LLM_TIMEOUT")
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_S
    except ValueError:
        logger.warning("CRAWLRX_LLM_TIMEOUT=%r is not a number; using %.0fs", raw, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S


def _settings_from_env() -> Optional[ModelSettings]:
    base_url = _env("CRAWLRX_LLM_BASE_URL", "OPENAI_BASE_URL")
    if not base_url:
        return None
    return ModelSettings(
        base_url=_normalise_base_url(base_url),
        api_key=_env("CRAWLRX_LLM_API_KEY", "OPENAI_API_KEY"),
        model=_env("CRAWLRX_LLM_MODEL"),
        vision_model=_env("CRAWLRX_VISION_MODEL"),
        timeout_s=_timeout_from_env(),
        source="env",
    )


def _settings_from_file(path: Optional[Path] = None) -> Optional[ModelSettings]:
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable CrawlRX config %s (%s). Fix or delete it.", path, exc)
        return None
    model_cfg = raw.get("model", {}) if isinstance(raw, dict) else {}
    base_url = str(model_cfg.get("base_url", "")).strip()
    if not base_url:
        return None
    return ModelSettings(
        base_url=_normalise_base_url(base_url),
        api_key=_env("CRAWLRX_LLM_API_KEY", "OPENAI_API_KEY") or str(model_cfg.get("api_key", "")),
        model=_env("CRAWLRX_LLM_MODEL") or str(model_cfg.get("model", "")),
        vision_model=_env("CRAWLRX_VISION_MODEL") or str(model_cfg.get("vision_model", "")),
        timeout_s=_timeout_from_env(),
        source="config",
    )


def save_settings(settings: ModelSettings, path: Optional[Path] = None) -> Path:
    """Persist the endpoint (never the API key) to the user config file."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    existing["model"] = {
        "base_url": settings.base_url,
        "model": settings.model,
        "vision_model": settings.vision_model,
    }
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(base_url: str, api_key: str = "", timeout_s: float = _PROBE_TIMEOUT_S) -> list[str]:
    """Return model ids served at ``base_url`` (raises ``httpx.HTTPError``)."""
    resp = httpx.get(f"{base_url}/models", headers=_auth_headers(api_key), timeout=timeout_s)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]


def is_vision_model(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VISION_NAME_HINTS)


def pick_models(available: Sequence[str]) -> tuple[str, str]:
    """Choose (text_model, vision_model) from what a server offers."""
    usable = [m for m in available if "embed" not in m.lower()]
    if not usable:
        return "", ""
    vision = next((m for m in usable if is_vision_model(m)), "")
    text = next((m for m in usable if not is_vision_model(m)), "") or vision
    return text, vision


def server_candidates() -> list[tuple[str, str]]:
    """Default local servers plus any from ``CRAWLRX_LLM_CANDIDATES``.

    ``CRAWLRX_LLM_CANDIDATES`` is a comma-separated list of base URLs to
    probe first (the Docker compose file uses it for ``ollama`` and
    ``host.docker.internal``).
    """
    extra = [u.strip() for u in _env("CRAWLRX_LLM_CANDIDATES").split(",") if u.strip()]
    candidates = [(_label_for(url), _normalise_base_url(url)) for url in extra]
    seen = {url for _, url in candidates}
    candidates.extend(c for c in LOCAL_SERVER_CANDIDATES if c[1] not in seen)
    return candidates


def _label_for(url: str) -> str:
    if ":11434" in url:
        return "Ollama"
    if ":1234" in url:
        return "LM Studio"
    return "OpenAI-compatible server"


def detect_local_servers(timeout_s: float = _PROBE_TIMEOUT_S) -> list[dict[str, Any]]:
    """Probe Ollama / LM Studio default ports (plus configured candidates)."""
    found: list[dict[str, Any]] = []
    for name, base_url in server_candidates():
        try:
            models = list_models(base_url, timeout_s=timeout_s)
        except (httpx.HTTPError, ValueError):
            continue
        found.append({"name": name, "base_url": base_url, "models": models})
    return found


def _autodetect_settings() -> ModelSettings:
    for server in detect_local_servers():
        text, vision = pick_models(server["models"])
        if text or vision:
            return ModelSettings(
                base_url=server["base_url"], model=text, vision_model=vision,
                timeout_s=_timeout_from_env(), source="autodetect",
            )
    return ModelSettings()


_resolved_cache: list[ModelSettings] = []
_NEGATIVE_TTL_S = 30.0
_negative_until: list[float] = [0.0]


def resolve_settings(refresh: bool = False, autodetect: bool = True) -> ModelSettings:
    """Resolve the model endpoint.

    A found endpoint is cached for the process; "nothing found" is cached for
    30 s so a missing model server does not slow every request.
    """
    if _resolved_cache and not refresh:
        return _resolved_cache[0]
    settings = _settings_from_env() or _settings_from_file()
    if settings is None:
        recently_empty = time.monotonic() < _negative_until[0]
        if not autodetect or (recently_empty and not refresh):
            return ModelSettings()
        settings = _autodetect_settings()
    if settings.configured:
        _resolved_cache[:] = [settings]
    else:
        _negative_until[0] = time.monotonic() + _NEGATIVE_TTL_S
    return settings


def _fill_missing_models(settings: ModelSettings) -> ModelSettings:
    """Ask the server which models it has when the user named none."""
    if settings.model and settings.vision_model:
        return settings
    try:
        available = list_models(settings.base_url, settings.api_key)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("model listing failed at %s: %s", settings.base_url, exc)
        return settings
    text, vision = pick_models(available)
    return replace(
        settings,
        model=settings.model or text,
        vision_model=settings.vision_model or vision or settings.model,
    )


def require_settings(vision: bool = False) -> ModelSettings:
    """Resolved settings with a usable model, or ``ModelBackendUnavailable``."""
    settings = resolve_settings()
    if not settings.configured:
        raise ModelBackendUnavailable(setup_hint())
    filled = _fill_missing_models(settings)
    if filled != settings and filled.model:
        _resolved_cache[:] = [filled]  # remember the server's model list answer
    settings = filled
    chosen = settings.vision_model if vision else settings.model
    if not chosen:
        kind = "vision" if vision else "text"
        env_name = "CRAWLRX_VISION_MODEL" if vision else "CRAWLRX_LLM_MODEL"
        raise ModelBackendUnavailable(
            f"The model server at {settings.base_url} has no {kind} model to use. "
            f"Set {env_name}=<model id>, or pull one (e.g. `ollama pull qwen2.5vl:3b`)."
        )
    return settings


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

async def chat_completion(
    messages: list[dict[str, Any]],
    *,
    vision: bool = False,
    model: str = "",
    temperature: float = 0.1,
    max_tokens: int = 4000,
) -> dict[str, Any]:
    """POST ``/chat/completions`` and return the raw JSON response.

    Raises ``ModelBackendUnavailable`` (not configured / unreachable) or
    ``httpx.HTTPStatusError`` (server answered with an error).
    """
    settings = await asyncio.to_thread(require_settings, vision)
    chosen = model or (settings.vision_model if vision else settings.model)
    body: dict[str, Any] = {"model": chosen, "messages": messages,
                            "temperature": temperature, "max_tokens": max_tokens}
    configured_effort = _env("CRAWLRX_LLM_REASONING_EFFORT")
    key = f"{settings.base_url}|{chosen}"
    if configured_effort or key in _thinking_off:
        body["reasoning_effort"] = configured_effort or "none"
    payload = await _post_chat(settings, body)
    if response_text(payload) or "reasoning_effort" in body or not _only_reasoning(payload):
        return payload
    # A "thinking" model spent its whole budget reasoning. Ask once more with
    # reasoning off (Ollama, vLLM and newer OpenAI models accept this); servers
    # that reject the parameter keep the first answer.
    try:
        retry = await _post_chat(settings, {**body, "reasoning_effort": "none"})
    except (httpx.HTTPStatusError, ModelBackendUnavailable):
        return payload  # server rejected / timed out on the retry: keep the first answer
    if response_text(retry):
        _thinking_off.add(key)
        return retry
    return payload


_thinking_off: set[str] = set()  # "base_url|model" pairs that need reasoning_effort=none


async def _post_chat(settings: ModelSettings, body: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.timeout_s) as client:
            resp = await client.post(
                f"{settings.base_url}/chat/completions",
                headers=_auth_headers(settings.api_key),
                json=body,
            )
    except httpx.RequestError as exc:
        raise ModelBackendUnavailable(
            f"Model server {settings.base_url} is unreachable or too slow ({type(exc).__name__}). "
            "Is it running? Run `crawlrx doctor`; raise CRAWLRX_LLM_TIMEOUT for slow CPU models."
        ) from exc
    resp.raise_for_status()
    return resp.json()


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {}
    return message if isinstance(message, dict) else {}


def _only_reasoning(payload: dict[str, Any]) -> bool:
    message = _message(payload)
    return bool(message.get("reasoning") or message.get("reasoning_content"))


def response_text(payload: dict[str, Any]) -> str:
    """Pull the assistant text out of a chat-completions response."""
    return str(_message(payload).get("content") or "").strip()


def image_message(image_bytes: bytes, prompt: str, mime: str = "image/png") -> list[dict[str, Any]]:
    """OpenAI-style multimodal user message (works on Ollama, LM Studio, vLLM, cloud)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]


def sniff_image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


async def describe_image(
    image_bytes: bytes,
    prompt: str = "Describe this image in detail.",
    *,
    model: str = "",
    max_tokens: int = 1000,
) -> tuple[str, str]:
    """Run a vision prompt; returns ``(text, model_used)``."""
    if not image_bytes:
        raise ValueError("image is empty")
    messages = image_message(image_bytes, prompt, sniff_image_mime(image_bytes))
    payload = await chat_completion(messages, vision=True, model=model, max_tokens=max_tokens)
    return response_text(payload), str(payload.get("model") or model or "")


__all__ = [
    "LOCAL_SERVER_CANDIDATES",
    "ModelBackendUnavailable",
    "ModelSettings",
    "chat_completion",
    "config_dir",
    "config_path",
    "describe_image",
    "detect_local_servers",
    "image_message",
    "is_vision_model",
    "list_models",
    "llm_timeout_seconds",
    "pick_models",
    "server_candidates",
    "require_settings",
    "resolve_settings",
    "response_text",
    "save_settings",
    "setup_hint",
    "sniff_image_mime",
]
