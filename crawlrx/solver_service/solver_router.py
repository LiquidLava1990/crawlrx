"""
Solver router - routes CAPTCHA tasks to appropriate solver modules.
Updated with all new solvers: reCAPTCHA audio/grid, hCaptcha, Cloudflare,
Turnstile, FunCaptcha, DataDome, GeeTest, text CAPTCHA, stealth, TLS,
stealth (Botright engine), stealth_browser, and Camoufox.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type
import logging

from .models import Task, TaskResult, CaptchaMethod
from .solver_base import BaseSolver, SolverNotAvailableError, SolverValidationError
from .config import get_config
from ._solver_router_helpers import (
    RETRYABLE_ERROR_CODES,
    build_error_result,
    normalize_solver_result,
    safe_solver_health_check,
)

# Sentinel error tracking
try:
    from .core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self, name: str) -> str:
            return name

    ErrCode = _E()

    def emit(*a: object, **kw: object) -> None:
        return None

logger = logging.getLogger(__name__)


def _valid_geetest_result(task: Task, result: TaskResult) -> bool:
    """Only accept a complete response for the requested GeeTest widget."""
    if task.method != CaptchaMethod.GEETEST:
        return True
    try:
        answer = json.loads(result.token or '')
    except (TypeError, ValueError):
        return False
    is_v4 = task.params.get('method_hint') == 'geetest_v4'
    fields = ('captcha_id', 'lot_number', 'pass_token', 'gen_time', 'captcha_output') if is_v4 else ('challenge', 'validate', 'seccode')
    return (isinstance(answer, dict)
            and all(isinstance(answer.get(field), str) and answer[field].strip() for field in fields)
            and (not is_v4 or answer['captcha_id'] == task.params.get('captcha_id')))


# Concrete exception classes the router treats as recoverable solver errors.
_SOLVER_INVOCATION_ERRORS = (
    RuntimeError, OSError, ValueError, TypeError, KeyError, AttributeError, ImportError,
)
_FETCH_HTML_ERRORS = (OSError, asyncio.TimeoutError, RuntimeError)
_SOLVER_INSTANTIATE_ERRORS = (TypeError, ValueError, RuntimeError, OSError, ImportError)
_REGISTRATION_ERRORS = (ImportError, AttributeError, RuntimeError, OSError)


class SolverRouter:
    """Routes tasks to appropriate solvers based on CAPTCHA type with auto-detection and fallback chain."""

    METHOD_TO_SOLVER = {
        'userrecaptcha': 'recaptcha_v2_audio',
        'recaptcha_v2': 'recaptcha_v2_audio',
        'recaptcha_v2_audio': 'recaptcha_v2_audio',
        'recaptcha_v2_grid': 'recaptcha_v2_grid',
        'recaptcha_v3': 'recaptcha_v3',
        'hcaptcha': 'hcaptcha',
        'cloudflare': 'cloudflare',
        'turnstile': 'turnstile',
        'funcaptcha': 'funcaptcha',
        'datadome': 'datadome',
        'geetest': 'geetest',
        'imagecaptcha': 'text_captcha',
        'image': 'text_captcha',
        'base64': 'text_captcha',
        'text_captcha': 'text_captcha',
        'stealth': 'stealth',
        'stealth_browser': 'stealth_browser',
        'tls': 'tls',
        'camoufox': 'camoufox',
    }

    CAPTCHA_DETECTION_PATTERNS = {
        'recaptcha_v2_audio': [
            r'google\.com/recaptcha',
            r'g-recaptcha',
            r'recaptcha/api2',
            r'recaptcha.*audio',
        ],
        'recaptcha_v2_grid': [
            r'google\.com/recaptcha',
            r'g-recaptcha',
            r'recaptcha/api2',
            r'rc-imageselect',
        ],
        'recaptcha_v3': [
            r'recaptcha.*v3',
            r'g-recaptcha.*action',
            r'execute.*recaptcha',
        ],
        'hcaptcha': [
            r'hcaptcha\.com',
            r'data-hcaptcha-widget-id',
            r'h-captcha',
            r'hcaptcha-challenge',
        ],
        'cloudflare': [
            r'cloudflare',
            r'cf-turnstile',
            r'cf_chl',
            r'challenge-platform',
            r'cf_challenge',
        ],
        'turnstile': [
            r'turnstile',
            r'cf-turnstile',
            r'challenge.*cloudflare',
        ],
        'funcaptcha': [
            r'funcaptcha',
            r'arkoselabs',
            r'fc-challenge',
        ],
        'datadome': [
            r'datadome',
            r'dd-captcha',
        ],
        'geetest': [
            r'geetest',
            r'gt-captcha',
            r'gt_widget',
        ],
        'text_captcha': [
            r'captcha\.php',
            r'securimage',
            r'captcha.*image',
        ],
    }

    FALLBACK_CHAIN: Dict[str, List[str]] = {
        'recaptcha_v2_audio': ['stealth', 'camoufox'],
        'recaptcha_v2_grid': ['stealth', 'camoufox'],
        'recaptcha_v3': ['stealth', 'camoufox'],
        'hcaptcha': ['stealth', 'camoufox'],
        'cloudflare': ['stealth', 'camoufox'],
        'turnstile': ['stealth', 'camoufox'],
        'funcaptcha': ['stealth', 'camoufox'],
        'datadome': ['stealth', 'camoufox'],
        'geetest': ['stealth', 'camoufox'],
        'text_captcha': [],
        'stealth_browser': ['stealth', 'camoufox'],
        'tls': ['stealth'],
        'stealth': ['camoufox'],
        'camoufox': ['stealth'],
    }

    def __init__(self) -> None:
        self._solvers: Dict[str, BaseSolver] = {}
        self._solver_classes: Dict[str, Type[BaseSolver]] = {}
        self._fallback_enabled = True
        self._auto_detect_enabled = True

        self._register_placeholder_solvers()

    def _register_placeholder_solvers(self) -> None:
        """Register placeholder solvers that return graceful errors."""
        config = get_config()

        all_solver_types = [
            'recaptcha_v2_audio', 'recaptcha_v2_grid', 'recaptcha_v3',
            'hcaptcha', 'cloudflare', 'turnstile', 'funcaptcha',
            'datadome', 'geetest', 'text_captcha', 'stealth', 'tls',
            'stealth_browser', 'camoufox',
        ]

        for solver_type in all_solver_types:
            solver_config = config.get_solver_config(solver_type)
            self._solvers[solver_type] = PlaceholderSolver(solver_type, solver_config.dict())

    def register_solver(self, solver_type: str, solver: BaseSolver) -> None:
        """Register a solver instance for a specific type."""
        self._solvers[solver_type] = solver
        logger.info("Registered solver: %s", solver_type)

    def register_solver_class(
        self,
        solver_type: str,
        solver_class: Type[BaseSolver],
    ) -> None:
        """Register a solver class to be instantiated."""
        self._solver_classes[solver_type] = solver_class
        config = get_config()
        solver_config = config.get_solver_config(solver_type)

        try:
            solver = solver_class(solver_config.dict())
        except _SOLVER_INSTANTIATE_ERRORS as e:
            logger.error("Failed to instantiate solver %s: %s", solver_type, e)
            return None  # explicit recovery: non-fatal: _SOLVER_INSTANTIATE_ERRORS is recorded but does not abort the caller
        self.register_solver(solver_type, solver)

    def get_solver_type(self, method: CaptchaMethod) -> Optional[str]:
        """Map CAPTCHA method to solver type."""
        return self.METHOD_TO_SOLVER.get(method.value)

    def detect_captcha_type(self, html_content: str) -> Optional[str]:
        """Auto-detect CAPTCHA type from HTML content."""
        if not html_content:
            return None

        html_lower = html_content.lower()
        scores: Dict[str, int] = {}

        for solver_type, patterns in self.CAPTCHA_DETECTION_PATTERNS.items():
            score = 0
            for pattern in patterns:
                if re.search(pattern, html_lower, re.IGNORECASE):
                    score += 1
            if score > 0:
                scores[solver_type] = score

        if not scores:
            return None

        detected = max(scores, key=scores.get)
        logger.info("Auto-detected CAPTCHA type: %s (score: %d)", detected, scores[detected])
        return detected

    async def route(self, task: Task) -> TaskResult:
        """Route a task to the appropriate solver with fallback chain."""
        start_time = time.time()
        solver_type = await self._resolve_solver_type(task)
        if not solver_type:
            logger.warning("Unknown method: %s", task.method.value)
            return build_error_result(
                "ERROR_METHOD_NOT_SUPPORTED",
                f"CAPTCHA method '{task.method.value}' is not supported",
                start_time,
            )
        return await self._run_chain(task, solver_type, start_time)

    async def _resolve_solver_type(self, task: Task) -> Optional[str]:
        """Pick a solver type by method, falling back to HTML auto-detection."""
        hint = task.params.get('method_hint', '')
        solver_type = self.METHOD_TO_SOLVER.get(hint) if isinstance(hint, str) else None
        solver_type = solver_type or self.get_solver_type(task.method)
        if solver_type or not self._auto_detect_enabled:
            return solver_type
        page_url = task.params.get('pageurl')
        if not page_url:
            return None
        html_content = await self._fetch_page_html(page_url)
        detected = self.detect_captcha_type(html_content)
        if detected:
            logger.info("Using auto-detected solver: %s", detected)
        return detected

    async def _run_chain(self, task: Task, solver_type: str, start_time: float) -> TaskResult:
        """Try `solver_type` then its fallback chain; return first non-retryable result."""
        attempted: List[str] = []
        chain = [solver_type] + self.FALLBACK_CHAIN.get(solver_type, [])
        for current in chain:
            if current in attempted:
                continue
            attempted.append(current)
            result = await self._try_solver(
                task, current, start_time, is_fallback=len(attempted) > 1,
            )
            if result.success and not _valid_geetest_result(task, result):
                logger.warning("Solver %s returned an incomplete GeeTest response", current)
                result = build_error_result('ERROR_CAPTCHA_UNSOLVABLE', 'Incomplete GeeTest response', start_time)
            if result.success:
                return result
            if result.error_code in RETRYABLE_ERROR_CODES:
                logger.warning("Solver %s failed, trying fallback...", current)
                continue
            return result
        return build_error_result(
            "ERROR_ALL_SOLVERS_FAILED",
            f"All solvers failed. Attempted: {', '.join(attempted)}. "
            "No paid services - only local solvers used.",
            start_time,
        )

    async def _try_solver(
        self,
        task: Task,
        solver_type: str,
        start_time: float,
        is_fallback: bool = False,
    ) -> TaskResult:
        """Run a single solver attempt; convert errors to retryable TaskResult."""
        solver = self._solvers.get(solver_type)
        if not solver:
            logger.warning("No solver registered for type: %s", solver_type)
            return build_error_result(
                "ERROR_SOLVER_NOT_FOUND",
                f"Solver for '{solver_type}' not found",
                start_time,
            )
        availability = await self._check_availability(solver, solver_type, start_time)
        if availability is not None:
            return availability
        return await self._invoke_solver(solver, task, solver_type, start_time)

    async def _check_availability(
        self,
        solver: BaseSolver,
        solver_type: str,
        start_time: float,
    ) -> Optional[TaskResult]:
        """Return a TaskResult if `solver` is unavailable; else None to proceed."""
        try:
            is_available = await solver.is_available()
        except SolverNotAvailableError as e:
            return build_error_result(e.code, e.message, start_time)
        if is_available:
            return None
        if solver_type not in get_config().enabled_solvers:
            return build_error_result(
                "ERROR_SOLVER_NOT_AVAILABLE",
                f"Solver '{solver_type}' is not enabled",
                start_time,
            )
        return build_error_result(
            "ERROR_SOLVER_UNHEALTHY",
            f"Solver '{solver_type}' is enabled but not healthy",
            start_time,
        )

    async def _invoke_solver(
        self,
        solver: BaseSolver,
        task: Task,
        solver_type: str,
        start_time: float,
    ) -> TaskResult:
        """Invoke `solver.solve` with timeout and normalize the return shape."""
        try:
            solve_call = solver.solve(task)
            if asyncio.iscoroutine(solve_call):
                raw_result = await asyncio.wait_for(solve_call, timeout=solver.timeout)
            else:
                raw_result = solve_call
        except asyncio.TimeoutError:
            logger.error("Solver %s timed out for task %s", solver_type, task.id)
            emit(ErrCode.BROWSER_TIMEOUT, f"Solver {solver_type} timed out")
            return build_error_result(
                "ERROR_TIMEOUT",
                f"Solver timed out after {solver.timeout}s",
                start_time,
            )
        except SolverValidationError as e:
            logger.warning("Validation error for task %s: %s", task.id, e.message)
            return build_error_result(e.code, e.message, start_time)
        except _SOLVER_INVOCATION_ERRORS as e:
            logger.exception("Unexpected error in solver %s: %s", solver_type, e)
            emit(ErrCode.CAPTCHA_DETECTED, f"Solver error: {solver_type}: {e}")
            return build_error_result("ERROR_INTERNAL", str(e), start_time)
        result = normalize_solver_result(raw_result)
        if result.solve_time_ms is None:
            result.solve_time_ms = int((time.time() - start_time) * 1000)
        return result

    async def _fetch_page_html(self, url: str) -> str:
        """Fetch page HTML for auto-detection."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0',
                })
                return response.text
        except _FETCH_HTML_ERRORS as e:
            logger.warning("Failed to fetch page for auto-detection: %s", e)
            emit(ErrCode.BROWSER_TIMEOUT, f"Page fetch failed: {e}")
            return ""

    async def health_check(self) -> Dict[str, Any]:
        """Check health of all solvers."""
        health: Dict[str, Any] = {}
        for solver_type, solver in self._solvers.items():
            health[solver_type] = await safe_solver_health_check(solver)
        return health

    def list_solvers(self) -> List[Dict[str, Any]]:
        """List all registered solvers and their status."""
        config = get_config()
        result = []
        for solver_type in self._solvers:
            solver_config = config.get_solver_config(solver_type)
            result.append({
                'type': solver_type,
                'enabled': solver_config.enabled,
                'timeout': solver_config.timeout,
                'has_fallback': len(self.FALLBACK_CHAIN.get(solver_type, [])) > 0,
                'fallback_chain': self.FALLBACK_CHAIN.get(solver_type, []),
            })
        return result

    def get_solver_info(self, solver_type: str) -> Optional[Dict[str, Any]]:
        """Get detailed info about a specific solver."""
        config = get_config()
        solver = self._solvers.get(solver_type)
        if not solver:
            return None

        return {
            'type': solver_type,
            'enabled': solver_type in config.enabled_solvers,
            'timeout': solver.timeout if hasattr(solver, 'timeout') else None,
            'fallback_chain': self.FALLBACK_CHAIN.get(solver_type, []),
            'is_placeholder': isinstance(solver, PlaceholderSolver),
        }


class PlaceholderSolver(BaseSolver):
    """Placeholder solver that returns graceful errors."""

    @property
    def solver_type(self) -> str:
        return self._solver_type

    def __init__(self, solver_type: str, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._solver_type = solver_type

    def solve(self, task: Task) -> TaskResult:
        """Return a graceful error indicating solver not loaded."""
        return TaskResult(
            success=False,
            error_code="ERROR_SOLVER_NOT_LOADED",
            error_message=(
                f"Solver '{self._solver_type}' module not yet built. "
                "This solver will be implemented by another agent."
            ),
            solved_at=datetime.now(tz=timezone.utc),
        )

    def health_check(self) -> Dict[str, Any]:
        """Return unhealthy status."""
        return {
            'healthy': False,
            'message': f"Placeholder solver - module for '{self._solver_type}' not loaded",
        }


# Global router instance
_router: Optional[SolverRouter] = None


def get_router() -> SolverRouter:
    """Get or create global router instance."""
    global _router
    if _router is None:
        _router = SolverRouter()
    return _router


def reset_router() -> None:
    """Reset the global router (useful for testing)."""
    global _router
    _router = None


# =============================================================================
# Real Solver Registration
# =============================================================================

# Lazy registration -- called explicitly from lifespan, NOT at import time.
