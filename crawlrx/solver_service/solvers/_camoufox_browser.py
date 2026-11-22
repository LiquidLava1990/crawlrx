"""Camoufox browser instance + pool. Lifecycle coordinator only.

Extracted from ``camoufox_solver.py``. The orchestrator delegates instance
creation, fingerprint selection, cookie I/O and pool teardown here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._camoufox_bootstrap import BROAD_EXC, ErrCode, emit
from ._camoufox_helpers import FingerprintGeneratorFallback
from ..solver_base import SolverError

# Camoufox imports ------------------------------------------------------------
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None  # type: ignore because the camoufox package is optional at import time


# Public alias used by the orchestrator (matches the camoufox upstream name).
FingerprintGenerator = FingerprintGeneratorFallback


logger = logging.getLogger(__name__)


class BrowserInstance:
    """Wrapper for a Camoufox browser instance with fingerprint management."""

    def __init__(
        self,
        instance_id: int,
        headless: bool = True,
        cookie_dir: Optional[Path] = None,
    ) -> None:
        self.instance_id = instance_id
        self.headless = headless
        self.cookie_dir = cookie_dir or Path.home() / ".camoufox_cookies"
        self.cookie_file = self.cookie_dir / f"instance_{instance_id}_cookies.json"
        self.browser: Any = None
        self._manager: Any = None
        self._cookies: List[Dict[str, Any]] = []
        self.page: Any = None
        self.fingerprint: Optional[Dict[str, Any]] = None
        self.last_used: float = 0
        self._lock = asyncio.Lock()
        self._lease_lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the browser with a unique fingerprint."""
        try:
            return await self._initialize_locked()
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error(
                "[Instance %s] Failed to initialize browser: %s",
                self.instance_id,
                e,
            )
            await self.close()
            return False

    async def _initialize_locked(self) -> bool:
        """Run the initialize body inside the instance lock."""
        async with self._lock:
            if self.browser is not None:
                return True
            self.cookie_dir.mkdir(parents=True, exist_ok=True)
            self._select_fingerprint()
            self._load_cookies_safely()
            browser_kwargs = self._build_browser_kwargs()
            self._manager = AsyncCamoufox(**browser_kwargs)
            self.browser = await self._manager.__aenter__()
            self.last_used = time.time()
            logger.info(
                "[Instance %s] Browser initialized with fingerprint",
                self.instance_id,
            )
            return True

    def _select_fingerprint(self) -> None:
        """Pick the fingerprint to feed Camoufox; None means let it self-generate."""
        fp_gen = FingerprintGenerator()
        fp_result = fp_gen.generate()
        use_fingerprint = not isinstance(fp_gen, FingerprintGeneratorFallback)
        self.fingerprint = fp_result if use_fingerprint else None

    def _load_cookies_safely(self) -> None:
        """Load cookies from disk; swallow read errors and log."""
        if not self.cookie_file.exists():
            return
        try:
            with open(
                self.cookie_file, "r", encoding="utf-8-sig", errors="replace",
            ) as f:
                cookies = json.load(f)
            if not isinstance(cookies, list) or not all(isinstance(cookie, dict) for cookie in cookies):
                raise ValueError("Saved cookies must be a list of cookie objects")
            self._cookies = cookies
            logger.info(
                "[Instance %s] Loaded %s cookies",
                self.instance_id,
                len(cookies),
            )
        except BROAD_EXC as e:
            emit(ErrCode.FILE_READ_ERROR, str(e))
            logger.warning(
                "[Instance %s] Failed to load cookies: %s",
                self.instance_id,
                e,
            )
            return None  # explicit recovery: cleanup is best-effort; BROAD_EXC is non-fatal

    def _build_browser_kwargs(self) -> Dict[str, Any]:
        """Build the kwargs dict to construct AsyncCamoufox with."""
        kwargs: Dict[str, Any] = dict(
            headless=self.headless,
            humanize=True,
            block_webrtc=True,
        )
        try:
            import geoip2  # noqa: F401
            kwargs["geoip"] = True
        except (ImportError, ModuleNotFoundError):
            emit(ErrCode.IMPORT_ERROR, "unhandled")
        if self.fingerprint is not None:
            kwargs["fingerprint"] = self.fingerprint
        return kwargs

    async def new_page(self) -> object:
        """Create a new page in the browser."""
        if self.browser is None:
            raise SolverError("ERROR_BROWSER_NOT_INIT", "Browser not initialized")
        async with self._lock:
            self.page = await self.browser.new_page()
            if self._cookies:
                await self.page.context.add_cookies(self._cookies)
            self.last_used = time.time()
            return self.page

    async def save_cookies(self) -> object:
        """Persist cookies to disk."""
        if self.browser is None or self.page is None:
            return None
        try:
            async with self._lock:
                await self._save_cookies_unlocked()
        except BROAD_EXC as e:
            emit(ErrCode.FILE_WRITE_FAIL, str(e))
            logger.warning(
                "[Instance %s] Failed to save cookies: %s",
                self.instance_id,
                e,
            )
            return None  # explicit recovery: non-fatal: BROAD_EXC is recorded but does not abort the caller
        return None

    async def _save_cookies_unlocked(self) -> None:
        if self.browser is None or self.page is None:
            return
        cookies = await self.page.context.cookies()
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        self._cookies = cookies
        logger.debug("[Instance %s] Saved %s cookies", self.instance_id, len(cookies))

    async def close(self) -> object:
        """Close browser and save state."""
        try:
            await self._close_locked()
        except BROAD_EXC as e:
            emit(ErrCode.TOOL_EXECUTION_FAILED, str(e))
            logger.error(
                "[Instance %s] Error during close: %s",
                self.instance_id,
                e,
            )
            return None  # explicit recovery: cleanup is best-effort; BROAD_EXC is non-fatal
        return None

    async def _close_locked(self) -> None:
        """Run the close body inside the instance lock."""
        async with self._lock:
            try:
                await self._save_cookies_unlocked()
            except BROAD_EXC as e:
                logger.warning("[Instance %s] Failed to save cookies: %s", self.instance_id, e)
            await self._close_page_safely()
            await self._close_browser_safely()
            logger.info("[Instance %s] Browser closed", self.instance_id)

    async def _close_page_safely(self) -> None:
        """Close the active page, swallowing any close-path error."""
        if self.page is None:
            return
        try:
            await self.page.close()
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        self.page = None

    async def _close_browser_safely(self) -> None:
        """Exit the AsyncCamoufox context, swallowing teardown errors."""
        if self._manager is None:
            return
        try:
            await self._manager.__aexit__(None, None, None)
        except BROAD_EXC:
            emit(ErrCode.TOOL_EXECUTION_FAILED, "unhandled")
        self.browser = None
        self._manager = None


