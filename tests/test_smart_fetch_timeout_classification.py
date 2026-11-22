"""Regression: a hung server must yield error_type='timeout', not a 500.

Chain under test: primp.PrimpError registration (network_exception_types) ->
_run_tier_checked timeout detection -> smart_fetch timeout classification.
Caught live: scraping a hung fixture returned HTTP 500 with a raw
primp.TimeoutError traceback instead of a controlled error envelope.
"""
from __future__ import annotations

import socket
import threading
from typing import Iterator, Tuple

import pytest

from crawlrx import smart_fetch


@pytest.fixture()
def hung_server() -> Iterator[Tuple[str, int]]:
    """TCP server that accepts connections and never responds."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _accept_forever() -> None:
        while True:
            try:
                conn, _ = srv.accept()  # accept, then hold silently
            except OSError:
                return

    thread = threading.Thread(target=_accept_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        srv.close()


def test_hung_server_classified_as_timeout(
    hung_server: Tuple[str, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, port = hung_server
    monkeypatch.setenv("CRAWLRX_SSRF_ALLOW_PRIVATE", "1")
    result = smart_fetch.smart_fetch(f"http://{host}:{port}/hang", timeout=2)
    assert result["success"] is False
    assert result["error_type"] == "timeout", f"got: {result.get('error')!r}"
    assert "timed out" in (result.get("error") or "").lower()


def test_connection_refused_classification_is_explicit() -> None:
    """Classifier keys on timeout type names or 'timed out' messages."""
    from crawlrx.smart_fetch import _is_timeout_error

    assert _is_timeout_error(TimeoutError("operation timed out"))
    assert _is_timeout_error(OSError("timed out waiting"))

    class ConnectTimeout(RuntimeError):
        pass

    assert _is_timeout_error(ConnectTimeout("server did not respond"))
    assert not _is_timeout_error(ConnectionResetError("connection reset by peer"))
    assert not _is_timeout_error(ValueError("bad url"))


def test_scrape_failure_classification_uses_classifier() -> None:
    """Exhausted browser retries classify by message, not hardcoded network_error."""
    from crawlrx._scrape_attempt import _empty_scrape_failure

    timeout_case = _empty_scrape_failure("http://x/", TimeoutError("Timeout 4000ms exceeded."), 4000)
    assert timeout_case["error_type"] == "timeout"

    blocked_case = _empty_scrape_failure("http://x/", RuntimeError("request blocked by bot wall"), 100)
    assert blocked_case["error_type"] == "blocked"

    other_case = _empty_scrape_failure("http://x/", ValueError("boom"), 100)
    assert other_case["error_type"] == "network_error"
