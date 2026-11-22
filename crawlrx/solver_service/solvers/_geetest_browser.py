"""Browser-flow primitives for the GeeTest solver.

Owns CDP connection setup, context reuse, anti-detection script injection,
and graceful page/browser cleanup. The orchestrator owns the mutable
``page`` / ``context`` / ``browser`` fields and passes itself in via the
``host`` argument.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Protocol, TYPE_CHECKING

from ._geetest_helpers import ANTI_DETECTION_JS, V4_CAPTURE_INIT
from ._solver_helpers import BROAD_EXC as _BROAD_EXC

if TYPE_CHECKING:
    from playwright.async_api import Playwright

# Cancellation must reach the router; it is not a recoverable browser error.
_BROAD_EXC = tuple(error for error in _BROAD_EXC if error is not asyncio.CancelledError)

try:
    from ..core.sentinel_errors import ErrCode, emit
except ImportError:
    class _E:  # pragma: no cover
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"

    ErrCode = _E()  # type: ignore[assignment]  because sentinel ErrCode is unavailable; _E() shim returns ERR_UNKNOWN for any attr

    def emit(*a: object, **kw: object) -> None:  # noqa: ANN001
        return None


from playwright._impl._errors import Error as PlaywrightError  # noqa: E402

from ..models import TaskResult  # noqa: E402


logger = logging.getLogger(__name__)


class _BrowserHost(Protocol):
    """Subset of ``GeeTestSolver`` the browser helpers reach into."""

    cdp_port: int
    browser: Optional[Any]
    context: Optional[Any]
    page: Optional[Any]


def _is_connection_refused(error_msg: str) -> bool:
    """True iff ``error_msg`` matches the canonical CDP-refused signatures."""
    lowered = error_msg.lower()
    return any(token in lowered for token in ("connect", "refused", "econn"))


def _cdp_failed_result(cdp_port: int) -> TaskResult:
    """Standard ``CDP unreachable`` failure result."""
    return TaskResult(
        success=False,
        error_code="ERROR_CAPTCHA_UNSOLVABLE",
        error_message=(
            f"CDP connection failed: No browser listening on port "
            f"{cdp_port}. Start Chrome with "
            f"--remote-debugging-port={cdp_port} or set "
            f"GEETEST_CDP_PORT env var."
        ),
    )


async def connect_cdp_or_fail(host: _BrowserHost, p: "Playwright") -> Optional[TaskResult]:
    """Open the CDP browser; return ``None`` on success, ``TaskResult`` on
    connection-refused. Raises any other Playwright error.
    """
    try:
        host.browser = await p.chromium.connect_over_cdp(
            f"http://localhost:{host.cdp_port}",
        )
    except PlaywrightError as e:
        emit(ErrCode.NETWORK_ERROR, str(e))
        if _is_connection_refused(str(e)):
            logger.warning(
                "CDP connection failed on port %s: %s", host.cdp_port, e,
            )
            return _cdp_failed_result(host.cdp_port)
        raise
    return None


async def ensure_context(host: _BrowserHost) -> None:
    """Reuse first existing context or create a new viewport-stable one."""
    contexts = host.browser.contexts
    if contexts:
        host.context = contexts[0]
        return
    host.context = await host.browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
    )


async def setup_anti_detection(host: _BrowserHost) -> None:
    """Inject scripts to avoid detection."""
    if not host.page:
        return
    await host.page.add_init_script(ANTI_DETECTION_JS)
    if getattr(host, '_geetest_version', 3) == 4:
        await host.page.add_init_script(V4_CAPTURE_INIT)
        # Keep only bounded references to responses already requested by this
        # page. The slider reads its piece from these; it never fetches a URL
        # supplied by the page through an unrestricted HTTP client.
        host._geetest_image_responses = []

        def remember_image(response: Any) -> None:
            if response.request.resource_type not in ('image', 'fetch', 'xhr'):
                return
            host._geetest_image_responses.append(response)
            if len(host._geetest_image_responses) > 256:
                del host._geetest_image_responses[:-256]

        host.page.on('response', remember_image)


async def activate_v4_widget(host: _BrowserHost, captcha_id: str) -> bool:
    """Open one unambiguous matching widget before detecting its challenge."""
    try:
        await host.page.wait_for_function(
            "id => (window.__crawlrxGeeTest4 || []).length === 1 && window.__crawlrxGeeTest4[0].id === id",
            arg=captcha_id, timeout=10000,
        )
        widgets = host.page.locator('.geetest_captcha:visible')
        buttons = host.page.locator('.geetest_btn_click:visible')
        await buttons.first.wait_for(state='visible', timeout=10000)
        widget_count, button_count = await widgets.count(), await buttons.count()
        if widget_count != 1 or button_count != 1:
            logger.warning('Ambiguous GeeTest v4 controls: %s widgets, %s buttons', widget_count, button_count)
            return False
        await buttons.first.click(timeout=5000)
        return True
    except (PlaywrightError, *_BROAD_EXC) as error:
        logger.warning('Could not activate GeeTest v4 widget: %s', error)
        return False


async def _safe_close_page(host: _BrowserHost) -> None:
    """Close the active page, swallowing recoverable errors."""
    try:
        if host.page:
            await host.page.close()
            host.page = None
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def _safe_close_browser(host: _BrowserHost) -> None:
    """Close the browser, swallowing recoverable errors."""
    try:
        if host.browser:
            await host.browser.close()
            host.browser = None
    except _BROAD_EXC:
        emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")


async def cleanup(host: _BrowserHost) -> None:
    """Clean up browser resources."""
    await _safe_close_page(host)
    await _safe_close_browser(host)
    host.context = None
    host._geetest_image_responses = []


__all__ = [
    "connect_cdp_or_fail",
    "ensure_context",
    "setup_anti_detection",
    "cleanup",
]
