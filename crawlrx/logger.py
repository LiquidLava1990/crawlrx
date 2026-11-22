"""CrawlRX request logger — daily JSON log files with sanitization."""

import json
import time
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Iterator, List, Optional
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from .paths import env_dir

logger = logging.getLogger("crawlrx")

LOG_DIR = Path(env_dir("CRAWLRX_LOG_DIR", "logs"))

try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except BROWSER_SCRAPE_EXCEPTIONS as e:
    logger.error("Failed to create log directory %s: %s", LOG_DIR, e)

_log_lock = threading.Lock()

_LARGE_CONTENT_KEYS = ("html", "content", "markdown", "text", "body")
_OPAQUE_BINARY_KEYS = ("screenshot_b64", "image_data")
_LARGE_TRUNCATE_AT = 200


@dataclass(frozen=True)
class RequestLogEntry:
    """Structured request-log payload."""

    endpoint: str
    method: str
    params: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    success: bool = True
    error: Optional[str] = None


def _summarize_large_value(value: Any) -> Any:
    """Replace oversize string values with ``<...N chars...>``; passthrough else."""
    if isinstance(value, str) and len(value) > _LARGE_TRUNCATE_AT:
        return f"<...{len(value)} chars...>"
    return value


def _sanitize_field(key: str, value: Any) -> Any:
    """Apply per-key sanitization for ``sanitize_params``."""
    if key in _LARGE_CONTENT_KEYS:
        return _summarize_large_value(value)
    if key in _OPAQUE_BINARY_KEYS:
        return "<base64_image_data>"
    return value


def sanitize_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate large content fields, mask base64 blobs, drop non-dict input."""
    if not isinstance(params, dict):
        return {}
    return {key: _sanitize_field(key, value) for key, value in params.items()}


def _build_entry_dict(entry: RequestLogEntry) -> Dict[str, Any]:
    """Materialise the on-disk JSON shape for a single log line."""
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": entry.endpoint,
        "method": entry.method,
        "params": sanitize_params(entry.params),
        "elapsed_ms": entry.elapsed_ms,
        "success": entry.success,
        "error": entry.error,
    }


def log_request(entry: RequestLogEntry) -> None:
    """Append a JSON log line to today's daily log file (thread-safe)."""
    payload = _build_entry_dict(entry)
    try:
        log_file = LOG_DIR / f"crawlrx-{time.strftime('%Y-%m-%d')}.log"
        with _log_lock:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Failed to write request log: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller


def _iter_log_entries(log_file: Path) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON entries from ``log_file``; skip blank/invalid lines."""
    with open(log_file, "r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                continue


def get_logs_for_date(date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return parsed log entries for ``date_str`` (YYYY-MM-DD); today by default."""
    if date_str is None:
        date_str = time.strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"crawlrx-{date_str}.log"
    if not log_file.exists():
        return []
    try:
        return list(_iter_log_entries(log_file))
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Failed to read logs: %s", exc)
        return []
