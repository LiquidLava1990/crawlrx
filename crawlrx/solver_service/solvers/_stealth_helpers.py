"""Botright solver helpers — JS payloads, dataclasses, type tables.

Split out of ``stealth_solver.py`` so the orchestrator stays under the
500-LOC ceiling. This file is import-cheap (stdlib + dataclasses only)
and carries every constant the strategies / browser modules consume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Tunables. Mirrors the constants the original god-file exposed; keeping the
# names + values prevents behavioural drift across the split.
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT: int = 120  # seconds
MAX_RETRIES: int = 3
MAX_SESSIONS: int = 3  # concurrent browser sessions


@dataclass
class _SolveOutcome:
    """Result of one StealthSolver solve attempt.

    Exactly one of ``token`` / ``error`` is set per attempt; the
    ``captcha_type`` field carries the (possibly newly-detected) captcha
    type forward into the next retry.
    """

    token: Optional[str] = None
    error: Optional[Exception] = None
    captcha_type: Optional[str] = None


# Mapping from a ``Task.method.value`` string to the canonical captcha-type
# string the strategy code uses for token-extractor lookup.
_METHOD_CAPTCHA_TYPE: Dict[str, str] = {
    "recaptcha_v3": "recaptcha_v3",
    "hcaptcha": "hcaptcha",
    "cloudflare": "cloudflare",
}


# ---------------------------------------------------------------------------
# JavaScript token-extraction payloads. Defined as module-level constants so
# the strategy code stays focused on Python control-flow.
# ---------------------------------------------------------------------------
_RECAPTCHA_TOKEN_JS: str = """
    () => {
        if (window.grecaptcha && window.grecaptcha.getResponse) {
            return window.grecaptcha.getResponse();
        }
        const textarea = document.querySelector('#g-recaptcha-response');
        if (textarea && textarea.value) return textarea.value;
        const widget = document.querySelector('.g-recaptcha');
        if (widget) return widget.getAttribute('data-response');
        return null;
    }
"""


_HCAPTCHA_TOKEN_JS: str = """
    () => {
        if (window.hcaptcha && window.hcaptcha.getResponse) {
            return window.hcaptcha.getResponse();
        }
        const textarea = document.querySelector('textarea[name="h-captcha-response"]');
        if (textarea && textarea.value) return textarea.value;
        const widget = document.querySelector('[data-hcaptcha-widget-id]');
        if (widget) return widget.getAttribute('data-hcaptcha-response');
        return null;
    }
"""


_TURNSTILE_TOKEN_JS: str = """
    () => {
        if (window.turnstile && window.turnstile.getResponse) {
            return window.turnstile.getResponse();
        }
        const input = document.querySelector('input[name="cf-turnstile-response"]');
        if (input && input.value) return input.value;
        const widget = document.querySelector('.cf-turnstile');
        if (widget) return widget.getAttribute('data-response');
        const turnstile = document.querySelector('[data-turnstile-sitekey]');
        if (turnstile) {
            const response = turnstile.querySelector('input');
            if (response && response.value) return response.value;
        }
        return null;
    }
"""


_GENERIC_TOKEN_JS: str = """
    () => {
        const selectors = [
            'textarea[name*="captcha-response"]',
            'textarea[name*="g-recaptcha-response"]',
            'textarea[name*="h-captcha-response"]',
            'input[name*="cf-turnstile-response"]',
            'input[name*="captcha"]',
            '[data-captcha-response]'
        ];
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el && el.value && el.value.length > 20) {
                return el.value;
            }
        }
        return null;
    }
"""


_DETECT_CAPTCHA_JS: str = """
    () => {
        const result = { type: null, selectors: [] };

        if (document.querySelector('.g-recaptcha') ||
            document.querySelector('[data-sitekey]') ||
            window.grecaptcha) {
            result.type = 'recaptcha_v2';
            result.selectors.push('g-recaptcha');
        }

        if (document.querySelector('[data-hcaptcha-widget-id]') ||
            document.querySelector('.h-captcha') ||
            document.querySelector('iframe[src*="hcaptcha.com"]') ||
            window.hcaptcha) {
            result.type = 'hcaptcha';
            result.selectors.push('h-captcha');
        }

        if (document.querySelector('.cf-turnstile') ||
            document.querySelector('[data-turnstile-sitekey]') ||
            document.querySelector('iframe[src*="challenges.cloudflare"]') ||
            document.querySelector('iframe[src*="turnstile"]') ||
            window.turnstile) {
            result.type = 'cloudflare';
            result.selectors.push('turnstile');
        }

        return result;
    }
"""


# Maps captcha-type strings to the JS extractor used to fetch a token.
_TOKEN_EXTRACTORS: Dict[str, str] = {
    "recaptcha_v2": _RECAPTCHA_TOKEN_JS,
    "recaptcha_v3": _RECAPTCHA_TOKEN_JS,
    "userrecaptcha": _RECAPTCHA_TOKEN_JS,
    "hcaptcha": _HCAPTCHA_TOKEN_JS,
    "cloudflare": _TURNSTILE_TOKEN_JS,
}


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_RETRIES",
    "MAX_SESSIONS",
    "_SolveOutcome",
    "_METHOD_CAPTCHA_TYPE",
    "_TOKEN_EXTRACTORS",
    "_GENERIC_TOKEN_JS",
    "_DETECT_CAPTCHA_JS",
]
