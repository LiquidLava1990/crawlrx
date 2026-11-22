"""

hCaptcha checkbox, local-vision grid and token-polling handler using managed CDP.

"""



try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

import asyncio

import json

import logging


import time
import urllib.request

from pathlib import Path

from typing import Optional, Dict, Any





try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Error as PlaywrightError

    PLAYWRIGHT_AVAILABLE = True

except (ImportError, Exception):

    emit(ErrCode.IMPORT_ERROR, "unhandled")

    async_playwright = None

    Browser = None

    BrowserContext = None

    Page = None
    PlaywrightError = RuntimeError

    PLAYWRIGHT_AVAILABLE = False





from ..solver_base import BaseSolver, SolverError
from ._solver_helpers import BROAD_EXC as _BROAD_EXC  # narrow safety-net catch tuple

# Cancellation must escape every async catch so router deadlines can work.
_BROAD_EXC = tuple(exc for exc in _BROAD_EXC if exc is not asyncio.CancelledError) + (PlaywrightError,)





logger = logging.getLogger(__name__)





_CHALLENGE_FRAME_SELECTORS: tuple = (
    'iframe[src*="hcaptcha-challenge"]',
    'iframe[src*="challenge"]',
    'iframe[src*="funstage"]',
)


def _hcaptcha_failure(error_message: str, *, duration_ms: int, attempts: int,
                      error_code: str = "ERROR_CAPTCHA_UNSOLVABLE") -> Dict[str, Any]:
    """Standard failure-shape dict returned from solve()."""
    return {
        "success": False,
        "error_code": error_code,
        "error": error_message,
        "duration_ms": duration_ms,
        "attempts": attempts,
    }


def _hcaptcha_success(token: str, start_time: float, attempt: int) -> Dict[str, Any]:
    """Standard success-shape dict returned from solve()."""
    return {
        "success": True,
        "token": token,
        "duration_ms": int((time.time() - start_time) * 1000),
        "attempts": attempt,
    }


