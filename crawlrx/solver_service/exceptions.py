"""
Custom exception classes for FlareSolverr.
2Captcha-compatible error codes and FastAPI exception handlers.
"""

from typing import Optional, Dict, Any
from fastapi import Request, FastAPI
from fastapi.responses import JSONResponse

def emit(*a: object, **kw: object) -> None: pass  # TODO: add logging



class FlareSolverrError(Exception):
    """Base exception for FlareSolverr."""
    
    def __init__(self, message: str, error_code: str = "ERROR_GENERIC", 
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to 2Captcha-compatible error response."""
        return {
            "status": 0,
            "error": self.__class__.__name__,
            "errorCode": self.error_code,
            "errorDescription": self.message,
            **self.details
        }


class SolverError(FlareSolverrError):
    """Generic solver error - task could not be completed."""
    
    def __init__(self, message: str = "Solver failed", 
                 solver: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            details=details
        )
        self.solver = solver


class SolverTimeout(FlareSolverrError):
    """Solver timed out waiting for solution."""
    
    def __init__(self, message: str = "Solver timeout", 
                 solver: Optional[str] = None,
                 timeout_seconds: Optional[float] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged_details = details or {}
        if solver:
            merged_details["solver"] = solver
        if timeout_seconds:
            merged_details["timeout_seconds"] = timeout_seconds
            
        super().__init__(
            message=message,
            error_code="ERROR_TIMEOUT",
            details=merged_details
        )
        self.solver = solver
        self.timeout_seconds = timeout_seconds


class SolverUnavailable(FlareSolverrError):
    """Solver service is unavailable or overloaded."""
    
    def __init__(self, message: str = "Solver unavailable", 
                 solver: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged_details = details or {}
        if solver:
            merged_details["solver"] = solver
            
        super().__init__(
            message=message,
            error_code="ERROR_NO_SLOT_AVAILABLE",
            details=merged_details
        )
        self.solver = solver


class InvalidTask(FlareSolverrError):
    """Task parameters are invalid or malformed."""
    
    def __init__(self, message: str = "Invalid task parameters", 
                 field: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged_details = details or {}
        if field:
            merged_details["field"] = field
            
        super().__init__(
            message=message,
            error_code="ERROR_BAD_PARAMETERS",
            details=merged_details
        )
        self.field = field


class AuthError(FlareSolverrError):
    """Authentication failed - bad or missing API key."""
    
    def __init__(self, message: str = "Authentication failed", 
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            error_code="ERROR_WRONG_USER_KEY",
            details=details
        )


class InsufficientFunds(FlareSolverrError):
    """Account has insufficient funds (2Captcha compatibility)."""
    
    def __init__(self, message: str = "Insufficient funds", 
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            error_code="ERROR_ZERO_BALANCE",
            details=details
        )


class TaskNotFound(FlareSolverrError):
    """Task ID not found (for result polling)."""
    
    def __init__(self, task_id: Optional[str] = None,
                 message: str = "Task not found",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged_details = details or {}
        if task_id:
            merged_details["task_id"] = task_id
            
        super().__init__(
            message=message,
            error_code="ERROR_CAPTCHA_NOT_READY",
            details=merged_details
        )
        self.task_id = task_id


class ProxyError(FlareSolverrError):
    """Proxy connection or authentication failed."""
    
    def __init__(self, message: str = "Proxy error",
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            error_code="ERROR_PROXY",
            details=details
        )


# FastAPI exception handlers
def flaresolverr_exception_handler(request: Request, exc: FlareSolverrError) -> JSONResponse:
    """Handle all FlareSolverr exceptions."""
    status_code = 400
    
    # Map specific errors to HTTP status codes
    if isinstance(exc, AuthError):
        status_code = 403
    elif isinstance(exc, SolverUnavailable):
        status_code = 503
    elif isinstance(exc, SolverTimeout):
        status_code = 504
    elif isinstance(exc, TaskNotFound):
        status_code = 404
    
    return JSONResponse(
        status_code=status_code,
        content=exc.to_dict()
    )


def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""
    return JSONResponse(
        status_code=500,
        content={
            "status": 0,
            "error": "INTERNAL_ERROR",
            "errorCode": "ERROR_GENERIC",
            "errorDescription": "An unexpected error occurred",
            "message": str(exc)
        }
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers with FastAPI app.
    
    Args:
        app: FastAPI application instance
    """
    # Register base handler for all FlareSolverr exceptions
    app.add_exception_handler(FlareSolverrError, flaresolverr_exception_handler)
    
    # Register specific handlers (order matters - most specific first)
    app.add_exception_handler(SolverError, flaresolverr_exception_handler)
    app.add_exception_handler(SolverTimeout, flaresolverr_exception_handler)
    app.add_exception_handler(SolverUnavailable, flaresolverr_exception_handler)
    app.add_exception_handler(InvalidTask, flaresolverr_exception_handler)
    app.add_exception_handler(AuthError, flaresolverr_exception_handler)
    app.add_exception_handler(InsufficientFunds, flaresolverr_exception_handler)
    app.add_exception_handler(TaskNotFound, flaresolverr_exception_handler)
    app.add_exception_handler(ProxyError, flaresolverr_exception_handler)
    
    # Catch-all for unexpected errors
    app.add_exception_handler(Exception, generic_exception_handler)


# Helper functions for raising common errors
def raise_if_missing(value: object, field: str, message: Optional[str] = None) -> None:
    """Raise InvalidTask if value is None or empty."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InvalidTask(
            message=message or f"Missing required field: {field}",
            field=field
        )


def raise_if_invalid_type(value: object, field: str, expected_type: type) -> None:
    """Raise InvalidTask if value is not of expected type."""
    if not isinstance(value, expected_type):
        raise InvalidTask(
            message=f"Field '{field}' must be of type {expected_type.__name__}",
            field=field,
            details={"expected": expected_type.__name__, "got": type(value).__name__}
        )
