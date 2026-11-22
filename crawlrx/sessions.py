"""
sessions.py -- Browser Session Info
====================================
CrawlRX uses self-contained browsers: each request launches fresh,
does its work, then closes completely. No persistent connections.

Endpoint:
    GET /sessions -- returns status info
"""

import logging
from fastapi import APIRouter

logger = logging.getLogger("crawlrx.sessions")

router = APIRouter(prefix="", tags=["sessions"])


@router.get("/sessions", summary="Browser session info", tags=["sessions"])
def get_sessions() -> dict:
    """
    Returns CrawlRX browser mode info.
    Self-contained mode: each request launches a fresh headless browser
    and closes it when done. No persistent CDP connections.
    """
    return {
        "mode": "self-contained",
        "description": "Each request launches a fresh headless Chromium, does its work, then closes completely.",
        "persistent_connections": 0,
        "cdp_connections": 0,
    }