class HcaptchaChallengeHandler(BaseSolver):

    """

    Click an hCaptcha checkbox and poll for a genuine response token.

    This handler connects to the managed browser via CDP. Nine-tile image
    grids can be interpreted by the configured local vision service.

    """

    

    @property

    def solver_type(self) -> str:

        return "hcaptcha"

    

    # CDP endpoint - uses config cdp_port or defaults to 9234

    CDP_ENDPOINT: str = "http://localhost:9234"  # TODO: use service_registry

    

    # Default timeout for solving (hCaptcha can be slow)

    DEFAULT_TIMEOUT_MS = 120000  # 120 seconds

    

    # Default retry attempts

    DEFAULT_RETRIES = 3

    


    

    def _ensure_hc_models(self) -> bool:
        """The installed hCaptcha agent has no legacy model installer."""
        logger.warning("Legacy hCaptcha model installer is unavailable")
        return False

    def __init__(self, config: object=None) -> None:

        if config is None:

            config = {"enabled": True, "timeout": 120}

        super().__init__(config)

        self.model_dir = Path(config.get("model_dir", "")) if config.get("model_dir") else Path(__file__).parent / "models"

        self.timeout_ms = int(self.timeout * 1000)

        self.max_retries = config.get("retries", config.get("max_retries", 3))

        self.headless = config.get("headless", False)

        # Set CDP endpoint from config or default

        cdp_port = config.get("cdp_port", 9234)

        self.CDP_ENDPOINT = f"http://localhost:{cdp_port}"

        

        # Shared browser resources

        self._browser = None

        self._context = None

        self._playwright = None

        # A CDP context may contain other clients' tabs; only our pages belong to us.
        self._owned_pages = set()

        

        # Ensure model directory exists

        self.model_dir.mkdir(parents=True, exist_ok=True)

        

        logger.info("HcaptchaChallengeHandler initialized (timeout=%sms, retries=%s)", self.timeout_ms, self.max_retries)

    

    def _ensure_models(self) -> bool:
        """Legacy local model installer is unavailable; grids use the vision service."""
        logger.warning("Legacy hCaptcha local model installer is unavailable")
        return False

    async def _get_browser(self) -> Browser:

        """

        Get or create browser connection via CDP.

        

        Returns:

            Connected Browser instance

        """

        if self._browser and self._browser.is_connected():

            return self._browser

        

        if not PLAYWRIGHT_AVAILABLE:

            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install")

        

        if not self._playwright:

            self._playwright = await async_playwright().start()

        

        try:

            # Connect to existing browser via CDP

            self._browser = await self._playwright.chromium.connect_over_cdp(self.CDP_ENDPOINT)

            logger.debug("Connected to browser via CDP at %s", self.CDP_ENDPOINT)

            return self._browser

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.error("Failed to connect to browser via CDP: %s", e)

            raise RuntimeError(f"Cannot connect to browser at {self.CDP_ENDPOINT}. Ensure Chrome is running with --remote-debugging-port=9234")

    

    async def _get_context(self) -> BrowserContext:

        """

        Get or create shared browser context.

        

        Returns:

            BrowserContext instance

        """

        if self._context:

            return self._context

        

        browser = await self._get_browser()

        

        # Try to reuse existing context or create new one

        contexts = browser.contexts

        if contexts:

            self._context = contexts[0]

            logger.debug("Reusing existing browser context")

        else:

            self._context = await browser.new_context(

                viewport={"width": 1920, "height": 1080},

                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"

            )

            logger.debug("Created new browser context")

        

        return self._context

    

    async def _detect_hcaptcha_frame(self, page: Page) -> Optional[Any]:

        """

        Detect and return the hCaptcha iframe element.

        

        Args:

            page: Playwright Page instance

            

        Returns:

            FrameLocator for the hCaptcha iframe or None

        """

        # hCaptcha iframe selectors

        selectors = [

            'iframe[src*="hcaptcha.com"]',

            'iframe[data-hcaptcha-widget-id]',

            'iframe[src*="https://newassets.hcaptcha.com"]', 

            'iframe[src*="checksite"]',

            '[data-hcaptcha-widget-id] iframe'

        ]

        

        # is_visible() is an immediate observation; its timeout argument does not
        # wait for a dynamically inserted widget. Poll briefly and ignore hidden
        # matching frames so they cannot mask a later visible checkbox.
        deadline = time.monotonic() + min(5.0, max(0.0, self.timeout_ms / 1000))
        while True:
            for selector in selectors:
                try:
                    frame = page.locator(f"{selector}:visible").first
                    if await frame.is_visible():
                        logger.debug("Found hCaptcha iframe with selector: %s", selector)
                        return frame
                except _BROAD_EXC:
                    emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or page.is_closed():
                break
            await asyncio.sleep(min(0.2, remaining))

        

        return None

    

    async def _click_checkbox(self, page: Page, hcaptcha_frame: Any) -> bool:
        """Click the hCaptcha checkbox to trigger challenge."""
        try:
            frame = await self._resolve_hcaptcha_frame(hcaptcha_frame)
            if frame is None:
                logger.warning("Could not access hCaptcha frame content")
                return False
            if await self._click_first_checkbox(frame):
                return True
            logger.warning("Could not find hCaptcha checkbox")
            return False
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Error clicking checkbox: %s", e)
            return False

    async def _resolve_hcaptcha_frame(self, hcaptcha_frame: Any) -> Optional[Any]:
        """Convert a locator/handle to its content frame, returning None on failure."""
        frame_element = await hcaptcha_frame.element_handle()
        return await frame_element.content_frame()

    async def _click_first_checkbox(self, frame: Any) -> bool:
        """Try every checkbox selector in turn; click the first visible one."""
        selectors = (
            "#checkbox",
            ".checkbox",
            '[role="checkbox"]',
            'div[class*="checkbox"]',
            "#anchor input",
        )
        deadline = time.monotonic() + min(5.0, max(0.0, self.timeout_ms / 1000))
        while True:
            for selector in selectors:
                if await self._try_click_selector(frame, selector):
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0 or frame.is_detached():
                break
            await asyncio.sleep(min(0.2, remaining))
        return False

    async def _try_click_selector(self, frame: Any, selector: str) -> bool:
        """Click ``selector`` if visible; True on click, False on miss / error."""
        try:
            checkbox = frame.locator(f"{selector}:visible").first
            if not await checkbox.is_visible():
                return False
            from .hcaptcha_viewport import click_control
            await click_control(frame.page, frame, checkbox)
            logger.debug("Clicked hCaptcha checkbox with selector: %s", selector)
            await asyncio.sleep(0.5)
            return True
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return False

    

    async def _solve_challenge(self, page: Page) -> Optional[str]:
        """Wait for a genuine response token after the checkbox interaction."""
        try:
            return await self._poll_challenge(page)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Error solving challenge: %s", e)
            return None

    async def _locate_challenge_frame(self, page: Page) -> Optional[Any]:
        """Find the visible challenge iframe and return its content frame."""
        for selector in _CHALLENGE_FRAME_SELECTORS:
            frame = await self._resolve_frame_if_visible(page, selector)
            if frame is not None:
                logger.debug("Found challenge iframe")
                return frame
        return None

    async def _resolve_frame_if_visible(
        self, page: Page, selector: str,
    ) -> Optional[Any]:
        """Return the content frame for ``selector`` if visible within 5s."""
        try:
            frame_locator = page.locator(selector).first
            if not await frame_locator.is_visible(timeout=5000):
                return None
            frame_elem = await frame_locator.element_handle()
            return await frame_elem.content_frame()
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return None

    async def _poll_challenge(self, page: Page) -> Optional[str]:
        """Poll until a token arrives, the page closes, or the deadline expires."""
        from .hcaptcha_grid import HcaptchaGridSolver
        from .hcaptcha_object import HcaptchaObjectSolver
        from .hcaptcha_ring import HcaptchaRingSolver
        from .hcaptcha_motion import HcaptchaMotionSolver
        from .hcaptcha_line import HcaptchaLineSolver
        from .hcaptcha_overlap import HcaptchaOverlapSolver
        grid_solver = HcaptchaGridSolver()
        object_solver = HcaptchaObjectSolver()
        ring_solver = HcaptchaRingSolver()
        motion_solver = HcaptchaMotionSolver()
        line_solver = HcaptchaLineSolver()
        overlap_solver = HcaptchaOverlapSolver()
        attempts = (grid_solver.try_grid, object_solver.try_object, ring_solver.try_ring,
                    motion_solver.try_motion, line_solver.try_line, overlap_solver.try_overlap)
        deadline = time.monotonic() + max(0.0, self.timeout_ms / 1000)
        while True:
            token = await self._extract_token(page)
            if token:
                return token
            remaining = deadline - time.monotonic()
            if remaining <= 0 or page.is_closed():
                return None
            try:
                async with asyncio.timeout(remaining):
                    for attempt in attempts:
                        if await attempt(page):
                            break
            except asyncio.TimeoutError:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.5, remaining))

    async def _challenge_disappeared(self, page: Page) -> bool:
        """True iff the challenge iframe is no longer visible."""
        try:
            visible = await page.locator(
                'iframe[src*="hcaptcha-challenge"]'
            ).first.is_visible(timeout=1000)
            return not visible
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return False

    

    async def _extract_token(self, page: Page) -> Optional[str]:

        """

        Extract the hCaptcha token from the page.

        

        Args:

            page: Playwright Page instance

            

        Returns:

            hCaptcha response token or None

        """

        try:

            # Try multiple token extraction methods

            

            # Method 1: Direct textarea value

            token = await page.evaluate("""

                () => {

                    const textarea = document.querySelector('textarea[name="h-captcha-response"]');

                    return textarea ? textarea.value : null;

                }

            """)

            

            if token and len(token) > 10:

                logger.debug("Extracted token from textarea")

                return token

            

            # Method 2: Check data-hcaptcha-response attribute

            token = await page.evaluate("""

                () => {

                    const widget = document.querySelector('[data-hcaptcha-widget-id]');

                    return widget ? widget.getAttribute('data-hcaptcha-response') : null;

                }

            """)

            

            if token and len(token) > 10:

                logger.debug("Extracted token from widget attribute")

                return token

            

            # Method 3: Check window.grecaptcha or window.hcaptcha

            token = await page.evaluate("""

                () => {

                    if (window.hcaptcha && window.hcaptcha.getResponse) {

                        return window.hcaptcha.getResponse();

                    }

                    return null;

                }

            """)

            

            if token and len(token) > 10:

                logger.debug("Extracted token from hcaptcha.getResponse()")

                return token

            

            return None

            

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.error("Error extracting token: %s", e)

            return None

    

    async def solve(self, task: dict) -> dict:
        """Solve an hCaptcha task."""
        start_time = time.time()
        params = task if isinstance(task, dict) else getattr(task, "params", {})
        sitekey = params.get("sitekey")
        pageurl = params.get("pageurl")
        if not sitekey or not pageurl:
            return _hcaptcha_failure(
                "Missing required fields: sitekey and pageurl",
                duration_ms=0, attempts=0, error_code="ERROR_BAD_PARAMETERS",
            )
        page: Optional[Page] = None
        try:
            context = await self._get_context()
            page = await context.new_page()
            self._owned_pages.add(page)
            return await self._run_solve_loop(page, pageurl, start_time)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("Fatal error in solve: %s", e)
            return _hcaptcha_failure(str(e), duration_ms=duration_ms, attempts=0)
        finally:
            await self._close_page_safely(page)

    

    async def _run_solve_loop(
        self, page: "Page", pageurl: str, start_time: float,
    ) -> dict:
        """Drive the solve attempts; return the final response dict."""
        for attempt in range(1, self.max_retries + 1):
            result = await self._try_one_attempt(page, pageurl, start_time, attempt)
            if result is not None:
                return result
        duration_ms = int((time.time() - start_time) * 1000)
        return _hcaptcha_failure(
            f"Failed to solve after {self.max_retries} attempts",
            duration_ms=duration_ms,
            attempts=self.max_retries,
        )

    async def _try_one_attempt(
        self, page: "Page", pageurl: str, start_time: float, attempt: int,
    ) -> "Optional[dict]":
        """Run one attempt; return result dict on success or None to continue."""
        try:
            return await self._solve_one_attempt(page, pageurl, start_time, attempt)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Attempt %s failed: %s", attempt, e)
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** attempt)
            return None

    async def _solve_one_attempt(
        self,
        page: "Page",
        pageurl: str,
        start_time: float,
        attempt: int,
    ) -> Optional[dict]:
        """Run one navigate -> detect -> click -> solve cycle."""
        logger.info("Attempt %s/%s for %s", attempt, self.max_retries, pageurl)
        await page.goto(pageurl, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        hcaptcha_frame = await self._detect_hcaptcha_frame(page)
        if not hcaptcha_frame:
            token = await self._extract_token(page)
            if token:
                return _hcaptcha_success(token, start_time, attempt)
            raise SolverError(
                "ERROR_HCAPTCHA_NOT_FOUND",
                "hCaptcha iframe not found on page",
            )
        await self._click_checkbox(page, hcaptcha_frame)
        token = await self._solve_challenge(page)
        if token:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.info("Successfully solved hCaptcha in %sms", duration_ms)
            return _hcaptcha_success(token, start_time, attempt)
        await asyncio.sleep(3)
        token = await self._extract_token(page)
        if token:
            return _hcaptcha_success(token, start_time, attempt)
        logger.warning("Attempt %s: No token extracted, retrying...", attempt)
        return None

    async def _close_page_safely(self, page: Optional["Page"]) -> None:
        """Close ``page`` if non-None, swallowing any close-path error."""
        if page is None:
            return
        try:
            await page.close()
            self._owned_pages.discard(page)
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    def health_check(self) -> Dict[str, Any]:
        """Check the required local CDP endpoint without creating a browser session."""
        if not PLAYWRIGHT_AVAILABLE:
            return {'healthy': False, 'enabled': self.enabled,
                    'message': 'Playwright import failed.'}
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.CDP_ENDPOINT + '/json/version', timeout=0.5) as response:
                version = json.loads(response.read(65536))
            if not isinstance(version.get('webSocketDebuggerUrl'), str):
                raise ValueError('CDP browser endpoint missing')
        except (OSError, ValueError, TypeError, AttributeError):
            return {'healthy': False, 'enabled': self.enabled,
                    'message': f'Browser CDP endpoint unavailable at {self.CDP_ENDPOINT}.'}
        return {'healthy': True, 'enabled': self.enabled,
                'message': 'CDP available; supports checkbox, local-vision nine-tile grids and token polling.'}

    

    async def close(self) -> object:
        """Clean up resources. Close all pages and browser."""
        try:
            await self._close_all_pages_safely()
            await self._close_browser_safely()
            await self._stop_playwright_safely()
            logger.info("HcaptchaChallengeHandler resources cleaned up")
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Error during cleanup: %s", e)
            return None  # explicit recovery: cleanup is best-effort; _BROAD_EXC is non-fatal

    async def _close_all_pages_safely(self) -> None:
        """Close pages created by this handler, leaving other clients' tabs alone."""
        for page in tuple(self._owned_pages):
            await self._close_page_safely(page)

    async def _close_browser_safely(self) -> None:
        """Close the cached browser, if any, and clear the slot."""
        if self._browser is None:
            self._context = None
            return
        await self._browser.close()
        self._browser = None
        self._context = None

    async def _stop_playwright_safely(self) -> None:
        """Stop the Playwright driver, if any, and clear the slot."""
        if self._playwright is None:
            return
        await self._playwright.stop()
        self._playwright = None

    

    async def __aenter__(self) -> object:

        """Async context manager entry."""

        return self

    

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:

        """Async context manager exit."""

        await self.close()





