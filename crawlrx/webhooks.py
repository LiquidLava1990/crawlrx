"""CrawlRX webhook delivery: async HMAC-SHA256-signed POST with retry."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict

from ._scraper_helpers import network_exception_types
from .utils import is_safe_url

logger = logging.getLogger("crawlrx.webhooks")


# Tuple of HTTP delivery failures that are retryable. Resolved at import
# so the retry loop can declare a narrow ``except`` instead of broad ``Exception``.
_DELIVERY_RETRY_ERRORS: tuple[type[BaseException], ...] = (
    *network_exception_types(),
    urllib.error.URLError,
    urllib.error.HTTPError,
    json.JSONDecodeError,
    ValueError,
    TypeError,
)


class WebhookDelivery:
    """Async non-blocking webhook poster with HMAC-SHA256 signing + retry."""

    MAX_RETRIES = 3
    RETRY_BACKOFF_SECONDS = 2
    DEFAULT_TIMEOUT_S = 10

    async def deliver(
        self,
        url: str,
        payload: Dict[str, Any],
        secret: str = "",
        timeout_s: int = 10,
    ) -> bool:
        """POST ``payload`` to ``url`` with retry; signs when ``secret`` set."""
        attempt = 0
        last_error: BaseException | None = None
        while attempt < self.MAX_RETRIES:
            attempt += 1
            ok, last_error = await self._try_send(url, payload, secret, timeout_s, attempt)
            if ok:
                return True
            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
        logger.error(
            "Webhook delivery failed after %d attempts to %s: %s",
            self.MAX_RETRIES,
            url,
            last_error,
        )
        return False

    async def _try_send(
        self,
        url: str,
        payload: Dict[str, Any],
        secret: str,
        timeout_s: int,
        attempt: int,
    ) -> tuple[bool, BaseException | None]:
        """Run one delivery attempt; returns (ok, last_error)."""
        try:
            result = await self._send_request(url, payload, secret, timeout_s)
        except _DELIVERY_RETRY_ERRORS as exc:
            logger.warning(
                "Webhook delivery attempt %d/%d failed for %s: %s: %s",
                attempt,
                self.MAX_RETRIES,
                url,
                type(exc).__name__,
                exc,
            )
            return False, exc
        if result:
            logger.info(
                "Webhook delivered successfully to %s (attempt %d/%d)",
                url,
                attempt,
                self.MAX_RETRIES,
            )
            return True, None
        return False, None

    async def _send_request(
        self,
        url: str,
        payload: Dict[str, Any],
        secret: str,
        timeout_s: int,
    ) -> bool:
        """Send the actual HTTP POST request via the default executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_post, url, payload, secret, timeout_s
        )

    def _build_request(
        self,
        url: str,
        body: bytes,
        secret: str,
    ) -> urllib.request.Request:
        """Build a signed (or unsigned) ``urllib`` request for ``url``."""
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "CrawlRX-Webhook/1.0",
            },
            method="POST",
        )
        if secret:
            signature = self._generate_signature(body, secret)
            req.add_header("X-CrawlRX-Signature", f"sha256={signature}")
        return req

    def _sync_post(
        self,
        url: str,
        payload: Dict[str, Any],
        secret: str,
        timeout_s: int,
    ) -> bool:
        """Synchronous HTTP POST executed off the event loop."""
        body = json.dumps(payload, default=str).encode("utf-8")
        req = self._build_request(url, body, secret)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                status = response.getcode()
                if 200 <= status < 300:
                    return True
                logger.warning("Webhook returned non-2xx status: %d", status)
                return False
        except urllib.error.HTTPError as exc:
            logger.warning("Webhook HTTP error: %d %s", exc.code, exc.reason)
            return False
        except urllib.error.URLError as exc:
            logger.warning("Webhook URL error: %s", exc.reason)
            return False

    def _generate_signature(self, body: bytes, secret: str) -> str:
        """Generate the HMAC-SHA256 signature for ``body`` with ``secret``."""
        return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# Module-level singleton -- imported by main.py.
delivery = WebhookDelivery()


async def deliver_webhook(
    url: str,
    payload: Dict[str, Any],
    secret: str = "",
    timeout_s: int = 10,
) -> bool:
    """Deliver via the module singleton; SSRF-guarded by :func:`is_safe_url`."""
    if not is_safe_url(url):
        logger.warning("Webhook URL blocked by SSRF protection: %s", url)
        return False
    return await delivery.deliver(url, payload, secret, timeout_s)


__all__ = ("WebhookDelivery", "delivery", "deliver_webhook")
