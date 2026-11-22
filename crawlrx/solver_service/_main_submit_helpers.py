"""Helpers for `main.submit_captcha` — payload struct + validators (split for size).

`SubmitFormPayload` collapses the 14 form fields into a single dataclass so
the route handler hits the cc.shape.param_count threshold and the body stays
under the god-function ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .models import CaptchaMethod


_REQUIRED_KEY_METHODS = (
    CaptchaMethod.USER_RECAPTCHA_V2,
    CaptchaMethod.RECAPTCHA_V2,
    CaptchaMethod.RECAPTCHA_V3,
    CaptchaMethod.HCAPTCHA,
)
_IMAGE_BODY_METHODS = (
    CaptchaMethod.IMAGE_CAPTCHA,
    CaptchaMethod.IMAGE,
    CaptchaMethod.BASE64,
)


@dataclass
class SubmitFormPayload:
    """All `/in.php` form fields normalized to a single struct."""
    key: Optional[str] = None
    method: str = ""
    googlekey: Optional[str] = None
    sitekey: Optional[str] = None
    pageurl: Optional[str] = None
    url: Optional[str] = None
    body: Optional[str] = None
    file: Optional[str] = None
    json: int = 0
    soft_id: Optional[str] = None
    gt: Optional[str] = None
    challenge: Optional[str] = None
    api_server: Optional[str] = None
    captcha_id: Optional[str] = None

    @property
    def use_json(self) -> bool:
        """True iff the client requested JSON-shaped responses."""
        return self.json == 1

    @property
    def method_hint(self) -> str:
        """Lower-cased + stripped `method` form field for routing."""
        return self.method.lower().strip()

    def to_solver_params(self) -> Dict[str, Any]:
        """Build the solver-facing params dict from the form payload."""
        params: Dict[str, Any] = {
            'sitekey': self.googlekey or self.sitekey,
            'pageurl': self.pageurl or self.url,  # url is alias for pageurl
            'body': self.body or self.file,
            'method_hint': self.method_hint,
            'gt': self.gt,
            'challenge': self.challenge,
            'api_server': self.api_server,
            'captcha_id': self.captcha_id,
        }
        return {k: v for k, v in params.items() if v is not None}


_GEETEST_REQUIRED_KEYS: tuple[tuple[str, str], ...] = (
    ('gt', 'gt'),
    ('pageurl', 'pageurl'),
)
_RECAPTCHA_REQUIRED_KEYS: tuple[tuple[str, str], ...] = (
    ('sitekey', 'googlekey/sitekey'),
    ('pageurl', 'pageurl'),
)


def _first_missing_key(
    params: Dict[str, Any], required: tuple[tuple[str, str], ...],
) -> Optional[str]:
    """Return the missing-key label for the first absent required field."""
    for key, label in required:
        if not params.get(key):
            return label
    return None


def missing_param_for_recaptcha_family(
    method_enum: CaptchaMethod,
    method_hint: str,
    params: Dict[str, Any],
) -> Optional[str]:
    """Return the missing param name (or None) for recaptcha-family methods."""
    if method_enum == CaptchaMethod.GEETEST:
        required = (('captcha_id', 'captcha_id'), ('pageurl', 'pageurl')) if method_hint == 'geetest_v4' else _GEETEST_REQUIRED_KEYS
    elif method_enum in (CaptchaMethod.FUNCAPTCHA, CaptchaMethod.DATADOME):
        required = (('pageurl', 'pageurl/url'),)
    elif method_enum in _REQUIRED_KEY_METHODS:
        required = _RECAPTCHA_REQUIRED_KEYS
    else:
        return None
    return _first_missing_key(params, required)


def missing_param_for_cloudflare(method_enum: CaptchaMethod, params: Dict[str, Any]) -> Optional[str]:
    """Return missing-param name for Cloudflare/Turnstile methods, or None."""
    if method_enum != CaptchaMethod.CLOUDFLARE:
        return None
    if not params.get('pageurl'):
        return 'pageurl/url'
    return None


def missing_param_for_image_methods(method_enum: CaptchaMethod, params: Dict[str, Any]) -> Optional[str]:
    """Return missing-param name for image/base64 methods, or None."""
    if method_enum not in _IMAGE_BODY_METHODS:
        return None
    if not params.get('body'):
        return 'body/file (base64 image)'
    return None
