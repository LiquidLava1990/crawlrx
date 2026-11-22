"""Camoufox solver helpers: fingerprint fallback + Task/TaskResult shim + spec dataclasses.

Extracted from ``camoufox_solver.py`` so the orchestrator stays thin.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Camoufox fingerprint fallback ------------------------------------------------
_VIEWPORTS = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1280, 720)]
_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Europe/London", "Europe/Berlin",
]
_LOCALES = ["en-US", "en-GB", "de-DE", "fr-FR"]


def generate_fingerprint() -> Dict[str, Any]:
    """Generate a randomized browser fingerprint dict (Camoufox shape)."""
    w, h = random.choice(_VIEWPORTS)
    return {
        "screen": {"width": w, "height": h},
        "timezone": random.choice(_TIMEZONES),
        "locale": random.choice(_LOCALES),
    }


class FingerprintGeneratorFallback:
    """Minimal fallback for the ``camoufox.FingerprintGenerator`` class."""

    def generate(self) -> object:
        """Return one randomized fingerprint dict."""
        return generate_fingerprint()


# Task / TaskResult import shim -----------------------------------------------
try:
    from ..models import Task, TaskResult  # type: ignore because the FlareSolverr models package is optional at import time
except ImportError:
    from datetime import datetime as _dt
    from typing import Optional as _Opt

    from pydantic import BaseModel

    class TaskResult(BaseModel):
        """Minimal fallback shape when ``models`` is unavailable."""

        success: bool
        token: _Opt[str] = None
        error_code: _Opt[str] = None
        error_message: _Opt[str] = None
        solved_at: _Opt[_dt] = None
        solve_time_ms: _Opt[int] = None

    class Task(BaseModel):
        """Minimal fallback shape when ``models`` is unavailable."""

        id: str
        params: Dict[str, Any]


# Round-outcome + poll-loop shapes --------------------------------------------
@dataclass
class RoundOutcome:
    """Result of one CamoufoxSolver retry round."""

    result: Optional[TaskResult] = None
    error: Optional[Exception] = None


@dataclass
class PollLoopParams:
    """Bundle of parameters for the shared polling loop."""

    label: str
    initial_wait: float
    poll_interval: float
    max_polls: int
    check: Any
    timeout_message: str


@dataclass
class ChallengeSpec:
    """Polling parameters for a single challenge family (cookie-based)."""

    label: str
    initial_wait: float
    poll_interval: float
    max_polls: int
    cookie_name: str
    challenge_selectors: tuple = ()
    challenge_eval_js: Optional[str] = None
    error_eval_js: Optional[str] = None
    error_marker: str = ""


@dataclass
class TokenPollSpec:
    """Polling parameters for token-extraction handlers."""

    label: str
    initial_wait: float
    poll_interval: float
    max_polls: int
    token_js: str


CHALLENGE_SPECS: Dict[str, ChallengeSpec] = {
    "cloudflare": ChallengeSpec(
        label="Cloudflare",
        initial_wait=3,
        poll_interval=2,
        max_polls=30,
        cookie_name="cf_clearance",
        challenge_selectors=(
            "#cf-challenge-running",
            ".cf-browser-verification",
            'input[name="cf-turnstile-response"]',
            "#turnstile-container",
        ),
        error_eval_js=(
            "() => { const el = document.querySelector('.error-code, .cf-error-code');"
            " return el ? el.textContent : null; }"
        ),
        error_marker="100",
    ),
    "datadome": ChallengeSpec(
        label="DataDome",
        initial_wait=2,
        poll_interval=1,
        max_polls=20,
        cookie_name="datadome",
        challenge_eval_js=(
            "() => { return !!document.querySelector('[data-datadome]') ||"
            " document.title.includes('DataDome') ||"
            " document.body?.innerText?.includes('DataDome') ||"
            " !!document.querySelector('.datadome-captcha'); }"
        ),
    ),
}


TOKEN_POLL_SPECS: Dict[str, TokenPollSpec] = {
    "recaptcha_v2": TokenPollSpec(
        label="reCAPTCHA v2",
        initial_wait=2,
        poll_interval=2,
        max_polls=30,
        token_js="() => window.grecaptcha ? window.grecaptcha.getResponse() : null",
    ),
    "recaptcha_v3": TokenPollSpec(
        label="reCAPTCHA v3",
        initial_wait=2,
        poll_interval=2,
        max_polls=20,
        token_js="() => window.grecaptcha ? window.grecaptcha.getResponse() : null",
    ),
    "hcaptcha": TokenPollSpec(
        label="hCaptcha",
        initial_wait=2,
        poll_interval=2,
        max_polls=30,
        token_js="() => window.hcaptcha ? window.hcaptcha.getResponse() : null",
    ),
    "generic": TokenPollSpec(
        label="generic",
        initial_wait=2,
        poll_interval=1,
        max_polls=10,
        token_js="() => document.documentElement.outerHTML.length > 100 ? 'ok' : null",
    ),
}


__all__ = [
    "CHALLENGE_SPECS",
    "ChallengeSpec",
    "FingerprintGeneratorFallback",
    "PollLoopParams",
    "RoundOutcome",
    "TOKEN_POLL_SPECS",
    "Task",
    "TaskResult",
    "TokenPollSpec",
    "generate_fingerprint",
]
