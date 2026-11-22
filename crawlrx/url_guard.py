"""Request-wide SSRF guard.

Every route that takes a URL, whatever its body shape, is covered: the
middleware scans the JSON body and the query string before routing and
refuses the request if any URL points at loopback, private, link-local or
otherwise internal address space (see ``is_safe_url``). Per-route checks and
the redirect checks in ``smart_fetch`` stay in place as further layers.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ._utils_safety import is_safe_url

_MAX_BODY_BYTES = 25 * 1024 * 1024
_MAX_DEPTH = 6
_URL_KEYS = {"url", "urls", "link", "links", "domain", "endpoint", "href"}
_URL_KEY_SUFFIXES = ("_url", "_urls", "_domain", "_link", "_links")
_SCHEMES = ("http://", "https://")


def _is_url_key(key: str) -> bool:
    lower = key.lower()
    return lower in _URL_KEYS or lower.endswith(_URL_KEY_SUFFIXES)


def _candidate(value: str, url_key: bool) -> Optional[str]:
    """The URL to check for a string value, or None when it is not a URL."""
    text = value.strip()
    if not text:
        return None
    if text.lower().startswith(_SCHEMES):
        return text
    if not url_key:
        return None
    # URL-ish field without an http(s) scheme: either another scheme (file:, gopher:)
    # or a bare host the route will prefix itself. Check it as http://<value>.
    return text if "://" in text else f"http://{text}"


def _iter_strings(node: Any, url_key: bool = False, depth: int = 0) -> Iterator[Tuple[str, bool]]:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, str):
        yield node, url_key
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, url_key=_is_url_key(str(key)), depth=depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item, url_key=url_key, depth=depth + 1)


def find_unsafe_url(payload: Any) -> Optional[str]:
    """Return the first URL in ``payload`` that targets internal address space."""
    for value, url_key in _iter_strings(payload):
        candidate = _candidate(value, url_key)
        if candidate is not None and not is_safe_url(candidate):
            return candidate
    return None


def _query_payload(query_string: bytes) -> dict:
    pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=False)
    payload: dict = {}
    for key, value in pairs:
        payload.setdefault(key, []).append(value)
    return payload


def _json_payload(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None  # Not JSON; _body_payload falls back to form decoding.


def _body_payload(body: bytes) -> Any:
    """Decode a body as JSON, else as form data; ``None`` when it is neither."""
    parsed = _json_payload(body)
    if parsed is not None or not body:
        return parsed
    try:
        return _query_payload(body)
    except (ValueError, UnicodeDecodeError):
        return None


def _blocked_response(url: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "url": url,
            "error": "URL blocked: internal/private network addresses are not allowed. "
                     "CrawlRX only fetches public http(s) URLs.",
            "error_type": "blocked",
        },
    )


async def _read_body(receive: Receive) -> Tuple[Optional[bytes], List[Message]]:
    """Buffer the request body; ``None`` when it exceeds ``_MAX_BODY_BYTES``."""
    chunks: List[bytes] = []
    messages: List[Message] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            return None, messages
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), messages


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "success": False,
            "error": f"Request body is larger than {_MAX_BODY_BYTES // (1024 * 1024)} MB. "
                     "Send a smaller image or split the batch.",
            "error_type": "payload_too_large",
        },
    )


class UrlGuardMiddleware:
    """Pure ASGI middleware: buffers the body, checks it, then replays it downstream."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body, messages = await _read_body(receive)
        if body is None:
            await _too_large_response()(scope, receive, send)
            return
        # Scan the body whatever its Content-Type claims: frameworks may still parse it.
        payloads = [_query_payload(scope.get("query_string", b"")), _body_payload(body)]
        unsafe = await asyncio.to_thread(find_unsafe_url, payloads)
        if unsafe is not None:
            await _blocked_response(unsafe)(scope, receive, send)
            return
        await self.app(scope, _replay(messages, receive), send)


def _replay(messages: List[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
