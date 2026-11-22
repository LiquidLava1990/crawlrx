"""Solver contract and the solve-then-hand-off chain."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, FrozenSet, List, Protocol, Sequence, Tuple, runtime_checkable

from ..captcha_handoff import CaptchaChallenge, CaptchaHandoff

logger = logging.getLogger("crawlrx.solvers")

ANY_KIND = "*"


class SolverUnavailable(Exception):
    """Raised by a solver factory that cannot run here; the message says what is missing."""


@runtime_checkable
class CaptchaSolver(Protocol):
    """Clears one family of challenges on an open Playwright page.

    ``kinds`` uses the labels from ``captcha_handoff.classify_challenge``
    (cloudflare, turnstile, recaptcha, hcaptcha, datadome, generic) or ``"*"``.
    ``solve`` returns True only when the page is no longer blocked.
    """

    name: str
    kinds: FrozenSet[str]
    timeout_s: float

    async def solve(self, page: Any, challenge: CaptchaChallenge) -> bool: ...


class SolveThenHandoff:
    """A ``CaptchaHandoff`` that tries the solvers first, then hands off to a human."""

    def __init__(self, solvers: Sequence[CaptchaSolver], fallback: CaptchaHandoff) -> None:
        self.solvers: List[CaptchaSolver] = list(solvers)
        self.fallback = fallback
        self.name = f"solve+{fallback.name}"

    @property
    def needs_visible_browser(self) -> bool:
        return self.fallback.needs_visible_browser

    @property
    def max_wait_s(self) -> float:
        """Longest time one challenge can take: every solver, then the fallback."""
        solver_time = sum(solver.timeout_s for solver in self.solvers)
        fallback_time = getattr(self.fallback, "wait_s", 0.0) if self.fallback.needs_visible_browser else 0.0
        return solver_time + fallback_time

    def _candidates(self, kind: str) -> List[CaptchaSolver]:
        return [s for s in self.solvers if kind in s.kinds or ANY_KIND in s.kinds]

    async def _try(self, solver: CaptchaSolver, page: Any, challenge: CaptchaChallenge) -> bool:
        try:
            return bool(await asyncio.wait_for(solver.solve(page, challenge), timeout=solver.timeout_s))
        except asyncio.TimeoutError:
            logger.warning("solver %s timed out after %.0fs on %s", solver.name, solver.timeout_s, challenge.url)
        except Exception as exc:  # noqa: BLE001 -- a broken solver must not stop the fallback
            logger.warning("solver %s failed on %s: %s: %s", solver.name, challenge.url, type(exc).__name__, exc)
        return False

    async def resolve_detailed(self, page: Any, challenge: CaptchaChallenge) -> Tuple[bool, Dict[str, Any]]:
        """Solve or hand off; returns ``(cleared, details)``. Holds no shared state."""
        tried: List[str] = []
        for solver in self._candidates(challenge.kind):
            tried.append(solver.name)
            if await self._try(solver, page, challenge):
                logger.info("captcha (%s) at %s solved by %s", challenge.kind, challenge.url, solver.name)
                return True, {"solved_by": solver.name, "solvers_tried": tried}
        if tried:
            logger.info("no solver cleared %s (%s); handing off via %s",
                        challenge.url, ", ".join(tried), self.fallback.name)
        cleared = await self.fallback.resolve(page, challenge)
        return cleared, {"solved_by": None, "solvers_tried": tried}

    async def resolve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        cleared, _details = await self.resolve_detailed(page, challenge)
        return cleared
