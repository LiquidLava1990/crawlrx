"""FlareSolverr integration module for Cloudflare challenge resolution.



This module provides a production-grade interface to FlareSolverr,

a self-hosted challenge resolution service that bypasses Cloudflare challenges.



Features:

- Session pooling (max 5 concurrent sessions)

- Automatic retry with exponential backoff

- Health check monitoring

- Cloudflare clearance cookie extraction

- Timeout handling

"""



try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

import asyncio

import logging

import time

from dataclasses import dataclass

from typing import Dict, List, Optional, Any

from urllib.parse import urljoin



import aiohttp



from ..solver_base import BaseSolver
from ._solver_helpers import BROAD_EXC as _BROAD_EXC  # narrow safety-net catch tuple


from ..models import Task, TaskResult

from dataclasses import dataclass as _dataclass, field as _field





# Legacy types used by this solver (predates Task/TaskResult standardization)



@_dataclass

class CaptchaTask:

    """Adapter: converts from Task to the shape flaresolverr expects."""

    url: str = ""

    method: str = "get"

    max_timeout: int = 60000

    proxy: dict = _field(default_factory=dict)

    cookies: list = _field(default_factory=list)

    headers: dict = _field(default_factory=dict)

    data: str = ""



    @classmethod

    def from_task(cls, task: Task) -> "CaptchaTask":

        p = task.params

        return cls(

            url=p.get("pageurl", ""),

            method=p.get("method", "get"),

            max_timeout=int(p.get("timeout", 60)) * 1000,

            proxy={"url": p["proxy"]} if p.get("proxy") else {},

            cookies=p.get("cookies", []),

            headers=p.get("headers", {}),

            data=p.get("data", ""),

        )





@_dataclass

class SolveResult:

    """Adapter: captures flaresolverr output, convertible to TaskResult."""

    success: bool = False

    status: str = ""

    cookies: list = _field(default_factory=list)

    user_agent: str = ""

    response: str = ""

    headers: dict = _field(default_factory=dict)

    message: str = ""

    start_timestamp: int = 0

    end_timestamp: int = 0

    version: str = ""



    def to_task_result(self) -> TaskResult:

        solve_ms = self.end_timestamp - self.start_timestamp if self.end_timestamp else None

        if self.success:

            import json

            token_data = json.dumps({

                "cookies": self.cookies,

                "user_agent": self.user_agent,

                "headers": self.headers,

            })

            return TaskResult(success=True, token=token_data, solve_time_ms=solve_ms)

        return TaskResult(

            success=False,

            error_code="ERROR_FLARESOLVERR",

            error_message=self.message,

            solve_time_ms=solve_ms,

        )





# Configure module logger

logger = logging.getLogger(__name__)

logger.addHandler(logging.NullHandler())





# Constants

DEFAULT_BASE_URL = "http://localhost:8191/v1"  # TODO: use service_registry

DEFAULT_TIMEOUT = 60  # seconds

MAX_RETRIES = 3

MAX_SESSIONS = 5

BACKOFF_BASE = 2  # seconds





@dataclass

class SessionInfo:

    """Represents an active FlareSolverr session."""

    session_id: str

    created_at: float

    last_used: float

    in_use: bool = False





class FlareSolverrError(Exception):

    """Base exception for FlareSolverr errors."""

    pass





class FlareSolverrTimeoutError(FlareSolverrError):

    """Raised when a request times out."""

    pass





class FlareSolverrConnectionError(FlareSolverrError):

    """Raised when connection to FlareSolverr fails."""

    pass





