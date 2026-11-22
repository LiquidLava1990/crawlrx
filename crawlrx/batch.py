"""
batch.py -- /batch router
Batch URL processing with concurrency control.
Processes multiple URLs in parallel with semaphore-based throttling.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List
from fastapi import APIRouter

from .models import BatchRequest, BatchResult, BatchResponse
from .utils import timer_ms, is_safe_url
from .webhooks import deliver_webhook

# Prevent fire-and-forget tasks from being GC'd mid-execution
_background_tasks: set = set()
from .crawl import _do_crawl
from .scrape import _do_scrape

logger = logging.getLogger("crawlrx.batch")
router = APIRouter(prefix="", tags=["batch"])

# Narrow exception family for one batch unit. Network / parse / runtime are
# the recoverable failures; anything else (KeyboardInterrupt, MemoryError)
# must propagate so ``asyncio.gather`` can surface it.
_BATCH_UNIT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
    asyncio.TimeoutError,
)


def _normalise_links(raw_links: Any) -> List[str]:
    """Coerce assorted link payloads into a flat ``list[str]``."""
    if not raw_links:
        return []
    return [
        lnk.get("url", lnk) if isinstance(lnk, dict) else str(lnk)
        for lnk in raw_links
    ]


def _ssrf_blocked_result(url: str) -> BatchResult:
    """Canonical SSRF-block batch result."""
    return BatchResult(
        url=url,
        success=False,
        error="URL blocked by SSRF protection",
        elapsed_ms=0,
    )


def _crawl_result_from_raw(
    url: str,
    raw: Dict[str, Any],
    request: BatchRequest,
    elapsed: int,
) -> BatchResult:
    """Build a :class:`BatchResult` from a ``_do_crawl`` payload."""
    raw_links = raw.get("links", []) if request.extract_links else []
    return BatchResult(
        url=url,
        success=raw.get("success", False),
        title=raw.get("title"),
        text=raw.get("text") if request.extract_text else None,
        markdown=raw.get("markdown") if request.extract_markdown else None,
        links=_normalise_links(raw_links),
        error=raw.get("error"),
        elapsed_ms=elapsed,
    )


def _scrape_result_from_raw(
    url: str,
    raw: Dict[str, Any],
    elapsed: int,
) -> BatchResult:
    """Build a :class:`BatchResult` from a ``_do_scrape`` payload."""
    results = raw.get("results", [])
    first = results[0] if results else {}
    return BatchResult(
        url=url,
        success=raw.get("success", False),
        title=raw.get("title"),
        text=first.get("text"),
        markdown=first.get("markdown"),
        links=_normalise_links(first.get("links") or []),
        error=raw.get("error"),
        elapsed_ms=elapsed,
    )


@dataclass(frozen=True)
class _BatchUnit:
    """Bundle of per-URL execution context. Keeps ``_run_batch_unit`` ≤5 args."""

    url: str
    label: str
    invoker: Callable[[], Awaitable[Dict[str, Any]]]
    builder: Callable[[Dict[str, Any], int], BatchResult]


async def _run_batch_unit(
    unit: _BatchUnit,
    semaphore: asyncio.Semaphore,
) -> BatchResult:
    """Shared semaphore-bounded run loop for a single crawl/scrape unit."""
    async with semaphore:
        start = timer_ms()
        if not is_safe_url(unit.url):
            return _ssrf_blocked_result(unit.url)
        try:
            logger.info("Batch %s starting: %s", unit.label, unit.url)
            raw = await unit.invoker()
        except _BATCH_UNIT_EXCEPTIONS as exc:
            elapsed = int(timer_ms() - start)
            logger.error(
                "Batch %s failed for %s (%s): %s",
                unit.label,
                unit.url,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return BatchResult(
                url=unit.url,
                success=False,
                error=str(exc),
                elapsed_ms=elapsed,
            )
        elapsed = int(timer_ms() - start)
        logger.info("Batch %s done: %s (%d ms)", unit.label, unit.url, elapsed)
        return unit.builder(raw, elapsed)


def _crawl_kwargs(url: str, request: BatchRequest) -> Dict[str, Any]:
    """Keyword args passed to :func:`_do_crawl` for a single URL."""
    return {
        "url": url,
        "wait_for": "networkidle",
        "timeout": request.timeout,
        "javascript": True,
        "extract_links_flag": request.extract_links,
        "extract_text_flag": request.extract_text,
        "extract_markdown_flag": request.extract_markdown,
        "extract_images_flag": False,
        "user_agent": None,
        "headers": None,
        "screenshot": request.screenshot,
        "scroll": True,
        "agent": request.agent,
        "include_structured_data": False,
        "stealth": True,
        "include_readable": False,
    }


def _scrape_kwargs(url: str, request: BatchRequest) -> Dict[str, Any]:
    """Keyword args passed to :func:`_do_scrape` for a single URL."""
    return {
        "url": url,
        "selector": request.selector,
        "selectors": None,
        "extract": request.extract or "text",
        "wait_for": None,
        "timeout": request.timeout,
        "javascript": True,
        "user_agent": None,
        "headers": None,
        "scroll_to_bottom": False,
        "click_selector": None,
        "agent": request.agent,
    }


def _crawl_unit(url: str, request: BatchRequest) -> _BatchUnit:
    """Build a crawl :class:`_BatchUnit` for one URL."""
    kwargs = _crawl_kwargs(url, request)
    return _BatchUnit(
        url=url,
        label="crawl",
        invoker=lambda: _do_crawl(**kwargs),
        builder=lambda raw, elapsed: _crawl_result_from_raw(
            url, raw, request, elapsed
        ),
    )


def _scrape_unit(url: str, request: BatchRequest) -> _BatchUnit:
    """Build a scrape :class:`_BatchUnit` for one URL."""
    kwargs = _scrape_kwargs(url, request)
    return _BatchUnit(
        url=url,
        label="scrape",
        invoker=lambda: _do_scrape(**kwargs),
        builder=lambda raw, elapsed: _scrape_result_from_raw(url, raw, elapsed),
    )


async def _process_single_crawl(
    url: str,
    request: BatchRequest,
    semaphore: asyncio.Semaphore,
) -> BatchResult:
    """Process a single crawl within semaphore limit."""
    return await _run_batch_unit(_crawl_unit(url, request), semaphore)


async def _process_single_scrape(
    url: str,
    request: BatchRequest,
    semaphore: asyncio.Semaphore,
) -> BatchResult:
    """Process a single scrape within semaphore limit."""
    return await _run_batch_unit(_scrape_unit(url, request), semaphore)


def _coerce_gather_results(
    urls: List[str],
    raw_results: List[Any],
) -> List[BatchResult]:
    """Lift ``asyncio.gather(return_exceptions=True)`` output into BatchResults."""
    coerced: List[BatchResult] = []
    for index, result in enumerate(raw_results):
        if isinstance(result, Exception):
            logger.error(
                "Unexpected exception for %s (%s): %s",
                urls[index],
                type(result).__name__,
                result,
            )
            coerced.append(
                BatchResult(
                    url=urls[index],
                    success=False,
                    error=f"Unexpected error: {result}",
                    elapsed_ms=0,
                )
            )
        else:
            coerced.append(result)
    return coerced


def _build_response(
    batch_results: List[BatchResult],
    total_elapsed: int,
) -> BatchResponse:
    """Assemble the :class:`BatchResponse` from per-URL results."""
    succeeded = sum(1 for r in batch_results if r.success)
    failed = len(batch_results) - succeeded
    logger.info(
        "Batch done: %d total, %d ok, %d failed, %d ms",
        len(batch_results),
        succeeded,
        failed,
        total_elapsed,
    )
    return BatchResponse(
        success=(failed == 0),
        total=len(batch_results),
        succeeded=succeeded,
        failed=failed,
        results=batch_results,
        elapsed_ms=total_elapsed,
    )


def _build_webhook_payload(response: BatchResponse) -> Dict[str, Any]:
    """Shape the webhook payload from a finished :class:`BatchResponse`."""
    return {
        "success": response.failed == 0,
        "total": response.total,
        "succeeded": response.succeeded,
        "failed": response.failed,
        "elapsed_ms": response.elapsed_ms,
        "results": [
            {
                "url": r.url,
                "success": r.success,
                "title": r.title,
                "error": r.error,
                "elapsed_ms": r.elapsed_ms,
            }
            for r in response.results
        ],
    }


def _schedule_webhook(request: BatchRequest, response: BatchResponse) -> None:
    """Fire-and-forget webhook delivery; tracks task to avoid GC."""
    if not request.webhook_url:
        return
    response.webhook_url = request.webhook_url
    payload = _build_webhook_payload(response)
    task = asyncio.create_task(
        _deliver_webhook_and_update(
            request.webhook_url,
            payload,
            request.webhook_secret or "",
            response,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("/batch", response_model=BatchResponse, summary="Batch process multiple URLs")
async def batch_process(request: BatchRequest) -> BatchResponse:
    """Process URLs in parallel; ``mode`` selects crawl vs. scrape per URL."""
    start_total = timer_ms()
    logger.info(
        "Batch: %d URLs, mode=%s, concurrency=%d",
        len(request.urls),
        request.mode,
        request.max_concurrency,
    )
    semaphore = asyncio.Semaphore(request.max_concurrency)
    processor = (
        _process_single_crawl
        if request.mode == "crawl"
        else _process_single_scrape
    )
    tasks = [processor(url, request, semaphore) for url in request.urls]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    batch_results = _coerce_gather_results(list(request.urls), list(raw_results))
    total_elapsed = int(timer_ms() - start_total)
    response = _build_response(batch_results, total_elapsed)
    _schedule_webhook(request, response)
    return response


async def _deliver_webhook_and_update(
    url: str,
    payload: dict,
    secret: str,
    response: BatchResponse,
) -> None:
    response.webhook_delivered = await deliver_webhook(url, payload, secret)
