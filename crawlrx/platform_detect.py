"""
platform_detect.py -- CrawlRX Platform/CMS Detection Engine
============================================================
Fingerprints any URL and returns what platform it runs on.

Detection pipeline (priority order):
  1. HEAD request       -- response headers (fast)
  2. robots.txt check   -- common Disallow paths
  3. Homepage HTML scan -- CDN URLs, JS globals, meta generators
  4. API probe          -- known platform API endpoints

Usage:
    from crawlrx.platform_detect import detect_platform, detect_platform_batch

    r = detect_platform('https://www.gymshark.com')
    # {'url': ..., 'platform': 'shopify', 'confidence': 0.95, 'signals': [...],
    #  'has_ecommerce': True, 'api_available': True}
"""

import logging
import importlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from ._platform_detect_helpers import (
    API_PROBES,
    DETECTOR_EXCEPTIONS,
    HTTP_PROBE_EXCEPTIONS,
    has_generic_ecommerce_signals,
    header_signals,
    html_simple_signals,
    html_woocommerce_signals,
    html_wordpress_signals,
    merge_scores,
    probe_api_endpoint,
    run_module_detectors,
)

logger = logging.getLogger("crawlrx.platform_detect")


# ── Platform detector imports (wrapped in try/except for optional modules) ───

def _safe_import(module: str, name: str) -> Optional[Callable[..., Any]]:
    """Import ``name`` from ``.<module>``; return ``None`` on ImportError."""
    try:
        mod = importlib.import_module(f".{module}", package=__package__)
    except ImportError as exc:
        logger.debug("%s detector not available: %s", module, exc)
        return None
    return getattr(mod, name, None)


detect_shopify = _safe_import("shopify", "detect_shopify")
detect_woocommerce = _safe_import("woocommerce", "detect_woocommerce")
detect_wordpress = _safe_import("wordpress", "detect_wordpress")
detect_squarespace = _safe_import("squarespace_scraper", "detect_squarespace")
detect_wix = _safe_import("wix_scraper", "detect_wix")
detect_bigcommerce = _safe_import("bigcommerce_scraper", "detect_bigcommerce")
detect_magento = _safe_import("magento_scraper", "detect_magento")
detect_prestashop = _safe_import("prestashop_scraper", "detect_prestashop")
detect_godaddy = _safe_import("godaddy_scraper", "detect_godaddy")
detect_weebly = _safe_import("weebly_scraper", "detect_weebly")
detect_square = _safe_import("square_scraper", "detect_square")
detect_ecwid = _safe_import("ecwid_scraper", "detect_ecwid")
detect_volusion = _safe_import("volusion_scraper", "detect_volusion")
detect_opencart = _safe_import("opencart_scraper", "detect_opencart")
detect_shift4shop = _safe_import("shift4shop_scraper", "detect_shift4shop")
detect_nopcommerce = _safe_import("nopcommerce_scraper", "detect_nopcommerce")
detect_cscart = _safe_import("cscart_scraper", "detect_cscart")
detect_drupal = _safe_import("drupal_scraper", "detect_drupal")
detect_ghost = _safe_import("ghost_scraper", "detect_ghost")
detect_joomla = _safe_import("joomla_scraper", "detect_joomla")
detect_duda = _safe_import("duda_scraper", "detect_duda")
detect_clickfunnels = _safe_import("clickfunnels_scraper", "detect_clickfunnels")
detect_kajabi = _safe_import("kajabi_scraper", "detect_kajabi")
detect_jimdo = _safe_import("jimdo_scraper", "detect_jimdo")
detect_tilda = _safe_import("tilda_scraper", "detect_tilda")

# ── FastAPI router (optional) ───
try:
    from fastapi import APIRouter
    from pydantic import BaseModel, Field
    _FASTAPI = True
except ImportError:
    _FASTAPI = False


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

