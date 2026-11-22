"""Cloudflare challenges via the challenge-gateway service.

The gateway (bundled in the Docker stack, protocol compatible with
FlareSolverr https://github.com/FlareSolverr/FlareSolverr, MIT) runs as its own
container. CrawlRX only calls its
documented ``POST /v1`` API: the gateway opens the URL in its own browser,
waits out the challenge and returns the clearance cookies and user agent,
which CrawlRX copies into the blocked page before reloading it.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..captcha_handoff import CaptchaChallenge, page_still_blocked

logger = logging.getLogger("crawlrx.solvers.flaresolverr")

_KINDS = frozenset({"cloudflare", "turnstile"})
_DEFAULT_TIMEOUT_S = 60.0


def _timeout_from_env() -> float:
    raw = os.getenv("CRAWLRX_FLARESOLVERR_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_S
    except ValueError:
        logger.warning("CRAWLRX_FLARESOLVERR_TIMEOUT=%r is not a number; using %.0fs", raw, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if "://" not in base:
        base = f"http://{base}"
    return base if base.endswith("/v1") else f"{base}/v1"


def _playwright_cookie(cookie: Dict[str, Any], page_url: str) -> Optional[Dict[str, Any]]:
    name, value = cookie.get("name"), cookie.get("value")
    if not name or value is None:
        return None
    converted: Dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "domain": cookie.get("domain") or urlparse(page_url).hostname or "",
        "path": cookie.get("path") or "/",
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
    }
    expires = cookie.get("expires") or cookie.get("expiry")
    if isinstance(expires, (int, float)) and expires > 0:
        converted["expires"] = float(expires)
    same_site = str(cookie.get("sameSite") or "").capitalize()
    if same_site in ("Strict", "Lax", "None"):
        converted["sameSite"] = same_site
    return converted


class FlareSolverrSolver:
    """Clears Cloudflare interstitials using a FlareSolverr-compatible service."""

    name = "flaresolverr"
    kinds = _KINDS

    def __init__(self, base_url: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self.endpoint = _endpoint(base_url)
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> Optional["FlareSolverrSolver"]:
        url = os.getenv("CRAWLRX_FLARESOLVERR_URL", "").strip()
        return cls(url, _timeout_from_env()) if url else None

    async def _request_solution(self, url: str) -> Dict[str, Any]:
        payload = {"cmd": "request.get", "url": url, "maxTimeout": int(self.timeout_s * 1000)}
        async with httpx.AsyncClient(timeout=self.timeout_s + 10) as client:
            try:
                response = await client.post(self.endpoint, json=payload)
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"Challenge gateway is unreachable at {self.endpoint} ({type(exc).__name__}). Start it with "
                    "`docker compose up -d challenge-gateway` or fix CRAWLRX_FLARESOLVERR_URL."
                ) from exc
        data = response.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Challenge gateway could not clear {url}: {data.get('message', 'no message')}")
        return data.get("solution") or {}

    async def solve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        solution = await self._request_solution(challenge.url)
        cookies: List[Dict[str, Any]] = [
            c for c in (_playwright_cookie(raw, challenge.url) for raw in solution.get("cookies") or []) if c
        ]
        if not cookies:
            logger.info("Challenge gateway returned no cookies for %s", challenge.url)
            return False
        await page.context.add_cookies(cookies)
        user_agent = solution.get("userAgent")
        if user_agent:
            # Clearance cookies are bound to the user agent that earned them.
            await page.set_extra_http_headers({"User-Agent": str(user_agent)})
        await page.reload(wait_until="domcontentloaded")
        return not await page_still_blocked(page)
