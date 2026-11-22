"""Find captcha widgets on a page and hand a solved token back to it."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("crawlrx.solvers.tokens")

# Widget selector, response-field names and the 2Captcha-API method name per kind.
_WIDGETS = {
    "recaptcha": (".g-recaptcha[data-sitekey], [data-sitekey].g-recaptcha", ("g-recaptcha-response",), "userrecaptcha"),
    "hcaptcha": (".h-captcha[data-sitekey]", ("h-captcha-response", "g-recaptcha-response"), "hcaptcha"),
    "turnstile": (".cf-turnstile[data-sitekey]", ("cf-turnstile-response",), "turnstile"),
}

_FIND_SITEKEY_JS = """
(selector) => {
  const el = document.querySelector(selector);
  if (el) return el.getAttribute('data-sitekey');
  const frame = [...document.querySelectorAll('iframe[src*="k="], iframe[src*="sitekey="]')][0];
  if (!frame) return null;
  const url = new URL(frame.src, location.href);
  return url.searchParams.get('k') || url.searchParams.get('sitekey');
}
"""

# Writes the token into every matching response field, fires the widget's
# data-callback if it has one, and submits the enclosing form otherwise.
_INJECT_JS = """
([selector, fields, token]) => {
  for (const name of fields) {
    let nodes = document.querySelectorAll(`[name="${name}"], #${name}`);
    if (!nodes.length) {
      const input = document.createElement('textarea');
      input.name = name; input.style.display = 'none';
      (document.querySelector(selector) || document.body).appendChild(input);
      nodes = [input];
    }
    nodes.forEach((node) => { node.value = token; node.innerHTML = token; });
  }
  const widget = document.querySelector(selector);
  const callbackName = widget && widget.getAttribute('data-callback');
  if (callbackName && typeof window[callbackName] === 'function') {
    window[callbackName](token);
    return 'callback';
  }
  const form = widget && widget.closest('form');
  if (form) { form.submit(); return 'submitted'; }
  return 'injected';
}
"""


@dataclass(frozen=True)
class Widget:
    kind: str
    sitekey: str
    method: str


async def find_widget(page: Any, kind: str) -> Optional[Widget]:
    """The captcha widget of ``kind`` on the page, with its sitekey."""
    spec = _WIDGETS.get(kind)
    if spec is None:
        return None
    selector, _fields, method = spec
    sitekey = await page.evaluate(_FIND_SITEKEY_JS, selector)
    return Widget(kind=kind, sitekey=str(sitekey), method=method) if sitekey else None


async def submit_token(page: Any, kind: str, token: str) -> str:
    """Inject ``token`` and let the page continue; returns what was done."""
    selector, fields, _method = _WIDGETS[kind]
    outcome = await page.evaluate(_INJECT_JS, [selector, list(fields), token])
    if outcome == "submitted":
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001 -- the page may not navigate at all
            logger.debug("no navigation after token submit: %s", exc)
    return str(outcome)


def token_kinds() -> frozenset:
    return frozenset(_WIDGETS)
