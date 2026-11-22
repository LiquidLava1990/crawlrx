"""High-signal diagnostics for the generic /scrape pipeline."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scrape_attempt import _do_scrape
from .scrape_site_health import release_scrape_site_quarantine
from .paths import env_path

router = APIRouter(prefix="/scrape", tags=["scrape"])

_DIAGNOSTIC_TARGET_URL = "https://example.com"
_DIAGNOSTIC_CACHE_TTL_S = int(os.getenv("CRAWLRX_SCRAPE_DIAGNOSTIC_TTL_S", "900"))
_DIAGNOSTIC_LOG_PATH = Path(env_path("CRAWLRX_SCRAPE_DIAGNOSTIC_LOG", "scrape_diagnostics.jsonl"))
_DIAGNOSTIC_CACHE: tuple[float, "ScrapeDiagnosticsResponse"] | None = None


class ScrapeDiagnosticCheck(BaseModel):
    name: str
    status: str
    detail: str
    elapsed_ms: int
    source: Optional[str] = None
    rendered: Optional[bool] = None
    result_count: int = 0
    expected_failure: bool = False


class ScrapeDiagnosticsResponse(BaseModel):
    status: str
    summary: dict[str, int]
    checks: list[ScrapeDiagnosticCheck]
    elapsed_ms: int
    cache_hit: bool = False
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


async def _scrape_call(**kwargs: Any) -> dict:
    base: dict[str, Any] = {
        "url": _DIAGNOSTIC_TARGET_URL,
        "selector": None,
        "selectors": None,
        "extract": "text",
        "wait_for": None,
        "timeout": 15000,
        "javascript": True,
        "user_agent": None,
        "headers": None,
        "scroll_to_bottom": False,
        "click_selector": None,
        "agent": None,
        "include_structured_data": False,
        "stealth": True,
        "js_inject": None,
        "js_inject_timeout": 5000,
        "wait_for_text": None,
        "wait_for_gone": None,
        "wait_for_timeout_extra": 0,
        "screenshot_selector": None,
        "shadow_selector": None,
    }
    base.update(kwargs)
    return await _do_scrape(**base)


def _first_text(raw: dict) -> str:
    results = raw.get("results") or []
    if not results:
        return ""
    first = results[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("text") or first.get("markdown") or first.get("html") or "")


async def _check_http_whole() -> tuple[dict, str]:
    raw = await _scrape_call(javascript=False)
    text = _first_text(raw)
    if not raw.get("success"):
        return raw, raw.get("error") or "HTTP whole-page scrape failed"
    if "Example Domain" not in text:
        return raw, "HTTP whole-page scrape missed expected text"
    if raw.get("rendered"):
        return raw, "HTTP whole-page scrape unexpectedly used browser render"
    return raw, "HTTP whole-page extraction green"


async def _check_http_selector() -> tuple[dict, str]:
    raw = await _scrape_call(selector="h1")
    text = _first_text(raw)
    if not raw.get("success"):
        return raw, raw.get("error") or "HTTP selector scrape failed"
    if text != "Example Domain":
        return raw, "HTTP selector returned unexpected text: %s" % text[:80]
    if raw.get("rendered"):
        return raw, "HTTP selector scrape unexpectedly used browser render"
    return raw, "HTTP CSS selector extraction green"


async def _check_browser_js() -> tuple[dict, str]:
    raw = await _scrape_call(
        js_inject="() => ({ title: document.title, bodyLen: document.body.innerText.length })",
    )
    js_result = raw.get("js_result") or {}
    if not raw.get("success"):
        return raw, raw.get("error") or "Browser scrape failed"
    if not raw.get("rendered"):
        return raw, "Browser JS check did not use rendered path"
    if js_result.get("title") != "Example Domain":
        return raw, "Browser JS returned unexpected title"
    return raw, "Browser JS extraction green"


async def _check_shadow_dom() -> tuple[dict, str]:
    raw = await _scrape_call(
        js_inject=(
            "() => { const host = document.createElement('div');"
            "host.id = 'crawlrx-shadow-proof';"
            "const root = host.attachShadow({mode: 'open'});"
            "root.innerHTML = '<span>Shadow OK</span>';"
            "document.body.appendChild(host);"
            "return {shadow: true}; }"
        ),
        shadow_selector="#crawlrx-shadow-proof",
    )
    if not raw.get("success"):
        return raw, raw.get("error") or "Shadow DOM scrape failed"
    if raw.get("shadow_content") != "Shadow OK":
        return raw, "Shadow DOM content was %r" % raw.get("shadow_content")
    return raw, "Shadow DOM extraction green"


async def _check_missing_selector_truth() -> tuple[dict, str]:
    raw = await _scrape_call(selector=".crawlrx-definitely-missing")
    if raw.get("success"):
        return raw, "Missing selector incorrectly reported success"
    error = str(raw.get("error") or "")
    if "Selector matched no content" not in error:
        return raw, "Missing selector returned wrong error: %s" % error
    return raw, "Missing selector truthfully failed"


async def _run_check(
    name: str,
    fn: Callable[[], Awaitable[tuple[dict, str]]],
    *,
    expected_failure: bool = False,
) -> ScrapeDiagnosticCheck:
    started = time.time()
    try:
        raw, detail = await fn()
        ok = bool(raw.get("success"))
        if expected_failure:
            ok = not ok and "truthfully failed" in detail
        elif detail.endswith("green"):
            ok = True
        else:
            ok = False
        return ScrapeDiagnosticCheck(
            name=name,
            status="ok" if ok else "fail",
            detail=detail,
            elapsed_ms=int((time.time() - started) * 1000),
            source=raw.get("source"),
            rendered=raw.get("rendered"),
            result_count=len(raw.get("results") or []),
            expected_failure=expected_failure,
        )
    except Exception as exc:  # diagnostics should report failures, not crash the app
        return ScrapeDiagnosticCheck(
            name=name,
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.time() - started) * 1000),
            expected_failure=expected_failure,
        )


def _summarize(checks: list[ScrapeDiagnosticCheck]) -> dict[str, int]:
    summary = {"total": len(checks), "ok": 0, "warn": 0, "fail": 0}
    for check in checks:
        key = check.status if check.status in summary else "warn"
        summary[key] += 1
    return summary


def _write_diagnostic_log(response: ScrapeDiagnosticsResponse) -> None:
    try:
        _DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = response.model_dump(mode="json")
        with _DIAGNOSTIC_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        return


def _all_checks_green(response: ScrapeDiagnosticsResponse) -> bool:
    total = int(response.summary.get("total", 0))
    return (
        response.status == "ok"
        and total > 0
        and int(response.summary.get("ok", 0)) == total
        and all(check.status == "ok" for check in response.checks)
    )


async def run_scrape_diagnostics(force: bool = False) -> ScrapeDiagnosticsResponse:
    """Run or return cached scrape quality diagnostics."""
    global _DIAGNOSTIC_CACHE
    if not force and _DIAGNOSTIC_CACHE is not None:
        cached_at, cached_response = _DIAGNOSTIC_CACHE
        if time.time() - cached_at <= _DIAGNOSTIC_CACHE_TTL_S:
            return cached_response.model_copy(update={"cache_hit": True})
    started = time.time()
    checks = [
        await _run_check("http-whole-page", _check_http_whole),
        await _run_check("http-css-selector", _check_http_selector),
        await _run_check("browser-js", _check_browser_js),
        await _run_check("shadow-dom", _check_shadow_dom),
        await _run_check(
            "missing-selector-truth",
            _check_missing_selector_truth,
            expected_failure=True,
        ),
    ]
    summary = _summarize(checks)
    status = "ok" if summary["fail"] == 0 else "fail"
    response = ScrapeDiagnosticsResponse(
        status=status,
        summary=summary,
        checks=checks,
        elapsed_ms=int((time.time() - started) * 1000),
    )
    if _all_checks_green(response):
        release_scrape_site_quarantine(
            _DIAGNOSTIC_TARGET_URL,
            source="scrape_diagnostics",
            reason="fresh_all_green",
        )
    _DIAGNOSTIC_CACHE = (time.time(), response)
    _write_diagnostic_log(response)
    return response


@router.get("/diagnostics", response_model=ScrapeDiagnosticsResponse)
async def scrape_diagnostics(force: bool = False) -> ScrapeDiagnosticsResponse:
    """Run high-signal scrape pipeline diagnostics."""
    return await run_scrape_diagnostics(force=force)


@router.get("/status", response_model=ScrapeDiagnosticsResponse)
async def scrape_status(force: bool = False) -> ScrapeDiagnosticsResponse:
    """Alias for /scrape/diagnostics used by monitors and dashboards."""
    return await run_scrape_diagnostics(force=force)


class SiteHealthReleaseRequest(BaseModel):
    url: str = Field(description="Any URL on the host whose quarantine should be released")


class SiteHealthReleaseResponse(BaseModel):
    domain: str
    released: bool
    source: str = "operator"
    reason: str = "manual_clearance"


@router.post("/site-health/release", response_model=SiteHealthReleaseResponse)
async def release_site_health(body: SiteHealthReleaseRequest) -> SiteHealthReleaseResponse:
    """Release a host's scrape quarantine after out-of-band clearance.

    Call this once the challenge has been solved manually (or the site is
    verified healthy again); the next scrape proceeds without waiting out the
    quarantine TTL.
    """
    from .scrape_site_health import _domain

    domain = _domain(body.url)
    if not domain:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=f"No usable domain in URL: {body.url!r}")
    release_scrape_site_quarantine(body.url, source="operator", reason="manual_clearance")
    return SiteHealthReleaseResponse(domain=domain, released=True)
