"""DOM-traversal helpers for the recaptcha v2 grid solver.

Defines ``GridDomMixin`` -- the Playwright frame finders, prompt
extractor, grid screenshot capture, and the button-click helpers
used by the orchestrator's solve loop.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from typing import Any, Optional

from PIL import Image
from playwright.async_api import Frame, Page

from ..solver_base import SolverError

from ._solver_helpers import BROAD_EXC


def _grid_screenshot_to_pil(screenshot_bytes: bytes) -> Image.Image:
    """Decode a Playwright screenshot byte-string to a PIL ``Image``."""
    return Image.open(io.BytesIO(screenshot_bytes))


logger = logging.getLogger(__name__)


_GRID_TABLE_SELECTOR = (
    "table.rc-imageselect-table-33, table.rc-imageselect-table-44"
)


# --- Free helpers -----------------------------------------------------------

def _frame_url_or_empty(frame: Frame) -> str:
    """Return ``frame.url`` swallowing access errors (returns empty)."""
    try:
        return frame.url
    except BROAD_EXC as exc:
        logger.debug("Frame URL access failed: %s", exc)
        return ""  # explicit recovery: malformed frame skipped


def _is_recaptcha_anchor(frame: Frame) -> bool:
    """True when this Playwright frame is the recaptcha checkbox iframe."""
    url = _frame_url_or_empty(frame)
    return "recaptcha" in url and "anchor" in url


def _is_recaptcha_bframe(frame: Frame) -> bool:
    """True when this Playwright frame is the recaptcha challenge iframe."""
    url = _frame_url_or_empty(frame)
    return "recaptcha" in url and "bframe" in url


# --- Mixin contributed to ``ReCaptchaGridSolver`` ---------------------------

class GridDomMixin:
    """Frame finders, prompt extraction, screenshot, button clicks."""

    @staticmethod
    def _find_recaptcha_frame(page: Page) -> Optional[Frame]:
        """Locate the recaptcha checkbox iframe (``anchor`` URL)."""
        for frame in page.frames:
            if _is_recaptcha_anchor(frame):
                return frame
        return None

    @staticmethod
    def _first_challenge_frame(page: Page) -> Optional[Frame]:
        """One sweep over page frames for the bframe iframe."""
        for frame in page.frames:
            if _is_recaptcha_bframe(frame):
                return frame
        return None

    async def _wait_for_challenge_frame(
        self, page: Page, timeout: int = 10,
    ) -> Optional[Frame]:
        """Poll ``page.frames`` for up to ``timeout`` s for the challenge iframe."""
        start = time.time()
        while time.time() - start < timeout:
            hit = self._first_challenge_frame(page)
            if hit is not None:
                return hit
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _read_prompt_text(
        frame: Frame, selector: str,
    ) -> Optional[str]:
        """Probe one selector for a 'select all ...' instruction string."""
        try:
            element = await frame.wait_for_selector(selector, timeout=3000)
        except BROAD_EXC as exc:
            logger.debug("Prompt selector '%s' failed: %s", selector, exc)
            return None  # explicit recovery: try next selector
        if not element:
            return None
        try:
            text = await element.inner_text()
        except BROAD_EXC as exc:
            logger.debug("inner_text on '%s' failed: %s", selector, exc)
            return None  # explicit recovery: try next selector
        if text and "select all" in text.lower():
            return text.strip()
        return None

    @staticmethod
    async def _scrape_prompt_from_body(frame: Frame) -> Optional[str]:
        """Last-resort regex over the iframe body text."""
        try:
            body_text = await frame.evaluate(
                "() => document.body.innerText",
            )
        except BROAD_EXC as exc:
            logger.debug("body.innerText scrape failed: %s", exc)
            return None  # explicit recovery: body unavailable
        match = re.search(
            r"select all.*?with\s+([\w\s]+)", body_text, re.IGNORECASE,
        )
        if match:
            return f"Select all images with {match.group(1).strip()}"
        return None

    async def _get_challenge_prompt(self, frame: Frame) -> Optional[str]:
        """Extract the 'select all images with X' prompt text."""
        try:
            selectors = (
                ".rc-imageselect-instructions",
                ".rc-imageselect-desc-wrapper",
                "#rc-imageselect-target ~ div",
                '[class*="instructions"]',
            )
            for selector in selectors:
                hit = await self._read_prompt_text(frame, selector)
                if hit:
                    return hit
            return await self._scrape_prompt_from_body(frame)
        except BROAD_EXC as exc:
            logger.error("Error getting prompt: %s", exc)
            return None  # explicit recovery: prompt scrape non-fatal

    @staticmethod
    async def _detect_grid_size(frame: Frame) -> int:
        """Detect 3x3 vs 4x4 grid layout (default 3x3)."""
        try:
            cells = await frame.locator(
                "table.rc-imageselect-table-33 td, "
                "table.rc-imageselect-table-44 td",
            ).count()
            if cells == 16:
                return 4
            return 3
        except BROAD_EXC as exc:
            logger.debug("Grid-size detection failed: %s", exc)
            return 3  # explicit recovery: assume 3x3

    @staticmethod
    async def _capture_grid(
        frame: Frame, _grid_size: int,
    ) -> Image.Image:
        """Take a screenshot of the recaptcha image grid table."""
        try:
            table = await frame.wait_for_selector(
                _GRID_TABLE_SELECTOR, timeout=5000,
            )
            if not table:
                raise SolverError(
                    "ERROR_CAPTCHA_NOT_FOUND", "Grid table not found",
                )
            screenshot = await table.screenshot()
            return _grid_screenshot_to_pil(screenshot)
        except SolverError:
            raise
        except BROAD_EXC as exc:
            logger.error("Error capturing grid: %s", exc)
            raise SolverError(
                "ERROR_CAPTCHA_UNSOLVABLE",
                f"Grid capture failed: {exc}",
            ) from exc

    @staticmethod
    async def _click_with_jitter(frame: Frame, selector: str, label: str) -> None:
        """Click ``selector`` adding a small random pixel offset."""
        element = await frame.wait_for_selector(selector, timeout=5000)
        if not element:
            raise SolverError(
                "ERROR_CAPTCHA_NOT_FOUND", f"{label} not found",
            )
        box = await element.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2 + random.uniform(-2, 2)
            y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
            await frame.page.mouse.click(x, y)
            logger.info("Clicked %s (jittered)", label)
        else:
            await element.click()
            logger.info("Clicked %s (centered)", label)

    async def _click_checkbox(self, frame: Frame) -> None:
        """Click the recaptcha checkbox with human-like jitter."""
        try:
            await self._click_with_jitter(
                frame, ".recaptcha-checkbox-border", "checkbox",
            )
        except SolverError:
            raise
        except BROAD_EXC as exc:
            logger.error("Failed to click checkbox: %s", exc)
            raise SolverError(
                "ERROR_CAPTCHA_NOT_FOUND",
                f"Checkbox click failed: {exc}",
            ) from exc

    @staticmethod
    async def _click_simple_button(
        frame: Frame, selector: str, label: str,
    ) -> None:
        """Click a verify/refresh-style button (no jitter required)."""
        try:
            button = await frame.wait_for_selector(selector, timeout=3000)
            if button:
                await button.click()
                logger.info("Clicked %s button", label)
        except BROAD_EXC as exc:
            logger.error("Error clicking %s: %s", label, exc)
            raise SolverError(
                "ERROR_CAPTCHA_NOT_FOUND",
                f"{label} click failed: {exc}",
            ) from exc

    async def _click_verify(self, frame: Frame) -> None:
        """Click the recaptcha verify button."""
        await self._click_simple_button(
            frame, "#recaptcha-verify-button", "verify",
        )

    async def _click_refresh(self, frame: Frame) -> None:
        """Click the recaptcha refresh button (request new images)."""
        try:
            button = await frame.wait_for_selector(
                "#recaptcha-reload-button", timeout=3000,
            )
            if button:
                await button.click()
                logger.info("Clicked refresh button")
        except BROAD_EXC as exc:
            logger.error("Error clicking refresh: %s", exc)
            return None  # explicit recovery: refresh non-fatal

    @staticmethod
    async def _has_new_images(frame: Frame) -> bool:
        """Detect 'please select all matching images' style follow-up."""
        try:
            content = await frame.content()
            return "please" in content.lower() and "select" in content.lower()
        except BROAD_EXC as exc:
            logger.debug("has_new_images check failed: %s", exc)
            return False  # explicit recovery: assume no follow-up

    @staticmethod
    async def _extract_token(page: Page) -> Optional[str]:
        """Read ``g-recaptcha-response`` token from the page DOM."""
        try:
            token = await page.evaluate(
                """() => {
                    const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta && ta.value) return ta.value;
                    const byId = document.getElementById('g-recaptcha-response');
                    if (byId && byId.value) return byId.value;
                    return null;
                }""",
            )
            return token if token else None
        except BROAD_EXC as exc:
            logger.error("Error extracting token: %s", exc)
            return None  # explicit recovery: token absent

    @staticmethod
    async def _is_error_visible(frame: Frame) -> bool:
        """Detect visible 'incorrect / try again' error indicator."""
        try:
            err = await frame.wait_for_selector(
                ".rc-imageselect-error-msg, "
                ".rc-imageselect-incorrect-response",
                timeout=1000,
            )
            return err is not None
        except BROAD_EXC as exc:
            logger.debug("error-visible probe failed: %s", exc)
            return False  # explicit recovery: assume no error visible

    @staticmethod
    async def _open_browser_context(p: Any, cdp_port: int) -> tuple[Any, Any]:
        """Connect over CDP, return ``(browser, context)`` handles."""
        logger.info("Connecting to browser on port %s", cdp_port)
        browser = await p.chromium.connect_over_cdp(
            f"http://localhost:{cdp_port}",
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        return browser, context
