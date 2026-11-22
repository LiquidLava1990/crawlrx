"""
FastAPI application for self-hosted challenge resolution service.
2Captcha-compatible API endpoints with all solver modules integrated.
"""
import json as json_lib
import logging
from contextlib import asynccontextmanager

from .config import get_config, reload_config
from .models import (
    SubmitResponse, ResultResponse, BalanceResponse,
    ErrorResponse, StatsResponse, StatsData,
    HealthResponse, CaptchaMethod
)
from .task_queue import TaskQueue
from .solver_router import get_router
from .exceptions import register_exception_handlers
from ._main_solver_registration import register_all_solvers
from ._main_submit_helpers import (
    SubmitFormPayload,
    missing_param_for_cloudflare,
    missing_param_for_image_methods,
    missing_param_for_recaptcha_family,
)

# CRITICAL: Pre-import Go/C extensions BEFORE torch loads.
try:
    import tls_client  # noqa: F401
except ImportError:
    pass

# Sentinel error tracking
try:
    from .core.sentinel_errors import emit
except ImportError:
    def emit(*a: object, **kw: object) -> object:
        return None

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Global instances
task_queue: Optional[TaskQueue] = None


_ERROR_TEXT_MAP: Dict[str, str] = {
    'ERROR_WRONG_USER_KEY': 'Invalid API key format',
    'ERROR_KEY_DOES_NOT_EXIST': 'API key not found',
    'ERROR_ZERO_BALANCE': 'Insufficient balance',
    'ERROR_NO_SLOT_AVAILABLE': 'No solving slot available',
    'ERROR_CAPTCHA_UNSOLVABLE': 'CAPTCHA could not be solved',
    'ERROR_BAD_PARAMETERS': 'Missing or invalid parameters',
    'ERROR_TASK_ID_INVALID': 'Invalid task ID',
    'ERROR_INTERNAL': 'Internal server error',
    'ERROR_TIMEOUT': 'Request timed out',
    'ERROR_ALL_SOLVERS_FAILED': 'All available solvers failed',
    'ERROR_SOLVER_NOT_FOUND': 'Solver not found',
    'ERROR_SOLVER_NOT_AVAILABLE': 'Solver not available',
}


def verify_api_key(key: str) -> bool:
    """Verify API key if configured."""
    config = get_config()
    if not config.api_key:
        return True
    return key == config.api_key


def method_to_enum(method: str) -> Optional[CaptchaMethod]:
    """Convert string method to CaptchaMethod enum."""
    method_map = {
        'userrecaptcha': CaptchaMethod.USER_RECAPTCHA_V2,
        'recaptcha_v2': CaptchaMethod.RECAPTCHA_V2,
        'recaptcha_v2_audio': CaptchaMethod.RECAPTCHA_V2,
        'recaptcha_v2_grid': CaptchaMethod.RECAPTCHA_V2,
        'recaptcha_v3': CaptchaMethod.RECAPTCHA_V3,
        'hcaptcha': CaptchaMethod.HCAPTCHA,
        'cloudflare': CaptchaMethod.CLOUDFLARE,
        'turnstile': CaptchaMethod.CLOUDFLARE,
        'funcaptcha': CaptchaMethod.FUNCAPTCHA,
        'datadome': CaptchaMethod.DATADOME,
        'geetest': CaptchaMethod.GEETEST,
        'geetest_v4': CaptchaMethod.GEETEST,
        'imagecaptcha': CaptchaMethod.IMAGE_CAPTCHA,
        'image': CaptchaMethod.IMAGE,
        'base64': CaptchaMethod.BASE64,
        'text_captcha': CaptchaMethod.IMAGE,
        'stealth': CaptchaMethod.IMAGE,
    }
    return method_map.get(method.lower().strip())


def build_error_response(code: str, message: str, use_json: bool = False) -> object:
    """Build error response in 2Captcha format."""
    if use_json:
        return JSONResponse(
            content=ErrorResponse(request=code, error_text=_ERROR_TEXT_MAP.get(code, message)).model_dump(),
            status_code=400,
        )
    return PlainTextResponse(content=code, status_code=400)


@asynccontextmanager
async def lifespan(app: FastAPI) -> object:
    """Application lifespan manager."""
    global task_queue

    logger.info("Starting challenge resolution service...")
    config = get_config()

    task_queue = TaskQueue(
        ttl_seconds=config.task_ttl_seconds,
        cleanup_interval=config.cleanup_interval_seconds,
        max_concurrent=config.max_concurrent_tasks,
    )

    router = get_router()
    task_queue.set_processor(router.route)

    registered, failed = register_all_solvers()
    logger.info("Solver registration complete: %d registered, %d failed", len(registered), len(failed))

    await task_queue.start()
    logger.info("Service ready on %s:%s", config.host, config.port)

    yield

    logger.info("Shutting down...")
    await task_queue.stop()


