"""StealthPage: playwright Page wrapper with identity CDP override + solve hooks.

Applies the same CDP user-agent/brand forging the legacy engine used (client
hints with brands, fullVersionList, platformVersion), delegates stealth
script injection to playwright_stealth, and attaches ``solve_hcaptcha`` /
``solve_recaptcha`` backed by CrawlRX's own bridges — no botright involved.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ._compat import sibling as _compat  # noqa: E402

logger = logging.getLogger(__name__)

_NAV_HINTS = {"Windows": "Win32", "macOS": "MacIntel", "Linux": "Linux x86_64"}

_STEALTH_SETUP_ERRORS = (ImportError, AttributeError, TypeError, RuntimeError)


async def apply_identity_cdp(page: Any, identity: Any) -> None:
    """Send Emulation/Network UA overrides carrying coherent client hints."""
    cdp = page.cdp
    override = {
        "userAgent": identity.user_agent,
        "acceptLanguage": identity.locale,
        "platform": _NAV_HINTS.get(identity.platform_name, identity.platform),
        "userAgentMetadata": {
            "brands": [
                {"brand": name, "version": ver} for name, ver in identity.brands
            ],
            "fullVersionList": [
                {"brand": name, "version": ver}
                for name, ver in identity.full_version_list
            ],
            "fullVersion": identity.full_version,
            "platform": identity.platform_name,
            "architecture": identity.architecture,
            "bitness": identity.bitness,
            "platformVersion": identity.platform_version,
            "model": "",
            "mobile": False,
        },
    }
    await cdp.send("Emulation.setUserAgentOverride", override)
    await cdp.send("Network.setUserAgentOverride", override)


async def _apply_stealth_scripts(page: Any) -> None:
    """Reuse the pool's playwright_stealth fallback chain."""
    try:
        stealth_async = _compat.sibling("solvers._stealth_browser_launch").stealth_async
        await stealth_async(page._origin_page)  # type: ignore[attr-defined]
    except _STEALTH_SETUP_ERRORS as exc:  # stealth is best-effort
        logger.debug("stealth_async unavailable: %s", exc)


class StealthPage:
    """Duck-typed page: playwright API + identity + solve hooks."""

    def __init__(self, origin_page: Any, context: Any, identity: Any) -> None:
        self._origin_page = origin_page
        self.context = context
        self.identity = identity
        self.cdp: Optional[Any] = None
        # Expose origin attributes transparently via __getattr__ (below).

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_origin_page"], name)

    async def attach(self) -> None:
        """Open the CDP session, forge the identity, inject stealth."""
        self.cdp = await self._origin_page.context.new_cdp_session(self._origin_page)
        await apply_identity_cdp(self, self.identity)
        await _apply_stealth_scripts(self)

    # -- Solve hooks (duck-typed targets of _run_active_solver) ---------

    async def solve_hcaptcha(self, rq_data: Optional[str] = None) -> Optional[str]:
        HcaptchaChallengeHandler = _compat.sibling("solvers.hcaptcha_challenge").HcaptchaChallengeHandler

        if rq_data:
            return None  # provider-specific request data cannot be injected
        handler = HcaptchaChallengeHandler({"enabled": True, "timeout": 35})
        token = await handler._extract_token(self._origin_page)
        if token:
            return token
        frame = None
        for _ in range(10):
            frame = await handler._detect_hcaptcha_frame(self._origin_page)
            if frame is not None:
                break
            import asyncio

            await asyncio.sleep(0.5)
        if frame is None or not await handler._click_checkbox(self._origin_page, frame):
            return None
        return await handler._solve_challenge(self._origin_page)

    async def solve_recaptcha(self) -> Optional[str]:
        bridge = _compat.sibling("botright_patches.recaptcha_bridge").solve_recaptcha

        return await bridge(self._origin_page)

    # -- Lifecycle ------------------------------------------------------

    async def close(self, *args: Any, **kwargs: Any) -> Any:
        return await self._origin_page.close(*args, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StealthPage {self.identity.brand_brand} {self.identity.chrome_major}>"
