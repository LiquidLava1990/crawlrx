"""

HeadlessX/Camoufox V2 solver for challenge resolution service.



Uses Camoufox Firefox engine with C++ binary-level Canvas/WebGL/AudioContext

fingerprint spoofing. Claims 0% detection rate on Pixelscan, passes CreepJS + Sannysoft.

WebRTC leak protection built-in.



Install: pip install camoufox[geoip]

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
import os


import random

import time

from pathlib import Path

from typing import Any, Dict, List, Optional
from dataclasses import dataclass





try:
    from camoufox.async_api import AsyncCamoufox

    HAS_CAMOUFOX = True

except ImportError:

    AsyncCamoufox = None

    HAS_CAMOUFOX = False



from ..solver_base import BaseSolver, SolverTimeoutError
from ._solver_helpers import BROAD_EXC as _BROAD_EXC  # narrow safety-net catch tuple


from ..models import Task, TaskResult





# Selector dictionaries used by `_check_for_captcha` to identify the
# challenge family on a page.
_CAPTCHA_SELECTORS: Dict[str, list] = {
    "recaptcha_v2": [
        ".g-recaptcha",
        "[data-sitekey]",
        'iframe[src*="recaptcha"]',
        "#recaptcha",
    ],
    "recaptcha_v3": [
        'script[src*="recaptcha/api.js"]',
    ],
    "hcaptcha": [
        ".h-captcha",
        "[data-hcaptcha-sitekey]",
        'iframe[src*="hcaptcha"]',
    ],
    "cloudflare": [
        "#cf-turnstile",
        ".cf-turnstile",
        'iframe[src*="challenges.cloudflare"]',
        "#challenge-running",
    ],
    "datadome": [
        ".datadome-captcha",
        'iframe[src*="datadome"]',
    ],
    "perimeterx": [
        "#px-captcha",
        ".px-captcha",
    ],
}


@dataclass
class _AttemptOutcome:
    """Result of one HeadlessX solve attempt -- either a token or an error."""
    token: Optional[str] = None
    error: Optional[str] = None


def _stealth_headers(locale_raw: str) -> Dict[str, str]:
    """Return the canonical stealth header bundle keyed off the locale."""
    return {
        "Accept-Language": locale_raw.replace("_", "-"),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


class HeadlessXSolver(BaseSolver):

    """

    HeadlessX/Camoufox V2 challenge handler.

    

    Multi-strategy approach:

    1. Camoufox stealth - Firefox-based with C++ binary-level fingerprint spoofing

    2. Fingerprint rotation - randomize screen, timezone, locale, platform per session

    3. Anti-detection first - sites don't detect automation, CAPTCHAs often don't trigger

    4. CAPTCHA fallback - human-like timing if CAPTCHA appears

    5. Cookie persistence - build reputation over time

    """

    

    # Common screen resolutions for rotation

    SCREEN_RESOLUTIONS = [

        (1920, 1080), (1366, 768), (1440, 900), (1536, 864),

        (1280, 720), (1600, 900), (1280, 800), (1680, 1050),

        (2560, 1440), (3840, 2160), (2560, 1600), (1920, 1200)

    ]

    

    # Timezones for rotation

    TIMEZONES = [

        'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',

        'America/Toronto', 'America/Vancouver', 'Europe/London', 'Europe/Paris',

        'Europe/Berlin', 'Europe/Madrid', 'Asia/Tokyo', 'Asia/Shanghai',

        'Australia/Sydney', 'Pacific/Auckland'

    ]

    

    # Locales for rotation

    LOCALES = [

        'en-US', 'en-GB', 'en-CA', 'en-AU', 'fr-FR', 'de-DE', 'es-ES',

        'it-IT', 'pt-BR', 'ja-JP', 'zh-CN', 'ko-KR', 'nl-NL', 'pl-PL'

    ]

    

    # Platforms

    PLATFORMS = ['win32', 'linux', 'darwin']

    

    def __init__(self, config: Dict[str, Any]) -> None:

        super().__init__(config)

        self.solver_id = "headlessx"

        self.max_retries = config.get('max_retries', 3)

        self.cookie_dir = Path(config.get('cookie_dir', os.environ.get(
            'CRAWLRX_HEADLESSX_COOKIE_DIR', 'cookies/headlessx')))

        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        self.human_like = config.get('human_like', True)

        self.fingerprint_rotation = config.get('fingerprint_rotation', True)

        self._browser = None

        self._context = None

        

    @property

    def solver_type(self) -> str:

        return "headlessx"

    

    def _get_fingerprint(self, task: Task) -> Dict[str, Any]:

        """Generate randomized fingerprint for this session."""

        if not self.fingerprint_rotation:

            return {}

        

        # Use task hash for deterministic but varied fingerprints per site

        task_seed = hash(f"{task.site_key}:{task.domain}:{time.time()}")

        random.seed(task_seed)

        

        screen = random.choice(self.SCREEN_RESOLUTIONS)

        fingerprint = {

            'screen': {'width': screen[0], 'height': screen[1]},

            'viewport': {'width': screen[0], 'height': screen[1] - random.randint(80, 150)},

            'timezone': random.choice(self.TIMEZONES),

            'locale': random.choice(self.LOCALES),

            'platform': random.choice(self.PLATFORMS),

            'color_scheme': random.choice(['light', 'dark']),

        }

        

        # Reset random seed

        random.seed()

        return fingerprint

    

    def _get_cookie_path(self, domain: str) -> Path:

        """Get path for cookie storage per domain."""

        safe_domain = domain.replace('/', '_').replace(':', '_')

        return self.cookie_dir / f"{safe_domain}.json"

    

    def _load_cookies(self, domain: str) -> List[Dict]:

        """Load saved cookies for domain."""

        cookie_path = self._get_cookie_path(domain)

        if cookie_path.exists():

            try:

                with open(cookie_path, 'r', encoding='utf-8-sig', errors='replace') as f:

                    return json.load(f)

            except _BROAD_EXC:

                emit(ErrCode.FILE_READ_ERROR, "unhandled")

                return []

        return []

    

    def _save_cookies(self, domain: str, cookies: List[Dict]):

        """Save cookies for domain."""

        cookie_path = self._get_cookie_path(domain)

        try:

            with open(cookie_path, 'w', encoding='utf-8') as f:

                json.dump(cookies, f)

        except _BROAD_EXC as e:

            emit(ErrCode.FILE_WRITE_FAIL, str(e))

            print(f"[HeadlessX] Failed to save cookies for {domain}: {e}")

    

    async def _human_delay(self, min_ms: int = 100, max_ms: int = 300):

        """Human-like delay between actions."""

        if self.human_like:

            delay = random.uniform(min_ms, max_ms) / 1000

            await asyncio.sleep(delay)

    

    async def _gaussian_mouse_move(self, page, x: int, y: int):

        """Move mouse with gaussian curve for human-like behavior."""

        if not self.human_like:

            await page.mouse.move(x, y)

            return

        

        # Get current position

        current_pos = await page.evaluate('() => ({ x: window.mouseX || 0, y: window.mouseY || 0 })')

        start_x = current_pos.get('x', 0)

        start_y = current_pos.get('y', 0)

        

        # Generate curve points

        steps = random.randint(8, 15)

        for i in range(steps):

            t = (i + 1) / steps

            # Gaussian-like easing

            t = t * (2 - t)  # ease-out

            cur_x = int(start_x + (x - start_x) * t + random.randint(-5, 5))

            cur_y = int(start_y + (y - start_y) * t + random.randint(-5, 5))

            await page.mouse.move(cur_x, cur_y)

            await asyncio.sleep(random.uniform(0.01, 0.03))

    

    async def _click_element(self, page, selector: str):

        """Click element with human-like behavior."""

        element = await page.query_selector(selector)

        if not element:

            return False

        

        box = await element.bounding_box()

        if not box:

            return False

        

        # Click with slight offset from center

        x = box['x'] + box['width'] / 2 + random.randint(-5, 5)

        y = box['y'] + box['height'] / 2 + random.randint(-5, 5)

        

        await self._gaussian_mouse_move(page, int(x), int(y))

        await self._human_delay(50, 150)

        await page.mouse.click(int(x), int(y))

        return True

    

    async def _check_for_captcha(self, page) -> Optional[str]:
        """Check if CAPTCHA is present and identify type."""
        for captcha_type, selectors in _CAPTCHA_SELECTORS.items():
            if await self._any_visible(page, selectors):
                return captcha_type
        return None

    async def _any_visible(self, page, selectors: list) -> bool:
        """Return True iff any of the selectors resolves to a visible element."""
        for selector in selectors:
            element = await self._safe_query_selector(page, selector)
            if element is None:
                continue
            if await self._safe_is_visible(element):
                return True
        return False

    async def _safe_query_selector(self, page, selector: str):
        """Wrap page.query_selector in a swallow handler -- callers must accept None."""
        try:
            return await page.query_selector(selector)
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return None

    async def _safe_is_visible(self, element) -> bool:
        """Wrap element.is_visible in a swallow handler -- callers get False on error."""
        try:
            return await element.is_visible()
        except _BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
            return False

    

    async def _solve_recaptcha_v2(self, page, task: Task) -> Optional[str]:
        """Attempt to solve reCAPTCHA v2."""
        try:
            await self._click_recaptcha_checkbox(page)
            return await self._poll_recaptcha_token(page)
        except _BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            print(f"[HeadlessX] reCAPTCHA v2 solve error: {e}")
            return None

    async def _click_recaptcha_checkbox(self, page) -> None:
        """Click the reCAPTCHA v2 checkbox in any anchor iframe on the page."""
        anchors = await page.query_selector_all(
            'iframe[src*="recaptcha/api2/anchor"]'
        )
        for frame_element in anchors:
            frame = await frame_element.content_frame()
            if frame is None:
                continue
            checkbox = await frame.query_selector(".recaptcha-checkbox-border")
            if checkbox is None:
                continue
            await self._human_delay(200, 500)
            await checkbox.click()
            await self._human_delay(1000, 2000)

    async def _poll_recaptcha_token(self, page, *, max_seconds: int = 30) -> Optional[str]:
        """Poll the page for a reCAPTCHA token; return it once present."""
        script = '() => { return grecaptcha ? grecaptcha.getResponse() : ""; }'
        for _ in range(max_seconds):
            await asyncio.sleep(1)
            token = await page.evaluate(script)
            if token:
                return token
        return None

    

    async def _solve_hcaptcha(self, page, task: Task) -> Optional[str]:

        """Attempt to solve hCaptcha."""

        try:

            # hCaptcha often bypasses with good stealth

            # Wait for token

            for _ in range(30):

                await asyncio.sleep(1)

                token = await page.evaluate('() => { return hcaptcha ? hcaptcha.getResponse() : ""; }')

                if token:

                    return token

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            print(f"[HeadlessX] hCaptcha solve error: {e}")

        

        return None

    

    async def _solve_cloudflare(self, page, task: Task) -> Optional[str]:

        """Attempt to solve Cloudflare challenge."""

        try:

            # Cloudflare often passes with good stealth

            # Wait for turnstile token or redirect

            for _ in range(30):

                await asyncio.sleep(1)

                

                # Check for turnstile token

                token = await page.evaluate('''() => {

                    const input = document.querySelector('input[name="cf-turnstile-response"]');

                    return input ? input.value : "";

                }''')

                if token:

                    return token

                

                # Check if challenge completed (redirected)

                if 'challenges.cloudflare' not in page.url:

                    return "cf_passed"

                    

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            print(f"[HeadlessX] Cloudflare solve error: {e}")

        

        return None

    

    async def _attempt_captcha_solve(self, page, captcha_type: str, task: Task) -> Optional[str]:

        """Attempt to solve detected CAPTCHA."""

        print(f"[HeadlessX] CAPTCHA detected: {captcha_type}")

        

        solvers = {

            'recaptcha_v2': self._solve_recaptcha_v2,

            'recaptcha_v3': self._solve_recaptcha_v2,

            'hcaptcha': self._solve_hcaptcha,

            'cloudflare': self._solve_cloudflare,

        }

        

        solver = solvers.get(captcha_type)

        if solver:

            return await solver(page, task)

        

        return None

    

    async def _solve_attempt(
        self,
        task: Task,
        attempt: int,
        start_time: float,
    ) -> "_AttemptOutcome":
        """Run one full Camoufox-launch / navigate / detect / solve cycle."""
        elapsed = time.time() - start_time
        if elapsed >= self.timeout:
            raise SolverTimeoutError(self.timeout)
        attempt_timeout = min(self.timeout - elapsed, 40)
        print(f"[HeadlessX] Attempt {attempt}/{self.max_retries} for {task.domain}")
        fingerprint = self._get_fingerprint(task)
        camoufox_config = self._build_camoufox_config(fingerprint)
        async with AsyncCamoufox(**camoufox_config) as browser:
            context = await self._open_context(browser, fingerprint)
            try:
                page = await context.new_page()
                await self._prime_page(page, context, task, fingerprint)
                await self._navigate(page, task, attempt_timeout)
                token = await self._check_or_solve(page, task)
                cookies = await context.cookies()
                self._save_cookies(task.domain, cookies)
                if token is not None:
                    return _AttemptOutcome(token=token)
                return _AttemptOutcome(error="CAPTCHA not solved")
            finally:
                try:
                    await context.close()
                except _BROAD_EXC:
                    emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")

    def _build_camoufox_config(self, fingerprint: Optional[dict]) -> Dict[str, Any]:
        """Build the Camoufox launch kwargs from a fingerprint dict."""
        config: Dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if fingerprint:
            config.update(
                {
                    "screen": fingerprint.get("screen"),
                    "viewport": fingerprint.get("viewport"),
                    "timezone": fingerprint.get("timezone"),
                    "locale": fingerprint.get("locale"),
                    "color_scheme": fingerprint.get("color_scheme"),
                }
            )
        return config

    async def _open_context(self, browser, fingerprint: Optional[dict]):
        """Open a Camoufox browser context aligned with the fingerprint."""
        return await browser.new_context(
            viewport=fingerprint.get("viewport") if fingerprint else None,
            locale=fingerprint.get("locale") if fingerprint else "en-US",
            timezone_id=(
                fingerprint.get("timezone")
                if fingerprint
                else "America/New_York"
            ),
        )

    async def _prime_page(
        self, page, context, task: Task, fingerprint: Optional[dict],
    ) -> None:
        """Apply cookies + stealth headers before navigation."""
        cookies = self._load_cookies(task.domain)
        if cookies:
            await context.add_cookies(cookies)
            print(f"[HeadlessX] Loaded {len(cookies)} cookies for {task.domain}")
        locale_raw = fingerprint.get("locale", "en-US") if fingerprint else "en-US"
        await page.set_extra_http_headers(_stealth_headers(locale_raw))

    async def _navigate(self, page, task: Task, attempt_timeout: float) -> None:
        """Navigate to the task URL with a stealth pre-delay."""
        await self._human_delay(200, 500)
        url = task.url or f"https://{task.domain}"
        response = await page.goto(
            url,
            wait_until="networkidle",
            timeout=attempt_timeout * 1000,
        )
        if response:
            print(f"[HeadlessX] Navigated to {url} - Status: {response.status}")
        await self._human_delay(500, 1000)

    async def _check_or_solve(self, page, task: Task) -> Optional[str]:
        """Detect captcha; return stealth-pass / solve-token / None."""
        captcha_type = await self._check_for_captcha(page)
        if not captcha_type:
            print("[HeadlessX] No CAPTCHA detected - stealth successful")
            return "stealth_pass"
        print(f"[HeadlessX] CAPTCHA detected: {captcha_type}")
        return await self._attempt_captcha_solve(page, captcha_type, task)

    async def solve(self, task: Task) -> TaskResult:
        """Solve CAPTCHA using HeadlessX/Camoufox stealth browser."""
        if not HAS_CAMOUFOX:
            return TaskResult(
                success=False,
                error_code="ERROR_NO_CAMOUFOX",
                error_message="camoufox not installed. Run: pip install camoufox[geoip]",
            )

        start_time = time.time()
        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                outcome = await self._solve_attempt(task, attempt, start_time)
            except asyncio.TimeoutError:
                emit(ErrCode.TIMEOUT_ERROR, "unhandled")
                last_error = f"Timeout on attempt {attempt}"
                print(f"[HeadlessX] {last_error}")
            except _BROAD_EXC as e:
                emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
                last_error = str(e)
                print(f"[HeadlessX] Error on attempt {attempt}: {e}")
            else:
                if outcome.token is not None:
                    return TaskResult(success=True, token=outcome.token)
                last_error = outcome.error or last_error
            if attempt < self.max_retries:
                await asyncio.sleep(random.uniform(1, 3))

        return TaskResult(
            success=False,
            error_code="ERROR_MAX_RETRIES",
            error_message=last_error or "All attempts failed",
        )

    

    def health_check(self) -> Dict[str, Any]:

        """Check solver health by verifying imports -- no browser launch."""

        try:

            from camoufox.async_api import AsyncCamoufox as _check  # noqa: F401

            return {

                'healthy': True,

                'message': 'Camoufox module available',

                'solver_type': self.solver_type

            }

                    

        except ImportError as e:

            return {

                'healthy': False,

                'message': f'Camoufox not installed: {e}. Run: pip install camoufox[geoip]',

                'solver_type': self.solver_type

            }

        except _BROAD_EXC as e:

            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))

            return {

                'healthy': False,

                'message': f'Health check error: {str(e)}',

                'solver_type': self.solver_type

            }





# Factory function for service integration

def create_solver(config: Dict[str, Any]) -> HeadlessXSolver:

    """Create a HeadlessX solver instance."""

    return HeadlessXSolver(config)

