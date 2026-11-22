"""captcha_handoff.py -- what happens when a page is blocked by a captcha.

``CRAWLRX_CAPTCHA_MODE`` picks the strategy:

* ``solve`` (default): ``crawlrx.solvers`` tries every usable solver first
  and falls back to the human hand-off below when none clears the page.
* ``handoff``: a human solves it, as below. No solver runs.

The human hand-off (``CRAWLRX_CAPTCHA_HANDOFF``):

* ``none`` (default; right for servers and Docker): the request returns
  ``error_type="captcha_required"`` plus a ``captcha`` block telling the
  caller which URL needs a human.
* ``browser``: CrawlRX opens a *visible* browser window and waits for you to
  solve the challenge yourself, then carries on with the same page.
  Enable with ``CRAWLRX_CAPTCHA_HANDOFF=browser`` (needs a desktop session).

Integrations (e.g. a browser extension or a UI) can plug in their own
strategy with ``set_handoff(my_handoff)``. Implement ``CaptchaHandoff``:
return ``True`` only once a human has cleared the challenge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("crawlrx.captcha_handoff")

_DEFAULT_WAIT_S = 180.0
_POLL_INTERVAL_S = 2.0

# Markers of common captcha widgets / bot interstitials (detection only).
CAPTCHA_MARKERS: dict[str, tuple[str, ...]] = {
    "recaptcha": ("g-recaptcha", "google.com/recaptcha", "grecaptcha"),
    "hcaptcha": ("h-captcha", "hcaptcha.com"),
    "turnstile": ("cf-turnstile", "challenges.cloudflare.com/turnstile"),
    "cloudflare": ("cf-browser-verification", "challenge-platform", "just a moment", "checking your browser"),
    "datadome": ("captcha.datadome.co", "dd-captcha"),
    "generic": ("captcha", "verify you are human", "are you a robot"),
}


@dataclass(frozen=True)
class CaptchaChallenge:
    url: str
    kind: str

    def as_dict(self, handoff: str) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "handoff": handoff,
            "action": (
                "A human needs to solve this challenge. Open the URL in your own browser, or run "
                "CrawlRX on a desktop with CRAWLRX_CAPTCHA_HANDOFF=browser to solve it in a visible "
                "window, then retry."
            ),
        }


def classify_challenge(html: str, title: str = "") -> str:
    """Best-effort label for a detected challenge ("generic" if unsure)."""
    haystack = (html[:20000] + " " + title).lower()
    for kind, markers in CAPTCHA_MARKERS.items():
        if any(marker in haystack for marker in markers):
            return kind
    return "generic"


@runtime_checkable
class CaptchaHandoff(Protocol):
    name: str

    @property
    def needs_visible_browser(self) -> bool: ...

    async def resolve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        """Return True once a human has cleared the challenge on ``page``."""
        ...


class NoHandoff:
    """Report the captcha to the caller; do not wait."""

    name = "none"

    @property
    def needs_visible_browser(self) -> bool:
        return False

    async def resolve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        logger.info("captcha (%s) at %s -- returned to caller for a human to solve", challenge.kind, challenge.url)
        return False


async def page_still_blocked(page: Any) -> bool:
    from ._utils_safety import detect_bot_block

    try:
        html = await page.content()
        title = await page.title()
    except Exception as exc:  # noqa: BLE001 -- page closed / navigating: treat as still blocked
        logger.debug("captcha poll read failed (%s); still waiting", type(exc).__name__)
        return True
    return detect_bot_block(html, title or "")


class VisibleBrowserHandoff:
    """Wait for the user to solve the challenge in the visible browser window."""

    name = "browser"

    def __init__(self, wait_s: float = _DEFAULT_WAIT_S, poll_s: float = _POLL_INTERVAL_S) -> None:
        self.wait_s = wait_s
        self.poll_s = poll_s

    @property
    def needs_visible_browser(self) -> bool:
        return True

    async def resolve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        logger.warning(
            "CAPTCHA (%s) at %s -- please solve it in the open browser window (waiting up to %.0fs)",
            challenge.kind, challenge.url, self.wait_s,
        )
        try:
            await page.bring_to_front()
        except Exception as exc:  # noqa: BLE001 -- cosmetic only
            logger.debug("bring_to_front failed: %s", exc)
        deadline = time.monotonic() + self.wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_s)
            if not await page_still_blocked(page):
                logger.info("captcha cleared by user at %s", challenge.url)
                return True
        logger.warning("captcha at %s not solved within %.0fs", challenge.url, self.wait_s)
        return False


def _wait_from_env() -> float:
    raw = os.getenv("CRAWLRX_CAPTCHA_WAIT", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_WAIT_S
    except ValueError:
        logger.warning("CRAWLRX_CAPTCHA_WAIT=%r is not a number; using %.0fs", raw, _DEFAULT_WAIT_S)
        return _DEFAULT_WAIT_S


_MODE_ALIASES = {"solve": "solve", "on": "solve", "handoff": "handoff", "off": "handoff", "none": "handoff"}


def captcha_mode() -> str:
    """``solve`` (default: solvers first, hand-off as fallback) or ``handoff`` (humans only)."""
    raw = os.getenv("CRAWLRX_CAPTCHA_MODE", "").strip().lower()
    if not raw:
        return "solve"
    mode = _MODE_ALIASES.get(raw)
    if mode is None:
        # A typo must never switch solving on against the user's intent.
        logger.warning("Unknown CRAWLRX_CAPTCHA_MODE=%r; valid values: solve, handoff. Using handoff.", raw)
        return "handoff"
    return mode


def _human_handoff_from_env() -> CaptchaHandoff:
    mode = os.getenv("CRAWLRX_CAPTCHA_HANDOFF", "none").strip().lower()
    if mode == "browser":
        return VisibleBrowserHandoff(wait_s=_wait_from_env())
    if mode not in ("", "none"):
        logger.warning("Unknown CRAWLRX_CAPTCHA_HANDOFF=%r; valid values: none, browser. Using none.", mode)
    return NoHandoff()


def handoff_from_env() -> CaptchaHandoff:
    """The configured strategy: the human hand-off, wrapped by solvers in ``solve`` mode."""
    human = _human_handoff_from_env()
    if captcha_mode() != "solve":
        return human
    from . import solvers  # optional module: only loaded when solving is switched on

    available = solvers.load_solvers()
    if not available:
        logger.warning(
            "Challenge solving is on but no solver is usable here; using the %s hand-off. "
            "Install ffmpeg for reCAPTCHA audio, set CRAWLRX_FLARESOLVERR_URL, or run "
            "`crawlrx doctor` for details. CRAWLRX_CAPTCHA_MODE=handoff silences this.", human.name)
        return human
    logger.info("challenge solving on: %s (fallback: %s)", ", ".join(s.name for s in available), human.name)
    return solvers.SolveThenHandoff(available, human)


_active: list[CaptchaHandoff] = []


def get_handoff() -> CaptchaHandoff:
    if not _active:
        _active.append(handoff_from_env())
    return _active[0]


def set_handoff(handoff: Optional[CaptchaHandoff]) -> None:
    """Install a custom strategy (``None`` resets to the environment default)."""
    if handoff is not None and not isinstance(handoff, CaptchaHandoff):
        raise TypeError("handoff must implement CaptchaHandoff (name, needs_visible_browser, async resolve)")
    _active[:] = [handoff] if handoff is not None else []


def handoff_wait_seconds() -> int:
    """Extra wall-clock time a request may spend waiting for a human."""
    handoff = get_handoff()
    max_wait = getattr(handoff, "max_wait_s", None)
    if max_wait is not None:
        return int(max_wait) + 5
    if not handoff.needs_visible_browser:
        return 0
    return int(getattr(handoff, "wait_s", _DEFAULT_WAIT_S)) + 5


def browser_headless() -> bool:
    """Headless unless the user asked for a visible browser."""
    if os.getenv("CRAWLRX_HEADLESS", "").strip().lower() in ("0", "false", "no"):
        return False
    return not get_handoff().needs_visible_browser


async def hand_off(page: Any, url: str, html: str, title: str = "") -> tuple[bool, dict[str, Any]]:
    """Run the active hand-off. Returns ``(cleared, captcha_info)``."""
    handoff = get_handoff()
    challenge = CaptchaChallenge(url=url, kind=classify_challenge(html, title))
    details: dict[str, Any] = {}
    try:
        detailed = getattr(handoff, "resolve_detailed", None)
        if detailed is not None:
            cleared, details = await detailed(page, challenge)
        else:
            cleared = await handoff.resolve(page, challenge)
    except Exception as exc:  # noqa: BLE001 -- a broken plug-in must not crash the scrape
        logger.error("captcha hand-off %r failed: %s: %s", handoff.name, type(exc).__name__, exc)
        cleared = False
    return cleared, {**challenge.as_dict(handoff.name), **details}


async def clear_or_flag(page: Any, url: str, html: str, title: str,
                        result: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Hand a blocked page to a human.

    Returns the refreshed ``(html, title)`` once cleared; otherwise fills
    ``result`` with ``error_type="captcha_required"`` + ``captcha`` and
    returns ``None`` so the caller stops.
    """
    cleared, info = await hand_off(page, url, html, title)
    if not cleared:
        result["error"] = blocked_error(info)
        result["error_type"] = "captcha_required"
        result["captcha"] = info
        return None
    return await page.content(), (await page.title() or "")


def blocked_error(info: dict[str, Any]) -> str:
    return (f"Captcha / bot challenge ({info.get('kind', 'generic')}) needs a human. "
            "See the 'captcha' field for how to hand it off.")


__all__ = [
    "CAPTCHA_MARKERS",
    "captcha_mode",
    "page_still_blocked",
    "CaptchaChallenge",
    "CaptchaHandoff",
    "NoHandoff",
    "VisibleBrowserHandoff",
    "blocked_error",
    "browser_headless",
    "classify_challenge",
    "clear_or_flag",
    "get_handoff",
    "hand_off",
    "handoff_from_env",
    "handoff_wait_seconds",
    "set_handoff",
]
