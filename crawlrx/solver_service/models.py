"""
Pydantic models for challenge resolution service.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

try:
    from .core.sentinel_errors import emit  # noqa: F401
except ImportError:
    def emit(*a: object, **kw: object) -> None: pass  # noqa: F811



class TaskState(str, Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    PROCESSING = "processing"
    SOLVED = "solved"
    FAILED = "failed"


class CaptchaMethod(str, Enum):
    """Supported CAPTCHA methods matching 2Captcha API."""
    USER_RECAPTCHA_V2 = "userrecaptcha"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    FUNCAPTCHA = "funcaptcha"
    DATADOME = "datadome"
    GEETEST = "geetest"
    CLOUDFLARE = "cloudflare"
    IMAGE_CAPTCHA = "imagecaptcha"
    IMAGE = "image"
    BASE64 = "base64"


class TaskResult(BaseModel):
    """Result of a challenge resolution attempt."""
    success: bool
    token: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    solved_at: Optional[datetime] = None
    solve_time_ms: Optional[int] = None


class Task(BaseModel):
    """Internal task representation."""
    id: str
    state: TaskState = TaskState.PENDING
    method: CaptchaMethod
    params: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[TaskResult] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0
    
    def to_2captcha_response(self) -> str:
        """Convert task result to 2Captcha-compatible response format."""
        if self.state == TaskState.SOLVED and self.result and self.result.success:
            return f"OK|{self.result.token}"
        elif self.state == TaskState.PROCESSING or self.state == TaskState.PENDING:
            return "CAPCHA_NOT_READY"
        elif self.state == TaskState.FAILED and self.result:
            return self.result.error_code or "ERROR_CAPTCHA_UNSOLVABLE"
        return "ERROR_CAPTCHA_UNSOLVABLE"


# Response Models

class SubmitResponse(BaseModel):
    """2Captcha-compatible submission response."""
    status: int = Field(..., description="1 = success, 0 = error")
    request: str = Field(..., description="Task ID or error code")
    error_text: Optional[str] = Field(None, description="Human-readable error")


class ResultResponse(BaseModel):
    """2Captcha-compatible result response."""
    status: int = Field(..., description="1 = success, 0 = not ready/error")
    request: str = Field(..., description="Token or status code")
    error_text: Optional[str] = Field(None, description="Human-readable error")


class BalanceResponse(BaseModel):
    """2Captcha-compatible balance response."""
    status: int = 1
    request: float = Field(..., description="Account balance")


class ErrorResponse(BaseModel):
    """Error response format."""
    status: int = 0
    request: str
    error_text: str


class StatsData(BaseModel):
    """Statistics data model."""
    total_submitted: int = 0
    total_solved: int = 0
    total_failed: int = 0
    success_rate: float = 0.0
    avg_solve_time_ms: Optional[float] = None
    by_type: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    version: str = "1.0.0"
    solvers: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class StatsResponse(BaseModel):
    """Stats endpoint response."""
    status: str = "ok"
    data: StatsData
    timestamp: datetime = Field(default_factory=datetime.utcnow)