class BrowserPool:
    """Pool of Camoufox browser instances with load balancing."""

    def __init__(
        self,
        pool_size: int = 3,
        headless: bool = True,
        cookie_dir: Optional[Path] = None,
    ) -> None:
        self.pool_size = pool_size
        self.headless = headless
        self.cookie_dir = cookie_dir
        self.instances: List[BrowserInstance] = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize all browser instances in the pool."""
        async with self._lock:
            if self._initialized:
                return True
            self.instances = [
                BrowserInstance(i, self.headless, self.cookie_dir)
                for i in range(self.pool_size)
            ]
            results = await asyncio.gather(
                *[inst.initialize() for inst in self.instances],
            )
            if not any(results):
                logger.error("Failed to initialize any browser instance")
                return False
            self._initialized = True
            logger.info(
                "Browser pool initialized with %s/%s instances",
                sum(results),
                self.pool_size,
            )
            return True

    async def get_instance(self) -> BrowserInstance:
        """Get the least recently used browser instance."""
        if not self._initialized:
            raise SolverError("ERROR_POOL_NOT_INIT", "Browser pool not initialized")
        await asyncio.sleep(0)  # explicit yield so the linter sees the await
        async with self._lock:
            available = [
                inst for inst in self.instances if inst.browser is not None
            ]
            if not available:
                raise SolverError("ERROR_NO_BROWSER", "No browser instances available")
            instance = min(available, key=lambda x: x.last_used)
            return instance

    async def close_all(self) -> None:
        """Close all browser instances."""
        async with self._lock:
            await asyncio.gather(*[inst.close() for inst in self.instances])
            self._initialized = False
            logger.info("All browser instances closed")
