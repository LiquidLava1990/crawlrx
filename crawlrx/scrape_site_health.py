"""Persistent scrape domain health ledger."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from .paths import env_path

_HEALTH_PATH = Path(env_path("CRAWLRX_SCRAPE_SITE_HEALTH", "scrape_site_health.json"))
_HEALTH_CACHE: dict[str, dict[str, Any]] | None = None
_SUCCESS_RECOVERY = float(os.getenv("CRAWLRX_SCRAPE_HEALTH_SUCCESS_RECOVERY", "0.08"))
_FAILURE_PENALTY = float(os.getenv("CRAWLRX_SCRAPE_HEALTH_FAILURE_PENALTY", "0.18"))
_BLOCK_PENALTY = float(os.getenv("CRAWLRX_SCRAPE_HEALTH_BLOCK_PENALTY", "0.3"))
_QUARANTINE_TTL_S = int(os.getenv("CRAWLRX_SCRAPE_HEALTH_QUARANTINE_S", "900"))
_DIAGNOSTIC_RELEASE_FLOOR = float(os.getenv("CRAWLRX_SCRAPE_HEALTH_DIAGNOSTIC_RELEASE_FLOOR", "0.21"))
_DIAGNOSTIC_RELEASE_CEILING = float(os.getenv("CRAWLRX_SCRAPE_HEALTH_DIAGNOSTIC_RELEASE_CEILING", "0.64"))
_NEUTRAL_ERRORS = {"selector_empty"}


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or url).lower()


def _now() -> float:
    return time.time()


def _load_health() -> dict[str, dict[str, Any]]:
    global _HEALTH_CACHE
    if _HEALTH_CACHE is not None:
        return _HEALTH_CACHE
    try:
        data = json.loads(_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    _HEALTH_CACHE = data if isinstance(data, dict) else {}
    return _HEALTH_CACHE


def _save_health(data: dict[str, dict[str, Any]]) -> None:
    try:
        _HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HEALTH_PATH.write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


def _empty_state(domain: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "score": 1.0,
        "health_status": "healthy",
        "successes": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "last_strategy": None,
        "last_source": None,
        "last_error_type": None,
        "last_error": None,
        "last_seen": None,
    }


def _status(score: float, consecutive_failures: int) -> str:
    if score <= 0.2 or consecutive_failures >= 3:
        return "quarantined"
    if score < 0.65 or consecutive_failures:
        return "degraded"
    return "healthy"


def _timestamp(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _quarantine_expiry(state: dict[str, Any]) -> float:
    explicit_until = float(state.get("quarantined_until_s", 0.0) or 0.0)
    if explicit_until > 0:
        return explicit_until
    last_seen = _timestamp(state.get("last_seen"))
    if last_seen <= 0:
        return 0.0
    return last_seen + _QUARANTINE_TTL_S


def _mark_degraded_release(
    state: dict[str, Any],
    *,
    now: float,
    source: str,
    reason: str,
) -> dict[str, Any]:
    score = float(state.get("score", _DIAGNOSTIC_RELEASE_CEILING))
    state["score"] = round(
        min(_DIAGNOSTIC_RELEASE_CEILING, max(_DIAGNOSTIC_RELEASE_FLOOR, score)),
        3,
    )
    state["health_status"] = "degraded"
    state["consecutive_failures"] = 0
    state["last_error_type"] = None
    state["last_error"] = None
    state["last_source"] = source
    state["last_strategy"] = reason
    state["last_seen"] = now
    state["quarantined_until_s"] = 0.0
    return state


def _expire_quarantine_if_stale(domain: str, state: dict[str, Any]) -> bool:
    if state.get("health_status") != "quarantined":
        return False
    now = _now()
    expiry = _quarantine_expiry(state)
    if expiry <= 0 or now < expiry:
        return False
    _mark_degraded_release(
        state,
        now=now,
        source="scrape_site_health",
        reason="quarantine_ttl_expired",
    )
    state["quarantine_expired_at"] = now
    state["domain"] = domain
    return True


def record_scrape_site_outcome(url: str, raw: dict[str, Any]) -> None:
    """Update the persistent health state for a scrape target domain."""
    domain = _domain(url)
    data = _load_health()
    state = dict(data.get(domain) or _empty_state(domain))
    success = bool(raw.get("success"))
    error_type = raw.get("error_type")
    score = float(state.get("score", 1.0))
    if success:
        score = min(1.0, score + _SUCCESS_RECOVERY)
        state["successes"] = int(state.get("successes", 0)) + 1
        state["consecutive_failures"] = 0
        state["last_error_type"] = None
        state["last_error"] = None
    else:
        penalty = 0.0 if error_type in _NEUTRAL_ERRORS else (
            _BLOCK_PENALTY if error_type in ("blocked", "captcha_required") else _FAILURE_PENALTY
        )
        score = max(0.0, score - penalty)
        state["failures"] = int(state.get("failures", 0)) + 1
        if error_type not in _NEUTRAL_ERRORS:
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        state["last_error_type"] = error_type or "unknown"
        state["last_error"] = raw.get("error")
    state["score"] = round(score, 3)
    state["health_status"] = _status(score, int(state.get("consecutive_failures", 0)))
    if state["health_status"] == "quarantined":
        state["quarantined_until_s"] = max(
            float(state.get("quarantined_until_s", 0.0) or 0.0),
            _now() + _QUARANTINE_TTL_S,
        )
    elif success:
        state["quarantined_until_s"] = 0.0
    state["last_strategy"] = raw.get("strategy")
    state["last_source"] = raw.get("source")
    state["last_seen"] = _now()
    data[domain] = state
    _save_health(data)


def release_scrape_site_quarantine(url: str, *, source: str, reason: str) -> None:
    """Move a quarantined scrape domain to degraded after external proof of recovery."""
    domain = _domain(url)
    data = _load_health()
    state = data.get(domain)
    if not state or state.get("health_status") != "quarantined":
        return
    now = _now()
    released = _mark_degraded_release(dict(state), now=now, source=source, reason=reason)
    released["released_at"] = now
    released["release_source"] = source
    released["release_reason"] = reason
    data[domain] = released
    _save_health(data)


def get_scrape_site_health() -> dict[str, dict[str, Any]]:
    """Return current scrape domain health states."""
    data = _load_health()
    changed = False
    for domain, state in data.items():
        changed = _expire_quarantine_if_stale(domain, state) or changed
    if changed:
        _save_health(data)
    return dict(data)


def get_scrape_site_health_summary() -> dict[str, int]:
    """Return count by health status."""
    summary = {"healthy": 0, "degraded": 0, "quarantined": 0, "unknown": 0}
    for state in get_scrape_site_health().values():
        key = str(state.get("health_status") or "unknown")
        if key not in summary:
            key = "unknown"
        summary[key] += 1
    return summary


def is_scrape_site_quarantined(url: str) -> bool:
    """True when the domain health ledger says to avoid more scrape work."""
    domain = _domain(url)
    data = _load_health()
    state = data.get(domain)
    if state and _expire_quarantine_if_stale(domain, state):
        _save_health(data)
    return bool(state and state.get("health_status") == "quarantined")


def reset_scrape_site_health_cache() -> None:
    """Test helper: clear the in-process health cache."""
    global _HEALTH_CACHE
    _HEALTH_CACHE = None
