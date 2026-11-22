"""
Base solver interface for challenge resolution service.
All solver modules must implement this interface.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict
from .models import Task, TaskResult

# Sentinel error tracking
try:
    from .core.sentinel_errors import emit  # noqa: F401
except ImportError:
    def emit(*a: object, **kw: object) -> None: pass  # noqa: F811  # TODO: add logging


class BaseSolver(ABC):
    """Abstract base class for all challenge handlers."""
    
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.enabled = config.get('enabled', True)
        self.timeout = config.get('timeout', 120)
    
    @property
    @abstractmethod
    def solver_type(self) -> str:
        """Return the solver type identifier (e.g., 'recaptcha_v2', 'hcaptcha')."""
        pass
    
    @abstractmethod
    def solve(self, task: Task) -> TaskResult:
        """
        Solve a CAPTCHA task.
        
        Args:
            task: The Task object containing all necessary data
            
        Returns:
            TaskResult with success/failure status and token/error
        """
        pass
    
    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """
        Return solver health status.
        
        Returns:
            Dict with 'healthy' (bool) and optional 'message' or 'details'
        """
        pass
    
    async def is_available(self) -> bool:
        """Check if solver is enabled and healthy."""
        if not self.enabled:
            return False
        import asyncio
        result = self.health_check()
        if asyncio.iscoroutine(result):
            health = await result
        else:
            health = result
        return health.get('healthy', False)


class SolverError(Exception):
    """Base exception for solver errors."""
    
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SolverTimeoutError(SolverError):
    """Raised when solver exceeds timeout."""
    
    def __init__(self, timeout: int) -> None:
        super().__init__("ERROR_TIMEOUT", f"Solver timed out after {timeout}s")


class SolverNotAvailableError(SolverError):
    """Raised when solver is not available/disabled."""
    
    def __init__(self, solver_type: str) -> None:
        super().__init__("ERROR_SOLVER_NOT_AVAILABLE", f"Solver '{solver_type}' is not available")


class SolverValidationError(SolverError):
    """Raised when task validation fails."""
    
    def __init__(self, message: str) -> None:
        super().__init__("ERROR_BAD_PARAMETERS", message)