# Create FastAPI app
app = FastAPI(
    title="FlareSolverr API",
    description="Self-hosted challenge resolution service with 2Captcha-compatible API",
    version="1.1.0",
    lifespan=lifespan,
)
register_exception_handlers(app)


@app.get("/health", response_model=HealthResponse)
async def health_check() -> object:
    """Health check endpoint."""
    router = get_router()
    solver_health = await router.health_check()
    return HealthResponse(status="healthy", solvers=solver_health)


def _build_detailed_solver(solver_info: Dict[str, Any], health_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a single solver's static info + live health snapshot."""
    return {
        'type': solver_info['type'],
        'enabled': solver_info['enabled'],
        'available': health_entry.get('healthy', False),
        'status_message': health_entry.get('message', 'No status'),
        'timeout': solver_info['timeout'],
        'has_fallback': solver_info['has_fallback'],
        'fallback_chain': solver_info['fallback_chain'],
        'details': health_entry.get('details', {}),
    }


@app.get("/solvers")
async def list_solvers(key: Optional[str] = None) -> object:
    """List all available solvers with health status + fallback chain info."""
    if key and not verify_api_key(key):
        return JSONResponse(content={"error": "Invalid API key"}, status_code=403)

    router = get_router()
    solvers_info = router.list_solvers()
    health = await router.health_check()

    detailed = [
        _build_detailed_solver(s, health.get(s['type'], {'healthy': False, 'message': 'Unknown'}))
        for s in solvers_info
    ]
    available = [s for s in detailed if s['available'] and s['enabled']]
    unavailable = [s for s in detailed if not s['available'] or not s['enabled']]

    return JSONResponse(content={
        'status': 'ok',
        'total_solvers': len(detailed),
        'available_count': len(available),
        'unavailable_count': len(unavailable),
        'solvers': detailed,
        'available': [s['type'] for s in available],
        'unavailable': [s['type'] for s in unavailable],
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'policy': 'No paid services - all solvers are local/self-hosted only',
    })


@app.get("/stats", response_model=StatsResponse)
def get_stats() -> object:
    """Get solving statistics."""
    global task_queue
    stats = task_queue.get_stats()
    stats_data = StatsData(
        total_submitted=stats['total_submitted'],
        total_solved=stats['total_solved'],
        total_failed=stats['total_failed'],
        success_rate=stats['success_rate'],
        avg_solve_time_ms=stats.get('avg_solve_time_ms'),
        by_type=stats.get('by_type', {}),
    )
    return StatsResponse(data=stats_data)


async def _parse_submit_form(request: Request) -> SubmitFormPayload:
    """Parse the multipart/form-data body into a `SubmitFormPayload`."""
    form = await request.form()
    method = form.get('method')
    if not isinstance(method, str) or not method:
        raise ValueError("Form field 'method' is required")
    raw_json = form.get('json', 0)
    try:
        json_flag = int(raw_json) if raw_json not in (None, "") else 0
    except (TypeError, ValueError):
        json_flag = 0
    return SubmitFormPayload(
        key=form.get('key'),
        method=method,
        googlekey=form.get('googlekey'),
        sitekey=form.get('sitekey'),
        pageurl=form.get('pageurl'),
        url=form.get('url'),
        body=form.get('body'),
        file=form.get('file'),
        json=json_flag,
        soft_id=form.get('soft_id'),
        gt=form.get('gt'),
        challenge=form.get('challenge'),
        api_server=form.get('api_server'),
        captcha_id=form.get('captcha_id'),
    )


def _auth_or_error(payload: SubmitFormPayload) -> Optional[Any]:
    """Return an error response if auth fails; else None to proceed."""
    config = get_config()
    if config.api_key and (not payload.key or not verify_api_key(payload.key)):
        return build_error_response(
            'ERROR_WRONG_USER_KEY',
            'Invalid or missing API key',
            use_json=payload.use_json,
        )
    return None


def _resolve_method_or_error(payload: SubmitFormPayload):
    """Resolve method-string -> CaptchaMethod, or return an error response."""
    method_enum = method_to_enum(payload.method)
    if method_enum is None:
        return None, build_error_response(
            'ERROR_BAD_PARAMETERS',
            (
                f'Unknown method: {payload.method}. Supported: userrecaptcha, recaptcha_v2, '
                'recaptcha_v2_audio, recaptcha_v2_grid, recaptcha_v3, hcaptcha, '
                'cloudflare, turnstile, funcaptcha, datadome, geetest, geetest_v4, imagecaptcha, image, base64'
            ),
            use_json=payload.use_json,
        )
    return method_enum, None


def _validate_required_params(
    method_enum: CaptchaMethod,
    method_hint: str,
    params: Dict[str, Any],
    use_json: bool,
) -> Optional[Any]:
    """Run all per-method param validators; return the first error response or None."""
    for missing in (
        missing_param_for_recaptcha_family(method_enum, method_hint, params),
        missing_param_for_cloudflare(method_enum, params),
        missing_param_for_image_methods(method_enum, params),
    ):
        if missing:
            return build_error_response(
                'ERROR_BAD_PARAMETERS',
                f'Missing required parameter: {missing}',
                use_json=use_json,
            )
    return None


@app.post("/in.php")
async def submit_captcha(request: Request) -> object:
    """Submit a CAPTCHA for solving (2Captcha-compatible)."""
    payload = await _parse_submit_form(request)

    auth_error = _auth_or_error(payload)
    if auth_error is not None:
        return auth_error

    method_enum, method_err = _resolve_method_or_error(payload)
    if method_err is not None:
        return method_err

    params = payload.to_solver_params()
    param_err = _validate_required_params(
        method_enum, payload.method_hint, params, payload.use_json,
    )
    if param_err is not None:
        return param_err

    task = task_queue.submit(method_enum, params)
    if payload.use_json:
        return JSONResponse(content=SubmitResponse(status=1, request=task.id).model_dump())
    return PlainTextResponse(content=f"OK|{task.id}")


@app.get("/res.php")
def get_result(key: str, action: str, id: str, json: int = 0) -> object:
    """Poll for challenge resolution result (2Captcha-compatible)."""
    if not verify_api_key(key):
        return build_error_response('ERROR_KEY_DOES_NOT_EXIST', 'Invalid API key', use_json=json == 1)
    if action.lower() != 'get':
        return build_error_response(
            'ERROR_BAD_PARAMETERS',
            'Invalid action. Use "get" to retrieve results.',
            use_json=json == 1,
        )
    status = task_queue.get_task_status(id)
    if not status:
        return build_error_response('ERROR_TASK_ID_INVALID', 'Task ID not found', use_json=json == 1)

    response_text = status['response']
    if status.get('method') == CaptchaMethod.GEETEST.value and response_text.startswith('OK|'):
        try:
            answer = json_lib.loads(response_text[3:])
        except (TypeError, ValueError):
            answer = None
        is_v4 = status.get('method_hint') == 'geetest_v4'
        fields = ('captcha_id', 'lot_number', 'pass_token', 'gen_time', 'captcha_output') if is_v4 else ('challenge', 'validate', 'seccode')
        if (not isinstance(answer, dict) or
                any(not isinstance(answer.get(name), str) or not answer[name].strip() for name in fields) or
                (is_v4 and answer.get('captcha_id') != status.get('captcha_id'))):
            return build_error_response('ERROR_CAPTCHA_UNSOLVABLE', 'Incomplete GeeTest response', use_json=json == 1)
        return JSONResponse(content=answer)
    if json != 1:
        return PlainTextResponse(content=response_text)
    if response_text.startswith('OK|'):
        return JSONResponse(content=ResultResponse(status=1, request=response_text[3:]).model_dump())
    return JSONResponse(content=ResultResponse(status=0, request=response_text).model_dump())


@app.get("/balance")
def get_balance(key: str, action: str = 'getbalance', json: int = 0) -> object:
    """Get account balance (2Captcha-compatible) -- stub: unlimited (0.0)."""
    if not verify_api_key(key):
        return build_error_response('ERROR_KEY_DOES_NOT_EXIST', 'Invalid API key', use_json=json == 1)
    if json == 1:
        return JSONResponse(content=BalanceResponse(request=0.0).model_dump())
    return PlainTextResponse(content="0.0")


@app.post("/reload")
def reload_configuration(key: str) -> object:
    """Reload configuration (admin endpoint)."""
    if not verify_api_key(key):
        return PlainTextResponse(content="ERROR_KEY_DOES_NOT_EXIST", status_code=403)
    reload_config()
    return PlainTextResponse(content="OK|CONFIG_RELOADED")


def main() -> None:
    """Run the server."""
    config = get_config()
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
