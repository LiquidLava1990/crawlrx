"""reCAPTCHA / hCaptcha / Turnstile via a 2Captcha-compatible solver service.

Many self-hosted solver services speak the 2Captcha HTTP API
(``POST /in.php`` to submit, ``GET /res.php`` to poll). Point
``CRAWLRX_SOLVER_SERVICE_URL`` at one you run yourself. CrawlRX bundles no
service and no API key; ``CRAWLRX_SOLVER_SERVICE_KEY`` is only sent when you
set it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

from ..captcha_handoff import CaptchaChallenge, page_still_blocked
from ._page_tokens import find_widget, submit_token, token_kinds

logger = logging.getLogger("crawlrx.solvers.service")

_DEFAULT_TIMEOUT_S = 120.0
_POLL_S = 5.0


class SolverServiceSolver:
    """Sends the page's sitekey to a solver service and injects the returned token."""

    name = "service"
    kinds = token_kinds()

    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = _DEFAULT_TIMEOUT_S,
                 poll_s: float = _POLL_S) -> None:
        base = base_url.strip().rstrip("/")
        self.base_url = base if "://" in base else f"http://{base}"
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    @classmethod
    def from_env(cls) -> Optional["SolverServiceSolver"]:
        url = os.getenv("CRAWLRX_SOLVER_SERVICE_URL", "").strip()
        if not url:
            return None
        raw_timeout = os.getenv("CRAWLRX_SOLVER_SERVICE_TIMEOUT", "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else _DEFAULT_TIMEOUT_S
        except ValueError:
            logger.warning("CRAWLRX_SOLVER_SERVICE_TIMEOUT=%r is not a number; using %.0fs",
                           raw_timeout, _DEFAULT_TIMEOUT_S)
            timeout = _DEFAULT_TIMEOUT_S
        return cls(url, os.getenv("CRAWLRX_SOLVER_SERVICE_KEY", "").strip(), timeout)

    def _params(self, **extra: str) -> Dict[str, str]:
        params = {"json": "1", **extra}
        if self.api_key:
            params["key"] = self.api_key
        return params

    async def _submit(self, client: httpx.AsyncClient, method: str, sitekey: str, url: str) -> str:
        key_field = "googlekey" if method == "userrecaptcha" else "sitekey"
        data = self._params(method=method, pageurl=url, **{key_field: sitekey})
        response = await client.post(f"{self.base_url}/in.php", data=data)
        body = response.json()
        if body.get("status") != 1:
            raise RuntimeError(f"solver service refused the task: {body.get('request')}")
        return str(body["request"])

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str:
        while True:
            await asyncio.sleep(self.poll_s)
            response = await client.get(f"{self.base_url}/res.php", params=self._params(action="get", id=task_id))
            body = response.json()
            if body.get("status") == 1:
                return str(body["request"])
            if body.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"solver service failed: {body.get('request')}")

    async def solve(self, page: Any, challenge: CaptchaChallenge) -> bool:
        widget = await find_widget(page, challenge.kind)
        if widget is None:
            logger.info("no %s widget with a sitekey found on %s", challenge.kind, challenge.url)
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                task_id = await self._submit(client, widget.method, widget.sitekey, challenge.url)
                token = await self._poll(client, task_id)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"solver service unreachable at {self.base_url} ({type(exc).__name__}); "
                "check CRAWLRX_SOLVER_SERVICE_URL"
            ) from exc
        outcome = await submit_token(page, challenge.kind, token)
        logger.info("solver service token for %s applied (%s)", challenge.url, outcome)
        return not await page_still_blocked(page)
