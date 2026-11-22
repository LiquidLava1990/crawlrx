"""Page-flow + DOM helpers for the FunCaptcha solver.

Owns every interaction with the Arkose Labs iframe / DOM: detection,
iframe / frame resolution, arrow-button clicking, screenshot capture,
audio download, transcription, and token extraction. Also owns the
audio-challenge ``WhisperTranscriber`` re-export so the orchestrator
stays free of ML imports. Its backend lives in ``_funcaptcha_speech``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Error as PlaywrightError, Frame, Page

if __package__:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC
    from .hcaptcha_viewport import capture_view, measured_box
    from ._funcaptcha_speech import WhisperTranscriber
    from ._browser_audio_download import fetch_browser_audio, write_audio_temp
else:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC
    from hcaptcha_viewport import capture_view, measured_box
    from ._funcaptcha_speech import WhisperTranscriber
    from ._browser_audio_download import fetch_browser_audio, write_audio_temp

FrameOrPage = Union[Frame, Page]

_LOGGER = logging.getLogger(__name__)


# Arkose-specific selectors. Public so the orchestrator can keep using
# them as the canonical detection surface (existing call sites use
# ``self.FC_CONTAINER`` etc. -- the orchestrator re-exports these).
FC_CONTAINER: str = (
    "[data-e2e='verification-iframe'], #arkose, .arkose, .funcaptcha, "
    "iframe[src*='arkoselabs'], iframe[src*='funcaptcha']"
)
FC_IFRAME: str = (
    "iframe[src*='arkoselabs.com'], iframe[src*='funcaptcha.com']"
)
PUZZLE_IMAGE_SELECTOR: str = (
    "[data-test-id='challenge-image'], .challenge-image, "
    "img[src*='challenge'], canvas"
)
AUDIO_BUTTON: str = (
    "[data-test-id='audio-button'], .audio-button, [aria-label*='audio']"
)
AUDIO_PLAY_BUTTON: str = "[data-test-id='play-audio'], .play-audio"
AUDIO_DOWNLOAD_LINK: str = (
    "[data-test-id='audio-link'], .audio-link, a[download]"
)

ARROW_SELECTORS: Dict[str, str] = {
    "left": (
        "[data-test-id='rotate-left'], .rotate-left, [aria-label*='left'], "
        "button[data-direction='left']"
    ),
    "right": (
        "[data-test-id='rotate-right'], .rotate-right, [aria-label*='right'], "
        "button[data-direction='right']"
    ),
    "up": (
        "[data-test-id='rotate-up'], .rotate-up, [aria-label*='up'], "
        "button[data-direction='up']"
    ),
    "down": (
        "[data-test-id='rotate-down'], .rotate-down, [aria-label*='down'], "
        "button[data-direction='down']"
    ),
}

DETECTION_PATTERNS: Tuple[str, ...] = (
    r"arkoselabs\.com",
    r"funcaptcha\.com",
    r"arkose",
    r"FunCaptcha",
    r"fc-token",
    r"verification-token",
)


def url_indicates_funcaptcha(url: str) -> bool:
    """True iff the page URL contains a known FunCaptcha host."""
    return "arkoselabs.com" in url or "funcaptcha.com" in url


def frame_indicates_funcaptcha(frames: list[Frame]) -> bool:
    """True iff any frame URL points at FunCaptcha."""
    for frame in frames:
        frame_url = frame.url
        if "arkoselabs" in frame_url or "funcaptcha" in frame_url:
            return True
    return False


def content_matches_detection(content: str) -> bool:
    """True iff the rendered HTML matches any DETECTION_PATTERN."""
    for pattern in DETECTION_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return True
    return False


async def container_is_present(page: Page) -> bool:
    """True iff any of the FC_CONTAINER selectors is in the DOM."""
    for selector in FC_CONTAINER.split(", "):
        element = await page.query_selector(selector)
        if element:
            return True
    return False


async def detect(page: Page) -> bool:
    """Detect if FunCaptcha/Arkose Labs is present on the page."""
    try:
        if url_indicates_funcaptcha(page.url):
            return True
        content = await page.content()
        if content_matches_detection(content):
            return True
        if frame_indicates_funcaptcha(page.frames):
            return True
        return await container_is_present(page)
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Detection error: %s", exc)
        return False


def resolve_funcaptcha_frame(page: Page) -> FrameOrPage:
    """Return the embedded FunCaptcha frame or ``page`` if not present."""
    for frame in page.frames:
        if "arkoselabs" in frame.url or "funcaptcha" in frame.url:
            return frame
    return page


async def get_funcaptcha_iframe(page: Page) -> Optional[ElementHandle]:
    """Get the FunCaptcha iframe element (or ``None``)."""
    for selector in FC_IFRAME.split(", "):
        iframe = await page.query_selector(selector)
        if iframe:
            return iframe
    return None


async def capture_puzzle(
    frame: FrameOrPage, iframe_element: Optional[ElementHandle] = None,
) -> Optional[bytes]:
    """Crop the visible widget, including instructions, without element capture."""
    owned_element = None
    try:
        page = frame if isinstance(frame, Page) else frame.page
        scope = page.main_frame if isinstance(frame, Page) else frame
        if iframe_element is not None:
            if await iframe_element.content_frame() != scope:
                raise ValueError("FunCaptcha frame changed")
            element = iframe_element
            owner = await element.owner_frame()
            if owner is None:
                raise ValueError("FunCaptcha frame detached")
        elif scope.parent_frame is not None:
            owned_element = await scope.frame_element()
            element, owner = owned_element, scope.parent_frame
        else:
            element = page.locator("#arkose, .arkose, .funcaptcha")
            if await element.count() != 1:
                hostname = (urlsplit(page.url).hostname or "").lower()
                if not any(hostname == d or hostname.endswith("." + d)
                           for d in ("arkoselabs.com", "funcaptcha.com")):
                    raise ValueError("FunCaptcha widget is ambiguous or missing")
                element = page.locator("body")
            owner = page.main_frame
        box = await measured_box(owner, element)
        return await capture_view(page, owner, element, box)
    except PlaywrightError as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, type(exc).__name__)
        _LOGGER.warning("FunCaptcha capture unavailable: %s", type(exc).__name__)
        return None
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Screenshot error: %s", exc)
        return None
    finally:
        if owned_element is not None:
            try:
                await owned_element.dispose()
            except PlaywrightError:
                _LOGGER.debug("FunCaptcha capture handle already detached")


async def click_arrow(frame: FrameOrPage, direction: str) -> None:
    """Click the arrow button for ``direction`` (best-effort)."""
    try:
        selector = ARROW_SELECTORS.get(direction, "")
        if not selector:
            return
        for sel in selector.split(", "):
            button = await frame.query_selector(sel)
            if button:
                await button.click()
                _LOGGER.debug("Clicked %s arrow", direction)
                return
        await frame.evaluate(_arrow_js_fallback(direction))
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Click arrow error: %s", exc)


def _arrow_js_fallback(direction: str) -> str:
    """JS snippet that clicks any button whose aria-label mentions ``direction``."""
    return (
        "() => {"
        "    const buttons = document.querySelectorAll('button');"
        "    for (const btn of buttons) {"
        "        const aria = btn.getAttribute('aria-label') || '';"
        f"        if (aria.toLowerCase().includes('{direction}')) {{"
        "            btn.click();"
        "            return true;"
        "        }"
        "    }"
        "}"
    )


_COMPLETION_TEXT = re.compile(
    r"^\s*(?:verification\s+(?:complete(?:d)?|successful)|"
    r"you\s+(?:are|have\s+been)\s+verified)[.!]?\s*$",
    re.IGNORECASE,
)
_TOKEN_SELECTORS: Tuple[str, ...] = (
    "input[name='fc-token']",
    "input[name='arkose-token']",
    "[data-fc-token]",
    "#fc-token",
    "#arkose-token",
)


async def _frame_has_token_input(
    frame: FrameOrPage, selectors: Tuple[str, ...],
) -> bool:
    """True iff any selector exposes a non-empty token value."""
    for selector in selectors:
        if await _selector_value(frame, selector):
            return True
    return False


async def is_solved(frame: FrameOrPage) -> bool:
    """Observe a provider response or a visible, scoped completion message.

    Raw markup, callback source and empty response fields are not completion.
    A response token still needs verification by the integrating site's server.
    """
    try:
        if await _frame_has_token_input(frame, _TOKEN_SELECTORS):
            return True
        hostname = (urlsplit(frame.url).hostname or "").lower()
        provider_host = any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in ("arkoselabs.com", "funcaptcha.com")
        )
        scope = frame if provider_host else frame.locator("#arkose, .arkose, .funcaptcha")
        messages = scope.get_by_text(_COMPLETION_TEXT)
        for index in range(await messages.count()):
            if await messages.nth(index).is_visible():
                return True
        return False
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return False


_EXTRACT_SELECTORS = _TOKEN_SELECTORS


async def _selector_value(scope: FrameOrPage, selector: str) -> Optional[str]:
    """Read live Arkose response values, not stale HTML default attributes."""
    try:
        input_el = await scope.query_selector(selector)
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        return None
    if not input_el:
        return None
    value = await input_el.evaluate(
        "el => typeof el.value === 'string' ? el.value : el.getAttribute('data-fc-token')",
    )
    if isinstance(value, str) and len(value.strip()) > 10:
        return value.strip()
    return None


async def _scan_token_selectors(scope: FrameOrPage) -> Optional[str]:
    """Walk known token selectors against ``scope``; return first hit."""
    for selector in _EXTRACT_SELECTORS:
        value = await _selector_value(scope, selector)
        if value:
            return value
    return None


async def extract_token(page: Page) -> Optional[str]:
    """Extract the verification token from the page."""
    try:
        value = await _scan_token_selectors(page)
        if value:
            return value
        for frame in page.frames:
            value = await _scan_token_selectors(frame)
            if value:
                return value
        return None
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Token extraction error: %s", exc)
        return None


async def click_audio_button(iframe_page: FrameOrPage) -> None:
    """Click the audio mode button if present (best-effort)."""
    for selector in AUDIO_BUTTON.split(", "):
        audio_btn = await iframe_page.query_selector(selector)
        if audio_btn:
            await audio_btn.click()
            await asyncio.sleep(2)
            return


def unlink_quiet(path: str) -> None:
    """Best-effort temp-file cleanup."""
    try:
        os.unlink(path)
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _click_audio_play(frame: FrameOrPage) -> None:
    """Play the audio so the file is registered on the iframe."""
    for selector in AUDIO_PLAY_BUTTON.split(", "):
        play_btn = await frame.query_selector(selector)
        if play_btn:
            await play_btn.click()
            await asyncio.sleep(1)
            return


async def _resolve_audio_url(frame: FrameOrPage) -> Optional[str]:
    """Find the audio download href; falls back to inline ``<audio>`` src."""
    for selector in AUDIO_DOWNLOAD_LINK.split(", "):
        link = await frame.query_selector(selector)
        if not link:
            continue
        url = await link.get_attribute("href")
        if url:
            return url
    return await frame.evaluate(
        "() => { const a = document.querySelector('audio'); return a ? a.src : null; }",
    )


async def capture_audio_state(frame: FrameOrPage) -> Optional[Tuple[bytes, str]]:
    """Bind audio answers to a stable document, recording and visible puzzle."""
    identity_script = """() => JSON.stringify({
        document: performance.timeOrigin,
        sources: [...document.querySelectorAll(
            "audio, audio source, [data-test-id='audio-link'], .audio-link, a[download]"
        )].map(el => el.currentSrc || el.src || el.href || '')
    })"""
    try:
        before = await frame.evaluate(identity_script)
        image = await capture_puzzle(frame)
        after = await frame.evaluate(identity_script)
        if image and before == after:
            return image, after
    except PlaywrightError:
        return None
    return None


async def _save_audio_to_temp(download_url: str, frame: Optional[FrameOrPage] = None) -> Optional[str]:
    """Download audio to a temp .mp3; ``None`` on non-200."""
    if frame is not None:
        data = await fetch_browser_audio(frame, download_url)
        return write_audio_temp(data, 'funcaptcha_audio_')
    import aiohttp
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp_path = temp_file.name
    temp_file.close()
    completed = False
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(download_url) as response:
                if response.status != 200:
                    return None
                with open(temp_path, "wb") as f:
                    f.write(await response.read())
        completed = True
        return temp_path
    finally:
        if not completed:
            unlink_quiet(temp_path)


async def download_audio(frame: FrameOrPage) -> Optional[str]:
    """Download the audio challenge file, returns the temp .mp3 path."""
    try:
        await _click_audio_play(frame)
        download_url = await _resolve_audio_url(frame)
        if not download_url:
            return None
        return await _save_audio_to_temp(download_url, frame)
    except BROAD_EXC as exc:
        emit(ErrCode.TOOL_EXECUTION_FAILED, str(exc))
        _LOGGER.error("Audio download error: %s", exc)
        return None


async def click_next_audio(frame: FrameOrPage) -> None:
    """Click to get next audio challenge (best-effort)."""
    try:
        next_selectors = (
            "[data-test-id='next-audio']",
            ".next-audio",
            "[aria-label*='next']",
            "[aria-label*='refresh']",
            ".refresh",
        )
        for selector in next_selectors:
            btn = await frame.query_selector(selector)
            if btn:
                await btn.click()
                return
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")




__all__ = [
    "ARROW_SELECTORS",
    "AUDIO_BUTTON",
    "AUDIO_DOWNLOAD_LINK",
    "AUDIO_PLAY_BUTTON",
    "DETECTION_PATTERNS",
    "FC_CONTAINER",
    "FC_IFRAME",
    "PUZZLE_IMAGE_SELECTOR",
    "WhisperTranscriber",
    "capture_puzzle",
    "capture_audio_state",
    "click_arrow",
    "click_audio_button",
    "click_next_audio",
    "container_is_present",
    "content_matches_detection",
    "detect",
    "download_audio",
    "extract_token",
    "frame_indicates_funcaptcha",
    "get_funcaptcha_iframe",
    "is_solved",
    "resolve_funcaptcha_frame",
    "unlink_quiet",
    "url_indicates_funcaptcha",
]
