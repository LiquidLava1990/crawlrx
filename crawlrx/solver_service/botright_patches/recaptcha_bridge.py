"""Solve an existing page's reCAPTCHA with CrawlRX's audio adapter."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from playwright.async_api import Page

from ..solvers._recaptcha_audio_browser import (
    capture_audio_state,
    click_recaptcha_checkbox,
    extract_token,
    get_audio_challenge,
    is_challenge_visible,
    submit_audio_response,
    switch_to_audio_challenge,
)
from ..solvers._recaptcha_audio_transcribe import transcribe_audio, transcribe_audio_async

_LOGGER = logging.getLogger(__name__)

_applied: bool = False


async def solve_recaptcha(page: Page) -> Optional[str]:
    token = await extract_token(page)
    if token:
        return token
    await click_recaptcha_checkbox(page)
    await asyncio.sleep(2)
    if await is_challenge_visible(page):
        await switch_to_audio_challenge(page)
        await asyncio.sleep(2)
        audio_state = await capture_audio_state(page)
        audio_path = await get_audio_challenge(page)
        answer = await transcribe_audio_async(audio_path)
        if not answer:
            return None
        await submit_audio_response(page, answer, expected_state=audio_state)
    for _ in range(10):
        token = await extract_token(page)
        if token:
            return token
        await asyncio.sleep(1)
    return None


def apply() -> None:
    """Publish this module as ``botright.modules.recaptcha_bridge``.

    botright 0.5.1 has no ``recaptcha_bridge`` submodule; the patched
    ``Page.solve_recaptcha`` (see ``page.py``) imports it lazily, so the
    module is registered in ``sys.modules`` and bound as an attribute of
    ``botright.modules``. Idempotent: calls after a successful patch are
    no-ops. When the pip ``botright`` package is not installed, logs at
    warning level and leaves everything untouched instead of raising.
    """
    global _applied
    if _applied:
        return
    try:
        import botright.modules as botright_modules
    except ImportError as exc:
        _LOGGER.warning("botright is not installed; recaptcha bridge patch not applied: %s", exc)
        return
    sys.modules["botright.modules.recaptcha_bridge"] = sys.modules[__name__]
    botright_modules.recaptcha_bridge = sys.modules[__name__]
    _applied = True