class FlareSolverrSolver(BaseSolver):

    """FlareSolverr integration for Cloudflare challenge resolution."""



    @property

    def solver_type(self) -> str:

        return "cloudflare"



    def __init__(self, config: object=None) -> None:

        if config is None:

            config = {"enabled": True, "timeout": 60}

        super().__init__(config)

        self.base_url = config.get("base_url", DEFAULT_BASE_URL).rstrip("/")

        self.max_sessions = config.get("max_sessions", MAX_SESSIONS)

        self.default_timeout = config.get("default_timeout", DEFAULT_TIMEOUT)

        

        # Session management

        self._sessions: Dict[str, SessionInfo] = {}

        self._session_lock = asyncio.Lock()

        self._session_semaphore = asyncio.Semaphore(self.max_sessions)

        

        # HTTP client

        self._http_session: Optional[aiohttp.ClientSession] = None

        

        logger.info("FlareSolverrSolver initialized: %s, max_sessions=%s", self.base_url, self.max_sessions)



    def _get_http_session(self) -> aiohttp.ClientSession:

        """Get or create the HTTP session."""

        if self._http_session is None or self._http_session.closed:

            self._http_session = aiohttp.ClientSession(

                timeout=aiohttp.ClientTimeout(total=self.default_timeout + 10),

                headers={"Content-Type": "application/json"}

            )

        return self._http_session



    async def _close_http_session(self) -> None:

        """Close the HTTP session."""

        if self._http_session and not self._http_session.closed:

            await self._http_session.close()

            self._http_session = None



    async def _make_request(

        self,

        endpoint: str,

        payload: Dict[str, Any],

        timeout: Optional[int] = None

    ) -> Dict[str, Any]:

        """Make a request to FlareSolverr API with retry logic.

        

        Args:

            endpoint: API endpoint path

            payload: Request payload

            timeout: Request timeout in seconds

            

        Returns:

            JSON response from FlareSolverr

            

        Raises:

            FlareSolverrConnectionError: If connection fails

            FlareSolverrTimeoutError: If request times out

            FlareSolverrError: For other API errors

        """

        url = urljoin(self.base_url + "/", endpoint.lstrip("/"))
        session = await self._get_http_session()
        timeout_val = timeout or self.default_timeout
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await self._perform_request(
                    session, url, payload, timeout_val, attempt,
                )
            except (asyncio.TimeoutError, aiohttp.ClientError, FlareSolverrError) as exc:
                last_error = self._classify_attempt_error(exc, timeout_val, attempt)
            await self._sleep_before_retry(attempt)

        raise last_error or FlareSolverrError("Max retries exceeded")

    async def _perform_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: Dict[str, Any],
        timeout_val: int,
        attempt: int,
    ) -> Dict[str, Any]:
        """Run one HTTP attempt; raise on transport / API errors."""
        logger.debug("Request to %s (attempt %s/%s)", url, attempt, MAX_RETRIES)
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_val),
        ) as response:
            if response.status == 503:
                raise FlareSolverrError("Service unavailable (503)")
            response.raise_for_status()
            data = await response.json()
            if data.get("status") == "error":
                msg = data.get("message", "Unknown error")
                raise FlareSolverrError(f"FlareSolverr error: {msg}")
            return data

    def _classify_attempt_error(
        self,
        exc: Exception,
        timeout_val: int,
        attempt: int,
    ) -> Exception:
        """Translate a transport-layer exception into a FlareSolverr error."""
        if isinstance(exc, asyncio.TimeoutError):
            emit(ErrCode.TIMEOUT_ERROR, str(exc))
            logger.warning("Request timeout (attempt %s/%s)", attempt, MAX_RETRIES)
            return FlareSolverrTimeoutError(f"Timeout after {timeout_val}s")
        if isinstance(exc, aiohttp.ClientError):
            emit(ErrCode.NETWORK_ERROR, str(exc))
            logger.warning("Connection error (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            return FlareSolverrConnectionError(f"Connection error: {exc}")
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        logger.warning("API error (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
        return exc

    async def _sleep_before_retry(self, attempt: int) -> None:
        """Exponential backoff between retry attempts (no-op on last attempt)."""
        if attempt >= MAX_RETRIES:
            return
        delay = BACKOFF_BASE ** attempt
        logger.debug("Retrying in %ss...", delay)
        await asyncio.sleep(delay)



    async def _create_session(self) -> str:

        """Create a new FlareSolverr session.

        

        Returns:

            Session ID string

        """

        payload = {"cmd": "sessions.create"}

        response = await self._make_request("", payload)

        

        session_id = response.get("session")

        if not session_id:

            raise FlareSolverrError("Failed to create session: no session ID returned")

        

        now = time.time()

        self._sessions[session_id] = SessionInfo(

            session_id=session_id,

            created_at=now,

            last_used=now,

            in_use=True

        )

        

        logger.info("Created session: %s", session_id)

        return session_id



    async def _destroy_session(self, session_id: str) -> None:

        """Destroy a FlareSolverr session.

        

        Args:

            session_id: Session ID to destroy

        """

        if session_id not in self._sessions:

            return

        

        payload = {"cmd": "sessions.destroy", "session": session_id}

        

        try:

            await self._make_request("", payload)

            logger.info("Destroyed session: %s", session_id)

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.warning("Failed to destroy session %s: %s", session_id, e)
            return None  # explicit recovery: non-fatal: _BROAD_EXC is recorded but does not abort the caller

        finally:

            self._sessions.pop(session_id, None)



    async def _list_sessions(self) -> List[str]:

        """List active FlareSolverr sessions.

        

        Returns:

            List of session IDs

        """

        payload = {"cmd": "sessions.list"}

        response = await self._make_request("", payload)

        

        sessions = response.get("sessions", [])

        logger.debug("Listed %s sessions", len(sessions))

        return sessions



    async def _acquire_session(self) -> str:

        """Acquire an available session from the pool.

        

        Returns:

            Session ID to use

        """

        async with self._session_lock:

            # Try to find an available existing session

            for session_id, info in self._sessions.items():

                if not info.in_use:

                    info.in_use = True

                    info.last_used = time.time()

                    logger.debug("Reusing session: %s", session_id)

                    return session_id

            

            # Check if we can create a new session

            if len(self._sessions) < self.max_sessions:

                return await self._create_session()

        

        # Wait for a session to become available

        async with self._session_semaphore:

            async with self._session_lock:

                for session_id, info in self._sessions.items():

                    if not info.in_use:

                        info.in_use = True

                        info.last_used = time.time()

                        logger.debug("Reusing session after wait: %s", session_id)

                        return session_id

                

                # Should not reach here if semaphore is working correctly

                raise FlareSolverrError("Failed to acquire session")



    async def _release_session(self, session_id: str) -> None:

        """Release a session back to the pool.

        

        Args:

            session_id: Session ID to release

        """

        async with self._session_lock:

            if session_id in self._sessions:

                self._sessions[session_id].in_use = False

                logger.debug("Released session: %s", session_id)



    async def _cleanup_stale_sessions(self, max_age: int = 300) -> None:

        """Clean up sessions that haven't been used recently.

        

        Args:

            max_age: Maximum age in seconds before cleanup (default: 5 minutes)

        """

        now = time.time()

        sessions_to_remove: List[str] = []

        

        async with self._session_lock:

            for session_id, info in self._sessions.items():

                if not info.in_use and (now - info.last_used) > max_age:

                    sessions_to_remove.append(session_id)

        

        for session_id in sessions_to_remove:

            await self._destroy_session(session_id)

            logger.info("Cleaned up stale session: %s", session_id)



    def _extract_cf_clearance(self, cookies: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:

        """Extract Cloudflare clearance cookie from cookie list.

        

        Args:

            cookies: List of cookies from response

            

        Returns:

            cf_clearance cookie dict if found, None otherwise

        """

        for cookie in cookies:

            if cookie.get("name") == "cf_clearance":

                return cookie

        return None



    def health_check(self) -> Dict[str, Any]:
        """Check if FlareSolverr module is ready (service runs separately)."""
        return {
            "healthy": True,
            "message": (
                "FlareSolverr module ready. Service must be started separately at "
                + self.base_url
            ),
            "service_url": self.base_url,
            "note": "FlareSolverr is a self-hosted service. Start it with Docker or binary.",
        }



    async def solve(self, task: object) -> SolveResult:

        """Solve a Cloudflare CAPTCHA challenge for the given URL.

        

        This method acquires a session from the pool, makes the request

        through FlareSolverr, and extracts the clearance cookies.

        

        Args:

            task: The CAPTCHA task containing URL and options

            

        Returns:

            SolveResult with cookies, status, and response data

        """

        # Convert Task to CaptchaTask if needed
        if isinstance(task, Task):
            task = CaptchaTask.from_task(task)

        start_time = int(time.time() * 1000)
        session_id: Optional[str] = None

        try:
            session_id = await self._acquire_session()
            payload = self._build_solve_payload(task, session_id)
            logger.info("Solving %s %s", task.method.upper(), task.url)
            response = await self._make_request(
                "", payload, timeout=task.max_timeout + 10
            )
            return self._build_solve_result(response, start_time)
        except FlareSolverrTimeoutError as e:
            return self._failure_result(
                task, e, start_time, (ErrCode.TIMEOUT_ERROR, "Timeout"),
            )
        except FlareSolverrConnectionError as e:
            return self._failure_result(
                task, e, start_time, (ErrCode.NETWORK_ERROR, "Connection error"),
            )
        except _BROAD_EXC as e:
            return self._failure_result(
                task, e, start_time, (ErrCode.TOOL_EXECUTION_FAILED, "Error"),
            )
        finally:
            if session_id:
                await self._release_session(session_id)

    def _build_solve_payload(
        self, task: "CaptchaTask", session_id: str,
    ) -> Dict[str, Any]:
        """Compose the FlareSolverr request payload from a CaptchaTask."""
        payload: Dict[str, Any] = {
            "cmd": f"request.{task.method.lower()}",
            "url": task.url,
            "session": session_id,
            "maxTimeout": task.max_timeout * 1000,  # Convert to ms
        }
        if task.headers:
            payload["headers"] = task.headers
        if task.data:
            payload["postData"] = (
                task.data if isinstance(task.data, str) else str(task.data)
            )
        if task.cookies:
            payload["cookies"] = task.cookies
        if task.proxy:
            payload["proxy"] = task.proxy
        return payload

    def _build_solve_result(
        self, response: Dict[str, Any], start_time: int,
    ) -> "SolveResult":
        """Project a FlareSolverr response into a SolveResult."""
        end_time = int(time.time() * 1000)
        solution = response.get("solution", {})
        cookies = solution.get("cookies", [])
        cf_cookie = self._extract_cf_clearance(cookies)
        if cf_cookie:
            preview = cf_cookie.get("value", "")[:20]
            logger.info("Got cf_clearance cookie: %s...", preview)
        else:
            logger.warning("No cf_clearance cookie in response")
        return SolveResult(
            success=True,
            status="ok",
            cookies=cookies,
            user_agent=solution.get("userAgent"),
            response=solution.get("response"),
            headers=solution.get("headers", {}),
            message="Cloudflare challenge solved successfully",
            start_timestamp=start_time,
            end_timestamp=end_time,
            version=response.get("version"),
        )

    def _failure_result(
        self,
        task: "CaptchaTask",
        exc: BaseException,
        start_time: int,
        kind: "tuple[str, str]",
    ) -> "SolveResult":
        """Emit telemetry + build a failure SolveResult for a solve() exception."""
        err_code, prefix = kind
        emit(err_code, str(exc))
        logger.error("%s solving %s: %s", prefix, task.url, exc)
        return SolveResult(
            success=False,
            status="error",
            cookies=[],
            message=f"{prefix}: {str(exc)}",
            start_timestamp=start_time,
            end_timestamp=int(time.time() * 1000),
        )



    async def close(self) -> None:

        """Close all sessions and cleanup resources."""

        logger.info("Closing FlareSolverrSolver...")

        

        # Destroy all sessions

        session_ids = list(self._sessions.keys())

        for session_id in session_ids:

            await self._destroy_session(session_id)

        

        # Close HTTP session

        await self._close_http_session()

        

        logger.info("FlareSolverrSolver closed")



    def __aenter__(self) -> object:

        """Async context manager entry."""

        return self



    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:

        """Async context manager exit."""

        await self.close()

