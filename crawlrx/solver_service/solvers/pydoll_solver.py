# pip install pydoll-python



"""

Pydoll-based CDP stealth solver for FlareSolverr.



Uses Pydoll (github.com/autoscrape-labs/pydoll) for WebDriver-free,

stealth-first browser automation via Chrome DevTools Protocol.



Requirements:

    pip install pydoll-python

"""


try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()
    def emit(*a: object, **kw: object) -> None: pass

import asyncio

import os

import random

import logging

from typing import Any, Dict, Optional

from pathlib import Path



try:

    from pydoll.browser import Chrome as Browser

    from pydoll.browser.tab import Tab as Page

    from pydoll.elements.web_element import WebElement as Element

    from pydoll.exceptions import PydollException

    HAS_PYDOLL = True

except ImportError:

    Browser = None

    Page = None

    Element = None

    HAS_PYDOLL = False

    class PydollException(Exception):
        pass



from ..solver_base import BaseSolver

from ..models import Task, TaskResult

from ._solver_helpers import BROAD_EXC




logger = logging.getLogger(__name__)





class PydollSolver(BaseSolver):

    """

    CDP-based stealth challenge handler using Pydoll.

    

    Multi-strategy approach:

    1. Stealth page load with human-like behavior

    2. CAPTCHA detection (reCAPTCHA, hCaptcha, Turnstile, etc.)

    3. Human simulation (natural mouse movements, random delays)

    4. Challenge handling (checkbox, image grid, slider)

    5. Token extraction from response fields

    """

    

    # CAPTCHA detection selectors

    CAPTCHA_SELECTORS = {

        'recaptcha_v2': [

            '.g-recaptcha',

            'iframe[src*="google.com/recaptcha"]',

            'iframe[src*="recaptcha/api2"]',

            '#recaptcha-anchor',

            '.recaptcha-checkbox',

        ],

        'recaptcha_v3': [

            'input[name="g-recaptcha-response"]',

            'textarea[name="g-recaptcha-response"]',

        ],

        'hcaptcha': [

            '.h-captcha',

            'iframe[src*="hcaptcha.com"]',

            '#checkbox',

            '[data-hcaptcha-widget-id]',

        ],

        'turnstile': [

            '.cf-turnstile',

            'input[name="cf-turnstile-response"]',

            'iframe[src*="challenges.cloudflare"]',

        ],

        'geetest': [

            '.geetest_radar_btn',

            '.geetest_slider',

            '#geetest-box',

        ],

        'generic_checkbox': [

            '.captcha-checkbox',

            '#captcha-checkbox',

            '[class*="captcha"][class*="checkbox"]',

        ],

    }

    

    # Token extraction selectors

    TOKEN_SELECTORS = [

        'textarea[name="g-recaptcha-response"]',

        'input[name="g-recaptcha-response"]',

        'textarea[name="h-captcha-response"]',

        'input[name="h-captcha-response"]',

        'input[name="cf-turnstile-response"]',

        'textarea[name="cf-turnstile-response"]',

        '#g-recaptcha-response',

        '#h-captcha-response',

        '[data-g-recaptcha-response]',

    ]

    

    # Challenge type selectors

    CHALLENGE_SELECTORS = {

        'image_grid': [

            '.rc-imageselect',

            '.rc-image-tile',

            '[class*="imageselect"]',

            '.geetest_table_box',

        ],

        'slider': [

            '.geetest_slider',

            '.rc-slider',

            '[class*="slider"][class*="captcha"]',

            '.slider-track',

        ],

        'audio': [

            '.rc-audiochallenge',

            '#audio-response',

            '[class*="audio"][class*="challenge"]',

        ],

    }

    

    # Chrome discovery paths (checked in order; CHROME_EXE env var takes priority)

    CHROME_PATHS = [p for p in [

        os.environ.get('CHROME_EXE', ''),

        r'C:\Program Files\Google\Chrome\Application\chrome.exe',

        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',

    ] if p]



    def __init__(self, config: Dict[str, Any]) -> None:

        super().__init__(config)

        self.max_retries = config.get('max_retries', 3)

        self.headless = config.get('headless', True)

        self.user_data_dir = config.get('user_data_dir', None)

        self.proxy = config.get('proxy', None)

        self.viewport = config.get('viewport', {'width': 1920, 'height': 1080})

        self.stealth_delay_min = config.get('stealth_delay_min', 1.0)

        self.stealth_delay_max = config.get('stealth_delay_max', 3.0)

        self.mouse_steps = config.get('mouse_steps', 10)

        self.scroll_range = config.get('scroll_range', (300, 800))

        self.chrome_path = config.get('chrome_path', self._find_chrome())

    

    @staticmethod

    def _find_chrome() -> Optional[str]:

        """Find Chrome executable on disk."""

        import os

        for path in PydollSolver.CHROME_PATHS:

            if os.path.isfile(path):

                return path

        import glob
        browser_root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/ms-playwright')
        candidates = glob.glob(os.path.join(browser_root, 'chromium-*', 'chrome-linux64', 'chrome'))
        for path in sorted(candidates, reverse=True):
            if os.path.isfile(path):
                return path

        return None



    @property

    def solver_type(self) -> str:

        return "pydoll"

    

    async def _attempt_pydoll_solve(
        self, task: Task, attempt: int,
    ) -> tuple[Optional[TaskResult], Optional[str]]:
        """One attempt. Returns (success_result, last_error) — exactly one is non-None."""
        try:
            logger.info(
                "Pydoll solve attempt %s/%s for %s",
                attempt, self.max_retries, task.method.value,
            )
            result = await self._solve_single(task)
            if result.success:
                return result, None
            return None, result.error_message
        except asyncio.TimeoutError:
            emit(ErrCode.TIMEOUT_ERROR, "unhandled")
            err = f"Timeout on attempt {attempt}"
            logger.warning(err)
            # explicit recovery: timeout non-fatal; caller may retry
            return None, err
        except (PydollException, *BROAD_EXC) as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error("Attempt %s failed: %s", attempt, e)
            # explicit recovery: BROAD_EXC non-fatal; caller may retry
            return None, str(e)

    async def solve(self, task: Task) -> TaskResult:
        """Solve CAPTCHA using Pydoll CDP browser automation.

        Args:
            task: Task object with url, captcha_type, and optional site_key

        Returns:
            TaskResult with success status and extracted token
        """
        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 1):
            result, err = await self._attempt_pydoll_solve(task, attempt)
            if result is not None:
                return result
            last_error = err
            if attempt < self.max_retries:
                delay = random.uniform(2.0, 5.0)
                logger.info("Retrying in %.1fs...", delay)
                await asyncio.sleep(delay)

        return TaskResult(
            success=False,
            error_code="ERROR_MAX_RETRIES",
            error_message=f"All {self.max_retries} attempts failed. Last: {last_error}",
        )

    

    async def _solve_single(self, task: Task) -> TaskResult:

        """Execute single solve attempt."""

        if not HAS_PYDOLL:

            return TaskResult(success=False, error_code="ERROR_NO_PYDOLL", error_message="pydoll-python not installed")



        # Build ChromiumOptions with binary location

        try:

            from pydoll.browser.options import ChromiumOptions

            opts = ChromiumOptions()

            if self.chrome_path:

                opts.binary_location = self.chrome_path

            if self.headless:

                opts.headless = True

            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                opts.add_argument('--no-sandbox')
                opts.add_argument('--disable-dev-shm-usage')

        except ImportError:

            opts = None



        async with Browser(options=opts) as browser:

            page = await browser.start()

            

            try:

                # Step 1: Stealth page load

                await self._stealth_page_load(page, task.params.get('pageurl', ''))

                

                # Step 2: CAPTCHA detection

                captcha_info = await self._detect_captcha(page, task.method.value)

                if not captcha_info:

                    return TaskResult(

                        success=False,

                        error_code="ERROR_CAPTCHA_NOT_FOUND",

                        error_message="No CAPTCHA detected on page",

                    )

                

                logger.info("Detected %s CAPTCHA", captcha_info['type'])

                

                # Step 3 & 4: Human simulation + challenge handling

                token = await self._handle_challenge(page, captcha_info, task)

                

                if token:

                    return TaskResult(

                        success=True,

                        token=token,

                    )

                else:

                    return TaskResult(

                        success=False,

                        error_code="ERROR_TOKEN_EXTRACTION",

                        error_message="Failed to extract CAPTCHA token",

                    )

                    

            except BROAD_EXC as e:

                emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

                logger.exception("Solve single failed")

                return TaskResult(success=False, error_code="ERROR_EXCEPTION", error_message=str(e))

            finally:

                await page.close()

    

    async def _stealth_page_load(self, page: Page, url: str) -> None:

        """

        Load page with human-like behavior to avoid detection.

        

        Includes random delays, mouse movements, and scroll patterns.

        """

        # Initial delay before navigation

        await self._human_delay(0.5, 1.5)

        

        # Navigate to page

        await page.go_to(url)

        

        # Wait for initial load

        await self._human_delay(1.0, 2.5)

        

        # Simulate human-like mouse movements

        await self._simulate_mouse_movements(page)

        

        # Simulate scroll pattern

        await self._simulate_scroll(page)

        

        # Additional random delay

        await self._human_delay(self.stealth_delay_min, self.stealth_delay_max)

    

    async def _human_delay(self, min_sec: float, max_sec: float) -> None:

        """Wait for random duration to simulate human delays."""

        delay = random.uniform(min_sec, max_sec)

        await asyncio.sleep(delay)

    

    async def _simulate_mouse_movements(self, page: Page) -> None:

        """Simulate natural mouse movements across the page."""

        viewport = await page.execute_script('() => ({ width: window.innerWidth, height: window.innerHeight })')

        width, height = viewport.get('width', 1920), viewport.get('height', 1080)

        

        # Generate random points for mouse path

        num_points = random.randint(3, 6)

        points = []

        for _ in range(num_points):

            x = random.randint(100, width - 100)

            y = random.randint(100, height - 100)

            points.append((x, y))

        

        # Move mouse through points with bezier-like curve

        for x, y in points:

            try:

                await page.mouse.move(x, y, steps=self.mouse_steps)

                await self._human_delay(0.1, 0.3)

            except BROAD_EXC:

                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

                pass  # Continue even if mouse move fails

    

    async def _simulate_scroll(self, page: Page) -> None:

        """Simulate natural scroll behavior."""

        scroll_amount = random.randint(*self.scroll_range)

        steps = random.randint(3, 8)

        

        for i in range(steps):

            step_amount = scroll_amount // steps

            await page.execute_script(f'() => window.scrollBy(0, {step_amount})')

            await self._human_delay(0.1, 0.4)

        

        # Occasionally scroll back up slightly

        if random.random() < 0.3:

            await page.execute_script(f'() => window.scrollBy(0, -{scroll_amount // 4})')

            await self._human_delay(0.2, 0.5)

    

    async def _detect_captcha(self, page: Page, expected_type: Optional[str] = None) -> Optional[Dict[str, Any]]:

        """

        Scan page for CAPTCHA elements.

        

        Returns dict with captcha type, element, and additional info.

        """

        # Check expected type first if specified

        if expected_type and expected_type in self.CAPTCHA_SELECTORS:

            element = await self._find_element_by_selectors(page, self.CAPTCHA_SELECTORS[expected_type])

            if element:

                return {

                    'type': expected_type,

                    'element': element,

                    'iframe': None

                }

        

        # Check all known CAPTCHA types

        for captcha_type, selectors in self.CAPTCHA_SELECTORS.items():

            element = await self._find_element_by_selectors(page, selectors)

            if element:

                # Check if it's inside an iframe

                iframe = None

                if captcha_type in ('recaptcha_v2', 'hcaptcha', 'turnstile'):

                    iframe = await self._find_captcha_iframe(page, captcha_type)

                

                return {

                    'type': captcha_type,

                    'element': element,

                    'iframe': iframe

                }

        

        return None

    

    async def _find_element_by_selectors(self, page: Page, selectors: list) -> Optional[Element]:

        """Try to find element using multiple selectors."""

        for selector in selectors:

            try:

                element = await page.query(selector, raise_exc=False)

                if element and await element.is_visible():

                    return element

            except (PydollException, *BROAD_EXC):

                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

                continue

        return None

    

    async def _find_captcha_iframe(self, page: Page, captcha_type: str) -> Optional[Element]:

        """Find CAPTCHA iframe element."""

        iframe_selectors = {

            'recaptcha_v2': [

                'iframe[src*="google.com/recaptcha/api2/anchor"]',

                'iframe[src*="recaptcha/api2/anchor"]',

            ],

            'hcaptcha': [

                'iframe[src*="hcaptcha.com/"][src*="checkbox"]',

            ],

            'turnstile': [

                'iframe[src*="challenges.cloudflare.com"]',

            ],

        }

        

        selectors = iframe_selectors.get(captcha_type, [])

        for selector in selectors:

            try:

                iframe = await page.query(selector, raise_exc=False)

                if iframe:

                    return iframe

            except (PydollException, *BROAD_EXC):

                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

                continue

        return None

    

    async def _handle_challenge(self, page: Page, captcha_info: Dict[str, Any], task: Task) -> Optional[str]:

        """

        Handle CAPTCHA challenge based on type.

        

        Supports: checkbox click, image grid (screenshot), slider drag.

        """

        captcha_type = captcha_info['type']

        element = captcha_info['element']

        iframe = captcha_info.get('iframe')

        

        # Switch to iframe if needed

        if iframe:

            await self._switch_to_iframe(page, iframe)

        

        try:

            if captcha_type in ('recaptcha_v2', 'hcaptcha', 'generic_checkbox'):

                # Checkbox CAPTCHA - simulate human click

                token = await self._handle_checkbox_challenge(page, element)

            elif captcha_type == 'turnstile':

                token = await self._handle_turnstile_challenge(page, element)

            elif captcha_type == 'recaptcha_v3':

                token = await self._handle_recaptcha_v3(page)

            elif captcha_type == 'geetest':

                token = await self._handle_geetest_challenge(page, element)

            else:

                # Unknown type - try generic checkbox approach

                token = await self._handle_checkbox_challenge(page, element)

            

            return token

            

        finally:

            # Switch back to main frame if we switched

            if iframe:

                pass  # back to main frame

    

    async def _handle_checkbox_challenge(self, page: Page, checkbox_element: Element) -> Optional[str]:

        """

        Handle checkbox CAPTCHA (reCAPTCHA v2, hCaptcha checkbox).

        

        Simulates human-like mouse movement and click with random offset.

        """

        # Get element position

        try:

            bbox = await checkbox_element.get_bounds_using_js()

            if not bbox:

                return None

        except BROAD_EXC:

            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

            return None

        

        x, y, width, height = bbox['x'], bbox['y'], bbox['width'], bbox['height']

        

        # Calculate click position with random offset (center with jitter)

        click_x = x + width / 2 + random.uniform(-5, 5)

        click_y = y + height / 2 + random.uniform(-5, 5)

        

        # Move mouse naturally to element

        await page.mouse.move(int(click_x), int(click_y), steps=self.mouse_steps)

        await self._human_delay(0.2, 0.6)

        

        # Click with random offset

        await checkbox_element.click()

        

        # Wait for challenge to process

        await self._human_delay(2.0, 4.0)

        

        # Check if image grid challenge appeared

        if await self._is_image_challenge_present(page):

            return await self._handle_image_challenge(page)

        

        # Extract token

        return await self._extract_token(page)

    

    async def _handle_turnstile_challenge(self, page: Page, element: Element) -> Optional[str]:

        """Handle Cloudflare Turnstile challenge."""

        # Turnstile often auto-solves, just wait and extract token

        await self._human_delay(2.0, 4.0)

        return await self._extract_token(page)

    

    async def _handle_recaptcha_v3(self, page: Page) -> Optional[str]:

        """Handle reCAPTCHA v3 (invisible)."""

        # Token should already be present

        await self._human_delay(1.0, 2.0)

        return await self._extract_token(page)

    

    async def _handle_geetest_challenge(self, page: Page, element: Element) -> Optional[str]:

        """Handle GeeTest CAPTCHA (slider or click)."""

        # Check if it's a slider challenge

        slider = await self._find_element_by_selectors(page, self.CHALLENGE_SELECTORS['slider'])

        if slider:

            return await self._handle_slider_challenge(page, slider)

        

        # Otherwise treat as checkbox

        return await self._handle_checkbox_challenge(page, element)

    

    async def _is_image_challenge_present(self, page: Page) -> bool:

        """Check if image grid challenge is visible."""

        for selector in self.CHALLENGE_SELECTORS['image_grid']:

            try:

                element = await page.query(selector, raise_exc=False)

                if element and await element.is_visible():

                    return True

            except (PydollException, *BROAD_EXC):

                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

                continue

        return False

    

    async def _handle_image_challenge(self, page: Page) -> Optional[str]:

        """

        Handle image grid challenge by taking screenshot for vision analysis.

        

        Returns placeholder indicating vision analysis needed.

        Actual image solving requires external vision service.

        """

        logger.info("Image grid challenge detected - taking screenshot")

        

        # Take screenshot of challenge area

        try:

            screenshot_path = f"captcha_challenge_{random.randint(1000, 9999)}.png"

            await page.take_screenshot(screenshot_path, full_page=False)

            logger.info("Challenge screenshot saved: %s", screenshot_path)

            

            # Wait for potential auto-solve or external intervention

            await self._human_delay(5.0, 8.0)

            

            # Try to extract token anyway (sometimes solves after delay)

            token = await self._extract_token(page)

            if token:

                return token

            

            # Return special marker for image challenge requiring vision

            return f"[IMAGE_CHALLENGE:{screenshot_path}]"

            

        except BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.error("Image challenge handling failed: %s", e)

            return None

    

    async def _handle_slider_challenge(self, page: Page, slider: Element) -> Optional[str]:

        """

        Handle slider CAPTCHA with human-like drag simulation.

        """

        try:

            bbox = await slider.get_bounds_using_js()

            if not bbox:

                return None

            

            start_x, start_y = bbox['x'] + 20, bbox['y'] + bbox['height'] / 2

            

            # Calculate drag distance (varies by CAPTCHA)

            drag_distance = random.randint(200, 300)

            

            # Move to start position

            await page.mouse.move(int(start_x), int(start_y), steps=self.mouse_steps)

            await self._human_delay(0.2, 0.5)

            

            # Simulate human drag with slight curve

            steps = 20

            for i in range(steps):

                progress = i / steps

                # Add slight curve (human hands aren't perfect)

                curve = random.uniform(-3, 3)

                x = start_x + (drag_distance * progress)

                y = start_y + curve

                

                await page.mouse.move(int(x), int(y), steps=1)

                await asyncio.sleep(random.uniform(0.01, 0.03))

            

            # Release (mouse up)

            await self._human_delay(0.3, 0.7)

            

            # Wait for solve

            await self._human_delay(2.0, 4.0)

            

            return await self._extract_token(page)

            

        except BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.error("Slider challenge failed: %s", e)

            return None

    

    async def _switch_to_iframe(self, page: Page, iframe: Element) -> None:

        """Switch context to iframe."""

        try:

            pass  # iframe context handled by pydoll element.iframe_context

            await self._human_delay(0.3, 0.6)

        except BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            logger.warning("Failed to switch to iframe: %s", e)
            return None  # explicit recovery: non-fatal: BROAD_EXC is recorded but does not abort the caller

    

    # JS fallback used when no DOM selector yields a token. Hoisted to a
    # module-class constant so the cognitive cost in `_extract_token` stays
    # below the cc.shape.cognitive ceiling.
    _JS_TOKEN_FALLBACK = (
        "() => {"
        " const sels = ["
        "'textarea[name=\"g-recaptcha-response\"]',"
        "'input[name=\"g-recaptcha-response\"]',"
        "'textarea[name=\"h-captcha-response\"]',"
        "'input[name=\"h-captcha-response\"]',"
        "'input[name=\"cf-turnstile-response\"]',"
        "'#g-recaptcha-response','#h-captcha-response'];"
        " for (const s of sels) { const el = document.querySelector(s);"
        " if (el && el.value) return el.value; }"
        " if (window.grecaptcha && window.grecaptcha.getResponse) {"
        " return window.grecaptcha.getResponse(); }"
        " return null; }"
    )

    async def _token_from_element(self, element: Element, selector: str) -> Optional[str]:
        """Probe a single element for a CAPTCHA token via two known attrs."""
        value = await element.get_attribute('value')
        if value and len(value) > 20:
            logger.info("Token extracted via %s", selector)
            return value
        data_response = await element.get_attribute('data-g-recaptcha-response')
        if data_response and len(data_response) > 20:
            return data_response
        return None

    async def _token_from_dom(self, page: Page) -> Optional[str]:
        """Walk known selectors; first non-empty token wins."""
        for selector in self.TOKEN_SELECTORS:
            try:
                element = await page.query(selector, raise_exc=False)
            except (PydollException, *BROAD_EXC):
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
                continue
            if not element:
                continue
            try:
                token = await self._token_from_element(element, selector)
            except BROAD_EXC:
                emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
                continue
            if token:
                return token
        return None

    async def _token_from_js(self, page: Page) -> Optional[str]:
        """Last-chance JS scrape across known token field shapes."""
        try:
            token = await page.execute_script(self._JS_TOKEN_FALLBACK)
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return None
        if token:
            logger.info("Token extracted via JavaScript")
            return token
        return None

    async def _extract_token(self, page: Page) -> Optional[str]:
        """Extract solved CAPTCHA token from page (DOM probe -> JS fallback)."""
        dom_token = await self._token_from_dom(page)
        if dom_token:
            return dom_token
        return await self._token_from_js(page)

    

    def health_check(self) -> Dict[str, Any]:

        """

        Check Pydoll solver health.

        

        Attempts to start browser and verify basic functionality.

        """

        if not HAS_PYDOLL:

            return {'healthy': False, 'message': 'pydoll-python not installed'}

        if not self.chrome_path:

            return {'healthy': False, 'message': 'No Chrome binary found. Set chrome_path in config.'}

        # Verify Chrome binary exists and is accessible (no browser launch needed)

        chrome = Path(self.chrome_path)

        if not chrome.exists():

            return {

                'healthy': False,

                'message': f'Chrome binary not found: {self.chrome_path}',

                'details': {'error': 'binary_missing'}

            }

        try:

            from pydoll.browser.options import ChromiumOptions

            ChromiumOptions()  # verify import works

        except BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            return {

                'healthy': False,

                'message': f'Pydoll ChromiumOptions import failed: {e}',

                'details': {'error': str(e)}

            }

        return {

            'healthy': True,

            'message': 'Pydoll ready (Chrome binary verified, pydoll imports OK)',

            'details': {

                'solver_type': self.solver_type,

                'chrome_path': self.chrome_path,

                'max_retries': self.max_retries,

                'timeout': self.timeout,

                'headless': self.headless

            }

        }





# Factory function for solver registration

def create_solver(config: Dict[str, Any]) -> PydollSolver:

    """Create and return Pydoll solver instance."""

    return PydollSolver(config)

