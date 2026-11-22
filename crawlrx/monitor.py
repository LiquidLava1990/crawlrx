"""monitor.py -- URL change detection with webhook callbacks"""

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import requests
import urllib3
from fastapi import APIRouter, HTTPException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .models import MonitorRequest, MonitorResponse, MonitorStatus, MonitorListResponse
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.monitor")
router = APIRouter(prefix="", tags=["monitor"])

_SWEEP_SLEEP_SECONDS = 30


@dataclass
class MonitorJob:
    """Internal representation of a monitoring job."""
    id: str
    url: str
    interval_seconds: int
    webhook_url: str
    selector: Optional[str] = None
    label: Optional[str] = None
    baseline_hash: Optional[str] = None
    last_checked: Optional[str] = None
    last_changed: Optional[str] = None
    status: str = "active"  # active | paused | error


monitors: Dict[str, MonitorJob] = {}
_lock = threading.RLock()


def _generate_monitor_id() -> str:
    """Generate a short unique monitor ID."""
    return str(uuid.uuid4())[:8]


def _hash_content(content: str) -> str:
    """Generate SHA256 hash of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat() + "Z"


def _fetch_url_content(url: str, selector: Optional[str] = None) -> Optional[str]:
    """Fetch URL content; selector is logged but full page is hashed."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        if selector:
            logger.debug("Selector provided (%s) but using full page content", selector)
        return resp.text
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return None


def _fire_webhook(job: MonitorJob, event: str = "content_changed") -> bool:
    """POST a change-event payload to the job's webhook URL."""
    try:
        payload = {
            "monitor_id": job.id,
            "url": job.url,
            "label": job.label,
            "changed_at": _now_iso(),
            "event": event,
        }
        resp = requests.post(job.webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook fired successfully for monitor %s to %s", job.id, job.webhook_url)
        return True
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Failed to fire webhook for monitor %s: %s", job.id, exc)
        return False


def _last_checked_ts(job: MonitorJob) -> float:
    """Parse ``job.last_checked`` (ISO-Z) to epoch seconds; ``0`` on failure."""
    if not job.last_checked:
        return 0.0
    try:
        return datetime.fromisoformat(job.last_checked.replace("Z", "+00:00")).timestamp()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Could not parse last_checked timestamp for monitor %s: %s", job.id, exc)
        return 0.0


def _is_due(job: MonitorJob, now: float) -> bool:
    """True iff ``job`` should be polled at ``now``."""
    return now - _last_checked_ts(job) >= job.interval_seconds


def _apply_check_result(job: MonitorJob, content_hash: str, now_iso: str) -> Optional[MonitorJob]:
    """Update ``job``'s baseline + return the job if a webhook should fire."""
    with _lock:
        if job.baseline_hash is None:
            job.baseline_hash = content_hash
            logger.info("Set baseline hash for monitor %s", job.id)
            return None
        if job.baseline_hash != content_hash:
            logger.info("Content changed for monitor %s: %s", job.id, job.url)
            job.baseline_hash = content_hash
            job.last_changed = now_iso
            return job
    return None


def _check_one_monitor(job: MonitorJob, now: float) -> None:
    """Run one due-check sweep for ``job``; fire webhook on change."""
    if job.status != "active" or not _is_due(job, now):
        return
    logger.debug("Checking monitor %s: %s", job.id, job.url)
    content = _fetch_url_content(job.url, job.selector)
    now_iso = _now_iso()
    with _lock:
        job.last_checked = now_iso
    if content is None:
        logger.warning("Failed to fetch content for monitor %s", job.id)
        return
    webhook_job = _apply_check_result(job, _hash_content(content), now_iso)
    if webhook_job is not None:
        _fire_webhook(webhook_job)


def _sweep_once() -> None:
    """One pass over every registered monitor."""
    now = time.time()
    with _lock:
        jobs = list(monitors.values())
    for job in jobs:
        _check_one_monitor(job, now)


def _safe_sweep_once() -> None:
    """Run one sweep, swallowing recoverable errors so the daemon thread survives."""
    try:
        _sweep_once()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.error("Error in monitor check loop: %s", exc)
        return None  # explicit recovery: BROWSER_SCRAPE_EXCEPTIONS is non-fatal; loop continues


def _check_monitors() -> None:
    """Background thread: sweep all active monitors forever."""
    while True:
        _safe_sweep_once()
        time.sleep(_SWEEP_SLEEP_SECONDS)


_monitor_thread = threading.Thread(target=_check_monitors, daemon=True, name="crawlrx_monitor")
_monitor_thread.start()
logger.info("Monitor background thread started")


@router.post("/monitor", response_model=MonitorResponse)
def create_monitor(request: MonitorRequest) -> MonitorResponse:
    """Register a URL to monitor for changes."""
    monitor_id = _generate_monitor_id()
    logger.info("Creating monitor %s for %s", monitor_id, request.url)
    content = _fetch_url_content(request.url, request.selector)
    if content is None:
        logger.warning(
            "Could not fetch initial content for %s, setting monitor to error state",
            request.url,
        )
        baseline_hash: Optional[str] = None
        status = "error"
    else:
        baseline_hash = _hash_content(content)
        status = "active"
        logger.info("Initial baseline hash set for monitor %s", monitor_id)
    job = MonitorJob(
        id=monitor_id,
        url=request.url,
        interval_seconds=request.interval_seconds,
        webhook_url=request.webhook_url,
        selector=request.selector,
        label=request.label,
        baseline_hash=baseline_hash,
        last_checked=_now_iso() if content else None,
        status=status,
    )
    with _lock:
        monitors[monitor_id] = job
    logger.info("Monitor %s created successfully (status=%s)", monitor_id, status)
    return MonitorResponse(
        monitor_id=monitor_id,
        url=request.url,
        interval_seconds=request.interval_seconds,
        status=status,
    )


@router.get("/monitor", response_model=MonitorListResponse)
def list_monitors() -> MonitorListResponse:
    """List all active monitors."""
    with _lock:
        monitor_list = [
            MonitorStatus(
                monitor_id=j.id,
                url=j.url,
                label=j.label,
                interval_seconds=j.interval_seconds,
                webhook_url=j.webhook_url,
                status=j.status,
                last_checked=j.last_checked,
                last_changed=j.last_changed,
            )
            for j in monitors.values()
        ]
    return MonitorListResponse(monitors=monitor_list, count=len(monitor_list))


@router.delete("/monitor/{monitor_id}")
def delete_monitor(monitor_id: str) -> dict:
    """Remove a monitor by ID."""
    with _lock:
        if monitor_id not in monitors:
            logger.warning("Attempted to delete non-existent monitor %s", monitor_id)
            raise HTTPException(status_code=404, detail="Monitor not found")
        del monitors[monitor_id]
    logger.info("Monitor %s deleted", monitor_id)
    return {"success": True, "monitor_id": monitor_id}
