"""Challenge solvers (on by default; ``CRAWLRX_CAPTCHA_MODE=handoff`` turns them off).

Each solver is tried for the
challenge kinds it supports; when none clears the page, the human hand-off
(``CRAWLRX_CAPTCHA_HANDOFF``) takes over. ``CRAWLRX_SOLVERS`` (comma list)
limits which solvers run; by default every usable one does.

Solvers here wrap open-source projects; see THIRD_PARTY_NOTICES.md.
No paid solving services are used and no API keys are bundled.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional

from .base import ANY_KIND, CaptchaSolver, SolverUnavailable, SolveThenHandoff
from .flaresolverr import FlareSolverrSolver
from .recaptcha_audio import RecaptchaAudioSolver
from .solver_service import SolverServiceSolver

logger = logging.getLogger("crawlrx.solvers")

SolverFactory = Callable[[], Optional[CaptchaSolver]]

# name -> factory returning a ready solver, or None when its service/package is missing.
# Tried in this order for each challenge kind they support.
SOLVER_FACTORIES: Dict[str, SolverFactory] = {
    "flaresolverr": FlareSolverrSolver.from_env,         # Cloudflare / Turnstile interstitials
    "recaptcha-audio": RecaptchaAudioSolver.from_env,    # reCAPTCHA v2 (needs ffmpeg)
    "service": SolverServiceSolver.from_env,             # any 2Captcha-compatible service you run
}


def _requested_names() -> List[str]:
    raw = os.getenv("CRAWLRX_SOLVERS", "").strip()
    if not raw:
        return list(SOLVER_FACTORIES)
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name not in SOLVER_FACTORIES]
    if unknown:
        logger.warning("Unknown solver(s) in CRAWLRX_SOLVERS: %s. Available: %s",
                       ", ".join(unknown), ", ".join(SOLVER_FACTORIES))
    return [name for name in names if name in SOLVER_FACTORIES]


def load_solvers() -> List[CaptchaSolver]:
    """Build every requested solver that is usable on this machine."""
    solvers: List[CaptchaSolver] = []
    for name in _requested_names():
        try:
            solver = SOLVER_FACTORIES[name]()
        except (ImportError, SolverUnavailable) as exc:
            logger.info("solver %s unavailable: %s", name, exc)
            continue
        if solver is None:
            logger.debug("solver %s not configured", name)
            continue
        solvers.append(solver)
    return solvers


def solver_status() -> List[Dict[str, str]]:
    """One row per known solver for `crawlrx doctor`."""
    rows: List[Dict[str, str]] = []
    for name, factory in SOLVER_FACTORIES.items():
        try:
            state = "configured" if factory() is not None else "not configured"
        except SolverUnavailable as exc:
            state = str(exc)
        except ImportError as exc:
            state = f"missing package ({exc.name or exc})"
        rows.append({"name": name, "state": state})
    return rows


__all__ = [
    "ANY_KIND",
    "CaptchaSolver",
    "FlareSolverrSolver",
    "RecaptchaAudioSolver",
    "SolverServiceSolver",
    "SOLVER_FACTORIES",
    "SolverUnavailable",
    "SolveThenHandoff",
    "load_solvers",
    "solver_status",
]
