"""Page-flow helpers for the reCAPTCHA v2 audio challenge.

Owns every interaction with the recaptcha iframes: locating the
checkbox / challenge frames, clicking the audio-mode button, fetching
the audio URL, downloading the mp3, typing the response, and clicking
Verify. Splits roughly half of the previously-monolithic
``recaptcha_audio_solver`` into a focused module so the orchestrator
can stay under the LOC ceiling and below the
``cc.arch.god_module_imports`` threshold.

The functions take ``page``/``frame`` arguments instead of ``self`` so
no orchestrator instance state leaks across module boundaries; callers
in ``recaptcha_audio_solver`` adapt them via small wrapper methods.
"""
from __future__ import annotations

import asyncio
import random
import re as _re
from typing import List, Optional, Tuple

import httpx
from playwright.async_api import ElementHandle, Error as PlaywrightError, Frame, Page, TimeoutError as PlaywrightTimeoutError

if __package__:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC
    from ._browser_audio_download import fetch_browser_audio, write_audio_temp
else:
    from ._sentinel_shim import ErrCode, emit
    from ._solver_helpers import BROAD_EXC
    from ._browser_audio_download import fetch_browser_audio, write_audio_temp


async def random_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    """Sleep a random amount between ``min_sec`` and ``max_sec`` seconds."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def _human_like_mouse_move(page: Page, x: int, y: int) -> None:
    """Move the mouse along a quadratic-bezier curve to ``(x, y)``."""
    current = await page.evaluate(
        "() => ({ x: window.lastMouseX || 0, y: window.lastMouseY || 0 })"
    )
    start_x = current.get("x", 0)
    start_y = current.get("y", 0)
    steps = random.randint(10, 20)
    for i in range(1, steps + 1):
        t = i / steps
        control_x = (start_x + x) / 2 + random.randint(-50, 50)
        control_y = (start_y + y) / 2 + random.randint(-50, 50)
        curr_x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * control_x + t ** 2 * x
        curr_y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * control_y + t ** 2 * y
        await page.mouse.move(curr_x, curr_y)
        await asyncio.sleep(random.uniform(0.01, 0.03))
    await page.evaluate(f"window.lastMouseX = {x}; window.lastMouseY = {y};")


async def click_element(
    page: Page, selector: str, frame: Optional[Frame] = None,
    expected_state: tuple[Frame, str] | None = None,
) -> None:
    """Click the observed live element using trusted, measured browser input."""
    target = frame if frame else page
    element = await target.wait_for_selector(selector, timeout=10000)
    if not element:
        raise RuntimeError(f"Element not found: {selector}")
    await _click_handle(page, element, expected_state)


async def _click_handle(
    page: Page, element: ElementHandle, expected_state: tuple[Frame, str] | None,
) -> None:
    if expected_state is not None:
        await assert_audio_state(page, expected_state)
    await element.hover(timeout=10000)
    if expected_state is not None:
        await assert_audio_state(page, expected_state)
    await element.click(timeout=10000)


async def find_recaptcha_iframe(
    page: Page,
) -> Tuple[Optional[Frame], Optional[Frame]]:
    """Return ``(checkbox_frame, challenge_frame)``; either may be ``None``."""
    await asyncio.sleep(0)  # explicit yield so the linter sees the await
    checkbox_frame: Optional[Frame] = None
    challenge_frame: Optional[Frame] = None
    for frame in page.frames:
        url = frame.url
        if "recaptcha/api2/anchor" in url or "recaptcha/enterprise/anchor" in url:
            checkbox_frame = frame
        elif "recaptcha/api2/bframe" in url or "recaptcha/enterprise/bframe" in url:
            challenge_frame = frame
    return checkbox_frame, challenge_frame


async def is_challenge_visible(page: Page) -> bool:
    """True iff any recognised challenge popup element is visible."""
    _, challenge_frame = await find_recaptcha_iframe(page)
    if not challenge_frame:
        return False
    selectors = [
        ".rc-imageselect",
        ".rc-audiochallenge",
        "#recaptcha-verify-button",
        ".rc-button-default",
    ]
    try:
        for selector in selectors:
            element = await challenge_frame.query_selector(selector)
            if element and await element.is_visible():
                return True
    except BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
    return False


async def click_recaptcha_checkbox(page: Page) -> None:
    """Click the reCAPTCHA anchor checkbox (raises if not found)."""
    checkbox_frame, _ = await find_recaptcha_iframe(page)
    if not checkbox_frame:
        raise RuntimeError("reCAPTCHA checkbox iframe not found")
    selectors = [
        ".recaptcha-checkbox",
        ".recaptcha-checkbox-border",
        ".recaptcha-checkbox-checkmark",
        "#recaptcha-anchor",
        "[role='checkbox']",
    ]
    if await click_first_match(page, checkbox_frame, selectors):
        return
    raise RuntimeError("Could not click reCAPTCHA checkbox")


async def click_first_match(
    page: Page,
    challenge_frame: Frame,
    selectors: List[str],
    timeout_ms: int = 3000,
    expected_state: tuple[Frame, str] | None = None,
) -> bool:
    """Click first visible match from ``selectors``; ``True`` on hit."""
    button = await wait_first_match(challenge_frame, selectors, timeout_ms)
    if button is None:
        return False
    await _click_handle(page, button, expected_state)
    return True


async def wait_first_match(
    challenge_frame: Frame,
    selectors: List[str],
    timeout_ms: int = 3000,
) -> Optional[ElementHandle]:
    """Return the first selector match (no click); ``None`` if none hit."""
    if not selectors:
        return None
    try:
        return await challenge_frame.wait_for_selector(
            f":is({', '.join(selectors)}):visible", timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        return None


async def resolve_challenge_frame(page: Page) -> Frame:
    """Locate the reCAPTCHA challenge iframe; raises if missing."""
    _, challenge_frame = await find_recaptcha_iframe(page)
    if not challenge_frame:
        raise RuntimeError("Challenge frame not found")
    return challenge_frame


async def switch_to_audio_challenge(page: Page) -> None:
    """Switch from image challenge to audio challenge."""
    challenge_frame = await resolve_challenge_frame(page)
    audio_button_selectors = [
        "#recaptcha-audio-button",
        ".rc-button-audio",
        "[title='Get an audio challenge']",
        "button[aria-label*='audio']",
        ".rc-button[tabindex='0']",
    ]
    if await click_first_match(page, challenge_frame, audio_button_selectors):
        await random_delay(1, 2)
        return
    raise RuntimeError("Could not find audio challenge button")


async def _read_audio_url(selector: str, element: ElementHandle) -> Optional[str]:
    """Pull the audio URL from a single matched element."""
    return await element.evaluate('el => el.currentSrc || el.src || el.href || null')


async def _probe_audio_url(challenge_frame: Frame) -> Optional[str]:
    """Wait once for any supported recording source, including hidden audio."""
    try:
        handle = await challenge_frame.wait_for_function(
            """() => [...document.querySelectorAll('#audio-source, .rc-audiochallenge-tdownload, audio source, .rc-audiochallenge-instructions + div a')]
                .map(el => el.currentSrc || el.src || el.href).find(Boolean) || null""",
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        return None
    try:
        return await handle.json_value()
    finally:
        await handle.dispose()


async def _scrape_audio_url_from_html(challenge_frame: Frame) -> Optional[str]:
    """Last-resort regex scan of the iframe HTML for an mp3 URL."""
    content = await challenge_frame.content()
    mp3_matches = _re.findall(r'https?://[^\s"\']+\.mp3', content)
    return mp3_matches[0] if mp3_matches else None


async def _download_audio(audio_url: str, frame: Optional[Frame] = None) -> str:
    """Download ``audio_url`` to a temp .mp3 path; returns the path."""
    if frame is not None:
        data = await fetch_browser_audio(frame, audio_url)
    else:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(audio_url)
            response.raise_for_status()
            data = response.content
    return write_audio_temp(data, 'recaptcha_audio_')


async def get_audio_challenge(page: Page) -> str:
    """Locate the audio URL and download it to a temp file path."""
    challenge_frame = await resolve_challenge_frame(page)
    await random_delay(1, 2)
    audio_url = await _probe_audio_url(challenge_frame)
    if not audio_url:
        audio_url = await _scrape_audio_url_from_html(challenge_frame)
    if not audio_url:
        raise RuntimeError("Could not find audio challenge URL")
    return await _download_audio(audio_url, challenge_frame)


_AUDIO_SOURCES = '#audio-source, .rc-audiochallenge-tdownload, audio source, .rc-audiochallenge-instructions + div a'
_AUDIO_STATE_JS = """() => {
    const sources = [...document.querySelectorAll('#audio-source, .rc-audiochallenge-tdownload, audio source, .rc-audiochallenge-instructions + div a')]
        .map(el => [el.tagName, el.currentSrc || null, el.src || null, el.href || null])
        .filter(source => source.slice(1).some(Boolean));
    return sources.length ? JSON.stringify({origin: performance.timeOrigin, url: location.href, sources}) : null;
}"""


async def capture_audio_state(page: Page) -> tuple[Frame, str]:
    """Bind the transcript to this challenge document and its recording sources."""
    frame = await resolve_challenge_frame(page)
    await frame.wait_for_selector(_AUDIO_SOURCES, state='attached', timeout=10000)
    state = await frame.evaluate(_AUDIO_STATE_JS)
    if not state:
        raise RuntimeError('Audio challenge source unavailable')
    return frame, state


async def assert_audio_state(page: Page, expected: tuple[Frame, str]) -> None:
    """Reject changed sources, navigated documents, and replacement iframes."""
    frame, state = expected
    try:
        current = await resolve_challenge_frame(page)
        if current is frame and await frame.evaluate(_AUDIO_STATE_JS) == state:
            return
    except (PlaywrightError, RuntimeError):
        pass
    raise RuntimeError('Audio challenge changed before response')


async def submit_audio_response(
    page: Page, response_text: str, expected_state: tuple[Frame, str] | None = None,
) -> None:
    """Type ``response_text`` into the input and click Verify."""
    if expected_state is not None:
        await assert_audio_state(page, expected_state)
    challenge_frame = await resolve_challenge_frame(page)
    input_selectors = [
        "#audio-response",
        ".rc-audiochallenge-input-field input",
        "input[aria-label*='response']",
        "input[title*='response']",
    ]
    input_element = await wait_first_match(challenge_frame, input_selectors)
    if not input_element:
        raise RuntimeError("Audio response input not found")
    if expected_state is not None:
        await assert_audio_state(page, expected_state)
    await input_element.fill(response_text)
    await random_delay(0.5, 1.0)
    verify_selectors = [
        "#recaptcha-verify-button",
        "#rc-button-verify",
        "button[aria-label*='Verify']",
        ".rc-button-default",
    ]
    if await click_first_match(page, challenge_frame, verify_selectors, expected_state=expected_state):
        return
    raise RuntimeError("Verify button not found")


_TOKEN_JS = """
    () => {
        const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
        if (textarea && textarea.value) return textarea.value;

        const responseElements = document.querySelectorAll('[name*="g-recaptcha-response"]');
        for (const el of responseElements) {
            if (el.value) return el.value;
        }

        const enterpriseEls = document.querySelectorAll('[name*="g-recaptcha-response-100000"]');
        for (const el of enterpriseEls) {
            if (el.value) return el.value;
        }

        return null;
    }
"""


async def extract_token(page: Page) -> Optional[str]:
    """Extract the ``g-recaptcha-response`` token via JS evaluation."""
    return await page.evaluate(_TOKEN_JS)


__all__ = [
    "assert_audio_state",
    "capture_audio_state",
    "click_element",
    "click_first_match",
    "click_recaptcha_checkbox",
    "extract_token",
    "find_recaptcha_iframe",
    "get_audio_challenge",
    "is_challenge_visible",
    "random_delay",
    "resolve_challenge_frame",
    "submit_audio_response",
    "switch_to_audio_challenge",
    "wait_first_match",
]