PLATFORMS = [
    "shopify", "woocommerce", "bigcommerce", "magento", "prestashop",
    "wix", "squarespace", "godaddy", "weebly", "webflow", "wordpress",
    "opencart", "volusion", "shift4shop", "salesforce_commerce",
    "square", "ecwid", "nopcommerce", "cscart",
    "drupal", "ghost", "joomla", "duda",
    "clickfunnels", "kajabi", "jimdo", "tilda", "unknown",
]

ECOMMERCE_PLATFORMS = {
    "shopify", "woocommerce", "bigcommerce", "magento", "prestashop",
    "opencart", "volusion", "shift4shop", "salesforce_commerce",
    "square", "ecwid", "nopcommerce", "cscart",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def _step1_head(
    url: str, session: requests.Session, timeout: int
) -> Tuple[Optional[str], float, List[str]]:
    """Step 1 -- HEAD request: check headers for platform fingerprints."""
    try:
        resp = session.head(url, timeout=timeout, allow_redirects=True)
    except HTTP_PROBE_EXCEPTIONS as exc:
        logger.debug("HEAD failed for %s (%s): %s", url, type(exc).__name__, exc)
        return None, 0.0, []
    headers_lower = {k.lower(): v.lower() for k, v in resp.headers.items()}
    return header_signals(headers_lower)


def _step2_robots(
    url: str, session: requests.Session, timeout: int
) -> List[Tuple[str, float, List[str]]]:
    """Step 2 -- robots.txt: look for platform-specific Disallow paths."""
    robots_url = urljoin(url, "/robots.txt")
    try:
        resp = session.get(robots_url, timeout=timeout, allow_redirects=True)
    except HTTP_PROBE_EXCEPTIONS as exc:
        logger.debug(
            "robots.txt fetch failed for %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
        )
        return []
    if resp.status_code != 200:
        return []
    text = resp.text
    results: List[Tuple[str, float, List[str]]] = []
    if re.search(r"Disallow:\s*/admin/auth/login", text):
        results.append(("shopify", 0.85, ["robots.txt:Disallow:/admin/auth/login"]))
    if re.search(r"Disallow:\s*/wp-admin", text):
        results.append(("wordpress", 0.8, ["robots.txt:Disallow:/wp-admin"]))
    if re.search(r"Disallow:\s*/index\.php\?route=", text):
        # Can't distinguish without HTML -- record both candidates at lower confidence
        results.append(("opencart", 0.6, ["robots.txt:Disallow:/index.php?route="]))
        results.append(("prestashop", 0.6, ["robots.txt:Disallow:/index.php?route="]))
    return results


def _step3_html(
    url: str, session: requests.Session, timeout: int
) -> Tuple[Optional[str], List[Tuple[str, float, List[str]]]]:
    """Step 3 -- Homepage HTML scan: look for CDN URLs, JS globals, meta tags."""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except HTTP_PROBE_EXCEPTIONS as exc:
        logger.debug("HTML fetch failed for %s (%s): %s", url, type(exc).__name__, exc)
        return None, []
    html = resp.text
    results: List[Tuple[str, float, List[str]]] = []
    results.extend(html_simple_signals(html))
    results.extend(html_woocommerce_signals(html))
    has_woocommerce = any(p == "wordpress" for p, _, _ in results)
    if not has_woocommerce:
        results.extend(html_wordpress_signals(html))
    return html, results


def _step4_api(
    url: str, platform: str, session: requests.Session, timeout: int
) -> bool:
    """Step 4 -- API probe: try well-known API endpoints for ``platform``."""
    for path in API_PROBES.get(platform, []):
        if probe_api_endpoint(session, url, platform, path, timeout):
            return True
    return False


def _build_detector_list() -> List[Tuple[str, Optional[Callable[..., Any]], float]]:
    """Static list of ``(name, detector, confidence)`` triples for step 5."""
    return [
        ("shopify", detect_shopify, 0.95),
        ("woocommerce", detect_woocommerce, 0.95),
        ("wordpress", detect_wordpress, 0.90),
        ("squarespace", detect_squarespace, 0.95),
        ("wix", detect_wix, 0.95),
        ("bigcommerce", detect_bigcommerce, 0.95),
        ("magento", detect_magento, 0.95),
        ("prestashop", detect_prestashop, 0.95),
        ("godaddy", detect_godaddy, 0.95),
        ("weebly", detect_weebly, 0.95),
        ("square", detect_square, 0.95),
        ("ecwid", detect_ecwid, 0.95),
        ("volusion", detect_volusion, 0.95),
        ("opencart", detect_opencart, 0.95),
        ("shift4shop", detect_shift4shop, 0.95),
        ("nopcommerce", detect_nopcommerce, 0.95),
        ("cscart", detect_cscart, 0.95),
        ("drupal", detect_drupal, 0.95),
        ("ghost", detect_ghost, 0.95),
        ("joomla", detect_joomla, 0.95),
        ("duda", detect_duda, 0.95),
        ("clickfunnels", detect_clickfunnels, 0.95),
        ("kajabi", detect_kajabi, 0.95),
        ("jimdo", detect_jimdo, 0.95),
        ("tilda", detect_tilda, 0.95),
    ]


def _step5_module_detectors(url: str, timeout: int) -> List[Tuple[str, float, List[str]]]:
    """Step 5 -- Module-specific detectors: call each ``detect_*`` function."""
    return run_module_detectors(_build_detector_list(), url, timeout)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class _DetectCtx:
    """Bundle of (url, session, timeout) -- keeps step helpers ≤5 args."""

    url: str
    session: requests.Session
    timeout: int


def _empty_result(url: str) -> Dict[str, Any]:
    """Default zero-state detection result."""
    return {
        "url": url,
        "platform": "unknown",
        "confidence": 0.0,
        "signals": [],
        "has_ecommerce": False,
        "api_available": False,
    }


def _finalise_high_confidence(
    head_match: Tuple[str, float, List[str]],
    ctx: _DetectCtx,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Short-circuit: HEAD already gave confidence >= 0.95."""
    platform, confidence, signals = head_match
    result["platform"] = platform
    result["confidence"] = confidence
    result["signals"] = list(signals)
    result["has_ecommerce"] = platform in ECOMMERCE_PLATFORMS
    result["api_available"] = _step4_api(ctx.url, platform, ctx.session, ctx.timeout)
    if result["api_available"]:
        result["signals"].append(f"api_probe:{platform}")
    return result


def _gather_detections(
    url: str, session: requests.Session, timeout: int
) -> Tuple[List[Tuple[str, float, List[str]]], Optional[str]]:
    """Run steps 2/3/5 (skip step 4); return detections + raw HTML."""
    detections: List[Tuple[str, float, List[str]]] = []
    detections.extend(_step2_robots(url, session, timeout))
    html, html_results = _step3_html(url, session, timeout)
    detections.extend(html_results)
    detections.extend(_step5_module_detectors(url, timeout))
    return detections, html


def _resolve_best_platform(
    scores: Dict[str, Tuple[float, List[str]]],
) -> Tuple[str, float, List[str]]:
    """Pick the highest-confidence platform from a merged-scores map."""
    best = max(scores.keys(), key=lambda p: (scores[p][0], len(scores[p][1])))
    confidence, signals = scores[best]
    return best, confidence, signals


def _apply_api_probe(
    best: Tuple[str, float, List[str]],
    ctx: _DetectCtx,
) -> Tuple[float, List[str], bool]:
    """Run the API probe; bump confidence + signals on success."""
    name, confidence, signals = best
    api_avail = _step4_api(ctx.url, name, ctx.session, ctx.timeout)
    if api_avail:
        signals.append(f"api_probe:{name}")
        confidence = min(confidence + 0.05, 1.0)
    return confidence, signals, api_avail


def _detect_platform_inner(url: str, timeout: int, result: Dict[str, Any]) -> Dict[str, Any]:
    """Run the full pipeline; on no-match, fall back to generic ecommerce."""
    ctx = _DetectCtx(url=url, session=_make_session(), timeout=timeout)

    # ── Step 1: HEAD ───────────────────────────────────────────────────────
    head_platform, head_conf, head_signals = _step1_head(url, ctx.session, timeout)
    detections: List[Tuple[str, float, List[str]]] = []
    if head_platform:
        detections.append((head_platform, head_conf, head_signals))
        if head_conf >= 0.95:
            return _finalise_high_confidence(
                (head_platform, head_conf, head_signals),
                ctx,
                result,
            )

    # ── Steps 2/3/5 ────────────────────────────────────────────────────────
    extra, html = _gather_detections(url, ctx.session, timeout)
    detections.extend(extra)

    scores = merge_scores(detections)
    if "woocommerce" in scores and "wordpress" not in scores:
        del scores["woocommerce"]

    if not scores and html and has_generic_ecommerce_signals(html):
        result["platform"] = "generic"
        result["confidence"] = 0.3
        result["signals"] = ["generic:ecommerce_signals_detected"]
        result["has_ecommerce"] = True
        return result
    if not scores:
        return result

    best_triple = _resolve_best_platform(scores)
    best_conf, best_signals, api_avail = _apply_api_probe(best_triple, ctx)
    best_name = best_triple[0]
    result["platform"] = best_name
    result["confidence"] = round(best_conf, 4)
    result["signals"] = best_signals
    result["has_ecommerce"] = best_name in ECOMMERCE_PLATFORMS
    result["api_available"] = api_avail
    return result


def detect_platform(url: str, timeout: int = 8) -> dict:
    """Fingerprint a URL and return which platform it runs on (never raises)."""
    url = _normalize_url(url)
    result = _empty_result(url)
    try:
        return _detect_platform_inner(url, timeout, result)
    except DETECTOR_EXCEPTIONS as exc:
        logger.error(
            "detect_platform error for %s (%s): %s",
            url,
            type(exc).__name__,
            exc,
        )
        return result


def detect_platform_batch(urls: list, workers: int = 5) -> list:
    """Detect platforms for multiple URLs in parallel (input order preserved)."""
    results: Dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(detect_platform, url): i for i, url in enumerate(urls)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except DETECTOR_EXCEPTIONS as exc:
                results[idx] = {
                    "url": urls[idx],
                    "platform": "unknown",
                    "confidence": 0.0,
                    "signals": [f"batch_error:{type(exc).__name__}:{exc}"],
                    "has_ecommerce": False,
                    "api_available": False,
                }
    return [results[i] for i in range(len(urls))]


# ─────────────────────────────────────────────
# FastAPI router (optional)
# ─────────────────────────────────────────────

if _FASTAPI:
    router = APIRouter(prefix="", tags=["platform"])

    class PlatformDetectRequest(BaseModel):
        url: str = Field(..., description="URL to fingerprint")
        timeout: int = Field(default=8, description="Request timeout in seconds")

    class PlatformDetectResponse(BaseModel):
        url: str
        platform: str
        confidence: float
        signals: List[str]
        has_ecommerce: bool
        api_available: bool
        elapsed_ms: int
        error: Optional[str] = None

    @router.post(
        "/detect/platform",
        response_model=PlatformDetectResponse,
        summary="Detect website platform/CMS",
    )
    def detect_platform_endpoint(req: PlatformDetectRequest) -> PlatformDetectResponse:
        """Fingerprint a URL and return the platform/CMS it runs on."""
        t0 = time.time()
        try:
            r = detect_platform(req.url, timeout=req.timeout)
            return PlatformDetectResponse(
                elapsed_ms=int((time.time() - t0) * 1000),
                **r,
            )
        except DETECTOR_EXCEPTIONS as exc:
            return PlatformDetectResponse(
                url=req.url,
                platform="unknown",
                confidence=0.0,
                signals=[],
                has_ecommerce=False,
                api_available=False,
                elapsed_ms=int((time.time() - t0) * 1000),
                error=str(exc),
            )