# Convenience function for direct usage

async def solve_hcaptcha(

    sitekey: str,

    pageurl: str,

    timeout_ms: int = 120000,

    max_retries: int = 3

) -> dict:

    """

    Convenience function to solve hCaptcha in one call.

    

    Args:

        sitekey: hCaptcha sitekey

        pageurl: URL of the page with hCaptcha

        timeout_ms: Maximum solve time

        max_retries: Number of retry attempts

        

    Returns:

        Result dictionary with success, token, error, duration_ms, attempts

    """

    solver = HcaptchaChallengeHandler({"enabled": True, "timeout": timeout_ms / 1000, "retries": max_retries})

    try:

        result = await solver.solve({

            'sitekey': sitekey,

            'pageurl': pageurl

        })

        return result

    finally:

        await solver.close()





if __name__ == "__main__":

    # Test the solver

    logging.basicConfig(level=logging.INFO)

    

    async def test() -> None:

        solver = HcaptchaChallengeHandler()

        

        # Health check

        healthy = await solver.health_check()

        print(f"Health check: {healthy}")

        

        if healthy:

            # Example solve (replace with real values)

            result = await solver.solve({

                'sitekey': '10000000-ffff-ffff-ffff-000000000001',

                'pageurl': 'https://demo.hcaptcha.com'

            })

            print(f"Result: {json.dumps(result, indent=2)}")

        

        await solver.close()

    

    asyncio.run(test())

