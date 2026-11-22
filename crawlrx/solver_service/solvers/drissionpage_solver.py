"""

DrissionPage-based challenge handler.

Unified browser automation with ChromiumPage/SessionPage modes.

"""

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

import asyncio

import time

from datetime import datetime, timezone

from typing import Any, Dict, Optional

from DrissionPage import ChromiumPage, ChromiumOptions, SessionPage

from ..solver_base import BaseSolver
from ._solver_helpers import BROAD_EXC as _BROAD_EXC  # narrow safety-net catch tuple

from ..models import Task, TaskResult, CaptchaMethod

_DRISSIONPAGE_IFRAME_SELECTORS = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]',
    'iframe[src*="challenge"]',
    ".g-recaptcha iframe",
    ".h-captcha iframe",
)

class DrissionPageSolver(BaseSolver):

    """

    challenge handler using DrissionPage browser automation framework.

    

    Features:

    - ChromiumPage mode for JS-rendered CAPTCHAs

    - SessionPage mode for lightweight HTTP challenges

    - Auto-switch between modes based on CAPTCHA detection

    - Multi-provider support (reCAPTCHA, hCaptcha, Turnstile, DataDome, GeeTest)

    - Elegant iframe handling and token extraction

    """

    

    # CAPTCHA detection patterns

    CAPTCHA_PATTERNS = {

        'recaptcha_v2': [

            'g-recaptcha',

            'google.com/recaptcha',

            'grecaptcha.render',

            'data-sitekey',

            'g-recaptcha-response'

        ],

        'recaptcha_v3': [

            'recaptcha_v3',

            'grecaptcha.execute',

            'g-recaptcha-response'

        ],

        'hcaptcha': [

            'h-captcha',

            'hcaptcha.com',

            'data-hcaptcha-sitekey',

            'h-captcha-response',

            'hcaptcha-challenge'

        ],

        'turnstile': [

            'turnstile',

            'cloudflare-turnstile',

            'cf-turnstile',

            'data-cf-turnstile-sitekey',

            'cf-turnstile-response'

        ],

        'datadome': [

            'datadome',

            'dd-capcha',

            'data-dom-protection',

            'dd_cookie'

        ],

        'geetest': [

            'geetest',

            'gt-captcha',

            'initGeetest',

            'gt_challenge',

            'gt_validate'

        ]

    }

    

    # Token extraction selectors

    TOKEN_SELECTORS = {

        'recaptcha': [

            'textarea#g-recaptcha-response',

            'input[name="g-recaptcha-response"]',

            'textarea[name="g-recaptcha-response"]'

        ],

        'hcaptcha': [

            'textarea#h-captcha-response',

            'input[name="h-captcha-response"]',

            'textarea[name="h-captcha-response"]'

        ],

        'turnstile': [

            'input[name="cf-turnstile-response"]',

            'textarea#cf-turnstile-response'

        ]

    }

    

    def __init__(self, config: Dict[str, Any]) -> None:

        super().__init__(config)

        self.max_retries = config.get('max_retries', 3)

        self.headless = config.get('headless', True)

        self.proxy = config.get('proxy')

        self.user_agent = config.get('user_agent')

        self.chromium_path = config.get('chromium_path')

        self._page: Optional[Any] = None

        self._mode: Optional[str] = None

    

    @property

    def solver_type(self) -> str:

        return "drissionpage"

    

    def _detect_captcha_type(self, page_source: str) -> Optional[str]:

        """Detect CAPTCHA type from page source."""

        page_lower = page_source.lower()

        scores = {}

        

        for captcha_type, patterns in self.CAPTCHA_PATTERNS.items():

            score = sum(1 for pattern in patterns if pattern.lower() in page_lower)

            if score > 0:

                scores[captcha_type] = score

        

        return max(scores, key=scores.get) if scores else None

    

    def _needs_chromium(self, captcha_type: Optional[str], task: Task) -> bool:

        """Determine if Chromium mode is needed based on CAPTCHA type and task."""

        if captcha_type in ('recaptcha_v2', 'recaptcha_v3', 'hcaptcha', 'turnstile'):

            return True

        if task.method in (CaptchaMethod.RECAPTCHA_V2, CaptchaMethod.RECAPTCHA_V3, 

                          CaptchaMethod.HCAPTCHA, CaptchaMethod.CLOUDFLARE):

            return True

        return False

    

    def _create_chromium_options(self) -> ChromiumOptions:

        """Create Chromium options with anti-detection settings."""

        co = ChromiumOptions()

        

        if self.chromium_path:

            co.set_browser_path(self.chromium_path)

        

        if self.headless:

            co.headless(True)

        

        if self.proxy:

            co.set_proxy(self.proxy)

        

        # Anti-detection settings

        co.set_argument('--disable-blink-features=AutomationControlled')

        co.set_argument('--disable-web-security')

        co.set_argument('--disable-features=IsolateOrigins,site-per-process')

        co.set_argument('--disable-site-isolation-trials')

        

        # Stealth headers

        if self.user_agent:

            co.set_user_agent(self.user_agent)

        

        return co

    

    def _init_session_page(self) -> SessionPage:

        """Initialize lightweight SessionPage for HTTP-only challenges."""

        page = SessionPage()

        

        if self.user_agent:

            page.set.user_agent(self.user_agent)

        

        if self.proxy:

            page.set.proxy(self.proxy)

        

        return page

    

    def _init_chromium_page(self) -> ChromiumPage:

        """Initialize full ChromiumPage for JS-rendered CAPTCHAs."""

        co = self._create_chromium_options()

        return ChromiumPage(chromium_options=co)

    

    def _find_captcha_iframe(self, page) -> Optional[Any]:
        """Find and return CAPTCHA iframe element."""
        iframe = self._find_iframe_by_selector(page)
        if iframe is not None:
            return iframe
        return self._find_iframe_by_frame_inspection(page)

    def _find_iframe_by_selector(self, page) -> Optional[Any]:
        """Try each iframe selector until one resolves to an element."""
        for selector in _DRISSIONPAGE_IFRAME_SELECTORS:
            try:
                iframe = page.ele(selector, timeout=2)
            except _BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
                continue
            if iframe:
                return iframe
        return None

    def _find_iframe_by_frame_inspection(self, page) -> Optional[Any]:
        """Walk page.get_frames() looking for a captcha-bearing src."""
        try:
            if not hasattr(page, "get_frames"):
                return None
            frames = page.get_frames()
            for frame in frames:
                if self._frame_src_matches_captcha(frame):
                    return frame
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return None

    @staticmethod
    def _frame_src_matches_captcha(frame: Any) -> bool:
        """True iff ``frame``'s src attribute references a captcha provider."""
        src = frame.attr("src") if hasattr(frame, "attr") else str(frame)
        markers = ("recaptcha", "hcaptcha", "turnstile")
        return any(marker in str(src).lower() for marker in markers)

    def _extract_token(self, page: Any, captcha_type: str) -> Optional[str]:
        """Extract CAPTCHA token from page."""
        token = self._extract_token_from_selectors(page, captcha_type)
        if token:
            return token
        if captcha_type == "datadome":
            return self._extract_datadome_cookie_value(page)
        return None

    def _extract_token_from_selectors(self, page: Any, captcha_type: str) -> Optional[str]:
        """Iterate the configured selectors for ``captcha_type``."""
        selectors = self.TOKEN_SELECTORS.get(captcha_type, [])
        for selector in selectors:
            value = self._read_token_from_selector(page, selector)
            if value:
                return value
        return None

    def _read_token_from_selector(self, page: Any, selector: str) -> Optional[str]:
        """Read a single selector's value/text and return whichever is long enough."""
        try:
            elem = page.ele(selector, timeout=2)
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return None
        if not elem:
            return None
        value = elem.attr("value")
        if value and len(value) > 20:
            return value
        text = elem.text
        if text and len(text) > 20:
            return text
        return None

    def _extract_datadome_cookie_value(self, page) -> Optional[str]:
        """Look for a datadome-prefixed cookie name and return its value."""
        try:
            cookies = page.cookies()
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return None
        for cookie in cookies:
            if "datadome" in cookie.get("name", "").lower():
                return cookie.get("value")
        return None

    

    def _execute_js_challenge(self, page: Any, task: Task) -> bool:

        """Execute JavaScript to trigger CAPTCHA challenge."""

        site_key = task.params.get('sitekey') or task.params.get('googlekey')

        

        if task.method == CaptchaMethod.RECAPTCHA_V2 and site_key:

            js_code = f"""

                if (typeof grecaptcha !== 'undefined') {{

                    grecaptcha.execute('{site_key}');

                    return true;

                }}

                return false;

            """

            try:

                return page.run_js(js_code)

            except _BROAD_EXC:

                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

                pass

        

        return True  # Assume success if no explicit trigger needed

    

    async def _run_solver_attempt(
        self,
        task: Task,
        *,
        mode: str,
        body: Any,
    ) -> TaskResult:
        """Shared bootstrap+teardown for session/chromium solver attempts."""
        page = self._init_chromium_page() if mode == "chromium" else self._init_session_page()
        self._page = page
        self._mode = mode
        start_time = time.time()
        try:
            url = task.params.get("pageurl")
            if not url:
                return TaskResult(
                    success=False,
                    error_code="ERROR_BAD_PARAMETERS",
                    error_message="Missing pageurl parameter",
                )
            return await body(page, task, start_time)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return TaskResult(
                success=False,
                error_code="ERROR_EXCEPTION",
                error_message=str(e),
            )
        finally:
            self._safe_quit(page)

    async def _solve_with_session(self, task: Task) -> TaskResult:
        """Solve CAPTCHA using lightweight SessionPage."""
        return await self._run_solver_attempt(
            task, mode="session", body=self._session_body,
        )

    async def _session_body(self, page: Any, task: Task, start_time: float) -> TaskResult:
        """Lightweight SessionPage solve body; may upgrade to Chromium."""
        page.get(task.params["pageurl"])
        captcha_type = self._detect_captcha_type(page.html)
        if self._needs_chromium(captcha_type, task):
            page.quit()
            return await self._solve_with_chromium(task)
        token = self._extract_token(page, captcha_type or "recaptcha")
        return self._session_token_result(token, start_time)

    def _session_token_result(
        self, token: Optional[str], start_time: float,
    ) -> TaskResult:
        """Wrap a session-extracted token (or its absence) in a TaskResult."""
        solve_time_ms = int((time.time() - start_time) * 1000)
        if token:
            return TaskResult(
                success=True,
                token=token,
                solved_at=datetime.now(tz=timezone.utc),
                solve_time_ms=solve_time_ms,
            )
        return TaskResult(
            success=False,
            error_code="ERROR_CAPTCHA_UNSOLVABLE",
            error_message="Could not extract token with SessionPage",
        )

    

    async def _solve_with_chromium(self, task: Task) -> TaskResult:
        """Solve CAPTCHA using full Chromium browser."""
        return await self._run_solver_attempt(
            task, mode="chromium", body=self._chromium_body,
        )

    async def _chromium_body(self, page: Any, task: Task, start_time: float) -> TaskResult:
        """Heavy Chromium solve body; navigates and polls for token."""
        page.get(task.params["pageurl"])
        page.wait.load_start()
        await asyncio.sleep(2)
        captcha_type = self._detect_captcha_type(page.html)
        await self._interact_with_captcha_iframe(page)
        self._execute_js_challenge(page, task)
        return await self._poll_for_chromium_token(page, captcha_type, start_time)

    async def _interact_with_captcha_iframe(self, page) -> None:
        """If a captcha iframe exists, click its anchor checkbox if visible."""
        iframe = self._find_captcha_iframe(page)
        if iframe is None:
            return
        try:
            frame = page.get_frame(iframe) if hasattr(page, "get_frame") else iframe
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            return
        if not frame:
            return
        await asyncio.sleep(3)
        await self._click_checkbox_if_visible(frame)
        if hasattr(page, "to_main_frame"):
            page.to_main_frame()

    async def _click_checkbox_if_visible(self, frame: Any) -> None:
        """Click the recaptcha anchor checkbox; swallow misses."""
        try:
            checkbox = frame.ele(".rc-anchor-checkbox", timeout=3)
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return
        if checkbox:
            checkbox.click()

    async def _poll_for_chromium_token(
        self, page, captcha_type: Optional[str], start_time: float,
    ) -> TaskResult:
        """Poll for token; return TaskResult on success / timeout / error state."""
        poll_start = time.time()
        ctype = captcha_type or "recaptcha"
        token: Optional[str] = None
        while time.time() - poll_start < self.timeout:
            token = self._extract_token(page, ctype)
            if token and len(token) > 20:
                break
            await asyncio.sleep(1)
            if self._is_captcha_in_error_state(page):
                return TaskResult(
                    success=False,
                    error_code="ERROR_CAPTCHA_UNSOLVABLE",
                    error_message="CAPTCHA error state detected",
                )
        return self._chromium_token_result(token, start_time)

    def _is_captcha_in_error_state(self, page) -> bool:
        """True iff the page shows the captcha error banner."""
        html = page.html.lower()
        if "error" not in html or "captcha" not in html:
            return False
        try:
            return bool(page.ele(".rc-anchor-error-msg", timeout=1))
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return False

    def _chromium_token_result(
        self, token: Optional[str], start_time: float,
    ) -> TaskResult:
        """Build the TaskResult for the chromium polling outcome."""
        solve_time_ms = int((time.time() - start_time) * 1000)
        if token and len(token) > 20:
            return TaskResult(
                success=True,
                token=token,
                solved_at=datetime.now(tz=timezone.utc),
                solve_time_ms=solve_time_ms,
            )
        return TaskResult(
            success=False,
            error_code="ERROR_TIMEOUT",
            error_message=f"Token not found within {self.timeout}s",
        )

    def _safe_quit(self, page: Any) -> None:
        """Close ``page`` swallowing any quit-path error."""
        try:
            page.quit()
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    

    async def _attempt_solve(self, task: Task, attempt: int) -> TaskResult:

        """Single solving attempt with mode selection."""

        mode = task.params.get('mode', 'auto')

        

        if mode == 'session':

            return await self._solve_with_session(task)

        elif mode == 'chromium':

            return await self._solve_with_chromium(task)

        else:

            # Auto mode: try session first, fall back to chromium

            result = await self._solve_with_session(task)

            if not result.success and result.error_code != "ERROR_BAD_PARAMETERS":

                return await self._solve_with_chromium(task)

            return result

    

    async def solve(self, task: Task) -> TaskResult:
        """Solve a CAPTCHA task using DrissionPage."""
        if not self.enabled:
            return TaskResult(
                success=False,
                error_code="ERROR_SOLVER_NOT_AVAILABLE",
                error_message="DrissionPage solver is disabled",
            )

        last_error: Optional[TaskResult] = None
        for attempt in range(1, self.max_retries + 1):
            task.retry_count = attempt - 1
            outcome = await self._run_one_attempt(task, attempt)
            if outcome.success:
                return outcome
            last_error = outcome
            if outcome.error_code == "ERROR_BAD_PARAMETERS":
                return outcome
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** attempt)
        return last_error or TaskResult(
            success=False,
            error_code="ERROR_MAX_RETRIES",
            error_message="All attempts failed",
        )

    async def _run_one_attempt(self, task: Task, attempt: int) -> TaskResult:
        """Run one solver attempt with timeout protection."""
        try:
            return await asyncio.wait_for(
                self._attempt_solve(task, attempt),
                timeout=self.timeout + 10,
            )
        except asyncio.TimeoutError:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            return TaskResult(
                success=False,
                error_code="ERROR_TIMEOUT",
                error_message=f"Solver timed out after {self.timeout}s",
            )

    

    def health_check(self) -> Dict[str, Any]:

        """

        Check solver health by verifying DrissionPage import -- no browser launch.

        

        Returns:

            Dict with 'healthy', 'message', 'details'

        """

        try:

            from DrissionPage import SessionPage  # noqa: F401

            

            return {

                'healthy': True,

                'message': 'DrissionPage module available',

                'details': {

                    'mode': self._mode or 'not_initialized',

                    'headless': self.headless,

                    'timeout': self.timeout,

                    'max_retries': self.max_retries

                }

            }

            

        except ImportError as e:

            return {

                'healthy': False,

                'message': f'DrissionPage not installed: {str(e)}',

                'details': {'install_cmd': 'pip install DrissionPage'}

            }

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            return {

                'healthy': False,

                'message': f'Health check failed: {str(e)}',

                'details': {'error': str(e)}

            }

