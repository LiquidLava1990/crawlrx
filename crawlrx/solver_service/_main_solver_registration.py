"""Solver-module registration helpers for `main.py` (split for size + reuse).

`register_all_solvers` walks the table of solver modules, imports each, and
registers it on the global router. Failures are partitioned into
`(registered, failed)` lists so the caller can log a single summary.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from .config import get_config
from .solver_router import get_router

logger = logging.getLogger(__name__)

# Cached (registered, failed) outcome of the first register_all_solvers call.
_REGISTRATION_RESULT: Optional[Tuple[List[str], List[Tuple[str, str]]]] = None


# (registry_key, module_path, class_name) — table-driven so the loop body
# stays one screen of code regardless of how many solvers we add later.
# Registry keys: 'stealth' is the Botright-engine StealthSolver;
# 'stealth_browser' is the fingerprint-stealth StealthBrowserSolver.
SOLVER_MODULES: Tuple[Tuple[str, str, str], ...] = (
    ('recaptcha_v2_audio', 'crawlrx.solver_service.solvers.recaptcha_audio_solver', 'ReCaptchaAudioSolver'),
    ('recaptcha_v2_grid', 'crawlrx.solver_service.solvers.recaptcha_grid_solver', 'ReCaptchaGridSolver'),
    ('recaptcha_v3', 'crawlrx.solver_service.solvers.recaptcha_v3_solver', 'ReCaptchaV3Solver'),
    ('hcaptcha', 'crawlrx.solver_service.solvers.hcaptcha_challenge', 'HcaptchaChallengeHandler'),
    ('cloudflare', 'crawlrx.solver_service.solvers.flaresolverr_solver', 'FlareSolverrSolver'),
    ('geetest', 'crawlrx.solver_service.solvers.geetest_solver', 'GeeTestSolver'),
    ('text_captcha', 'crawlrx.solver_service.solvers.text_challenge', 'TextChallengeHandler'),
    ('stealth_browser', 'crawlrx.solver_service.solvers.stealth_browser', 'StealthBrowserSolver'),
    ('turnstile', 'crawlrx.solver_service.solvers.turnstile_solver', 'TurnstileSolver'),
    ('tls', 'crawlrx.solver_service.solvers.tls_solver', 'TLSSolver'),
    ('pydoll', 'crawlrx.solver_service.solvers.pydoll_solver', 'PydollSolver'),
    ('drissionpage', 'crawlrx.solver_service.solvers.drissionpage_solver', 'DrissionPageSolver'),
    ('headlessx', 'crawlrx.solver_service.solvers.headlessx_solver', 'HeadlessXSolver'),
    ('curl-impersonate', 'crawlrx.solver_service.solvers.curl_impersonate_solver', 'CurlImpersonateSolver'),
    ('stealth', 'crawlrx.solver_service.solvers.stealth_solver', 'StealthSolver'),
    ('camoufox', 'crawlrx.solver_service.solvers.camoufox_solver', 'CamoufoxSolver'),
    ('datadome', 'crawlrx.solver_service.solvers.datadome_solver', 'DataDomeSolver'),
    ('funcaptcha', 'crawlrx.solver_service.solvers.funcaptcha_challenge', 'FuncaptchaChallengeHandler'),
)


def _patch_torch_env() -> None:
    """Prevent torch's redirect module from crashing on Windows pipes."""
    os.environ["TORCHELASTIC_ERROR_FILE"] = ""
    os.environ["TORCH_DISABLE_REDIRECTS"] = "1"


def _try_register(solver_type: str, module_path: str, class_name: str) -> Tuple[bool, str]:
    """Attempt to import + register a single solver. Returns (ok, detail)."""
    try:
        module = __import__(module_path, fromlist=[class_name])
        solver_class = getattr(module, class_name)
        solver_config = get_config().get_solver_config(solver_type)
        solver = solver_class(solver_config.dict())
        get_router().register_solver(solver_type, solver)
        return True, ""
    except ImportError as e:
        return False, f"Missing deps: {e}"
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError) as e:
        return False, str(e)


def _apply_botright_patches() -> None:
    """Apply engine-compat patches before registration; never fatal."""
    from .botright_patches import apply_all_patches

    try:
        apply_all_patches()
    except Exception as e:  # noqa: BLE001 - patch failure must not abort registration
        logger.warning("botright_patches.apply_all_patches failed: %s", e)


def register_all_solvers() -> Tuple[List[str], List[Tuple[str, str]]]:
    """Register all available solver modules on startup (idempotent).

    Repeat calls return the first registration outcome without re-instantiating
    solvers, so lifespan restarts and test setup can call this freely.
    """
    global _REGISTRATION_RESULT
    if _REGISTRATION_RESULT is not None:
        return _REGISTRATION_RESULT

    _patch_torch_env()
    _apply_botright_patches()
    registered: List[str] = []
    failed: List[Tuple[str, str]] = []
    for solver_type, module_path, class_name in SOLVER_MODULES:
        logger.debug("Loading solver: %s...", solver_type)
        ok, detail = _try_register(solver_type, module_path, class_name)
        if ok:
            registered.append(solver_type)
            logger.info("Registered solver: %s", solver_type)
        else:
            failed.append((solver_type, detail))
            logger.warning("Solver '%s' registration failed: %s", solver_type, detail)
    logger.info("Registered %d solvers: %s", len(registered), registered)
    if failed:
        logger.warning("Failed to register %d solvers: %s", len(failed), [f[0] for f in failed])
    _REGISTRATION_RESULT = (registered, failed)
    return _REGISTRATION_RESULT
