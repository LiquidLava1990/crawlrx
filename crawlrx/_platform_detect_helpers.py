"""Helpers for platform_detect.py (split for the function-length limit)."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

logger = logging.getLogger("crawlrx._platform_detect_helpers")

# Narrow exception family for an outbound HTTP probe. Covers connection /
# timeout / decode / TLS errors raised by ``requests``.
HTTP_PROBE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    OSError,
    ValueError,
    UnicodeDecodeError,
)

# Detector callables can raise anything they want; we catch a broad-but-named
# tuple so a single broken detector doesn't take the whole run down.
DETECTOR_EXCEPTIONS: tuple[type[BaseException], ...] = (
    HTTP_PROBE_EXCEPTIONS
    + (
        AttributeError,
        KeyError,
        TypeError,
        IndexError,
        RuntimeError,
    )
)


# ─────────────────────────────────────────────
# Step-1 HEAD helper
# ─────────────────────────────────────────────


def _shopify_header_match(headers: Dict[str, str]) -> Optional[Tuple[str, float, List[str]]]:
    """Detect Shopify via ``x-shopify-*`` headers."""
    for key in ("x-shopify-stage", "x-shopify-request-id"):
        if key in headers:
            return "shopify", 0.95, [f"header:{key}"]
    return None


def header_signals(headers: Dict[str, str]) -> Tuple[Optional[str], float, List[str]]:
    """Map a lowercase HEAD response header dict to a platform fingerprint."""
    shopify_match = _shopify_header_match(headers)
    if shopify_match is not None:
        return shopify_match
    if "x-bc-request-id" in headers:
        return "bigcommerce", 0.95, ["header:x-bc-request-id"]
    server = headers.get("server", "")
    if "squarespace" in server:
        return "squarespace", 0.9, ["header:server=squarespace"]
    if any(k.startswith("x-wix-") for k in headers):
        return "wix", 0.9, ["header:x-wix-*"]
    return None, 0.0, []


# ─────────────────────────────────────────────
# Step-4 API probe helper
# ─────────────────────────────────────────────

API_PROBES: Dict[str, List[str]] = {
    "shopify": ["/products.json"],
    "woocommerce": ["/wp-json/wc/store/v1/products?per_page=1"],
    "bigcommerce": ["/api/storefront/catalog/products"],
    "wordpress": ["/wp-json/wp/v2/posts?per_page=1"],
}


def _is_json_response(resp: requests.Response) -> bool:
    """True if the response advertises (or starts as) JSON."""
    if "json" in resp.headers.get("content-type", ""):
        return True
    return resp.text.strip().startswith(("[", "{"))


def _validate_api_payload(platform: str, body: str) -> bool:
    """Confirm that ``body`` matches the expected shape for ``platform``."""
    body_stripped = body.strip()
    if platform == "shopify":
        return '"products"' in body
    if platform == "woocommerce":
        return '"items"' in body or '"products"' in body
    if platform in ("bigcommerce", "wordpress"):
        return body_stripped.startswith("[")
    return False


def probe_api_endpoint(
    session: requests.Session,
    base_url: str,
    platform: str,
    path: str,
    timeout: int,
) -> bool:
    """Make a single API probe; ``True`` only on a 200 with valid JSON shape."""
    probe_url = urljoin(base_url, path)
    try:
        resp = session.get(probe_url, timeout=min(timeout, 6), allow_redirects=True)
    except HTTP_PROBE_EXCEPTIONS as exc:
        logger.debug(
            "API probe failed for %s at %s (%s): %s",
            platform,
            probe_url,
            type(exc).__name__,
            exc,
        )
        return False
    if resp.status_code != 200:
        return False
    if not _is_json_response(resp):
        return False
    return _validate_api_payload(platform, resp.text)


# ─────────────────────────────────────────────
# Step-3 HTML pattern helpers
# ─────────────────────────────────────────────

# (pattern, signal-tag, platform-name, confidence)
_SIMPLE_HTML_RULES: tuple[tuple[str, str, str, float], ...] = (
    (r"cdn\.shopify\.com", "cdn.shopify.com", "shopify", 0.9),
    (r"Shopify\.theme", "Shopify.theme", "shopify", 0.9),
    (r"/products\.json", "/products.json", "shopify", 0.9),
    (r"static\.parastorage\.com", "static.parastorage.com", "wix", 0.95),
    (r"wixstatic\.com", "wixstatic.com", "wix", 0.95),
    (r"static1?\.squarespace\.com", "static.squarespace.com", "squarespace", 0.95),
    (r"cdn\d*\.bigcommerce\.com", "cdn.bigcommerce.com", "bigcommerce", 0.9),
    (r"bc-sf-filter", "bc-sf-filter", "bigcommerce", 0.9),
    (r"Mage\.Cookies", "Mage.Cookies", "magento", 0.9),
    (r"MAGE_", "MAGE_", "magento", 0.9),
    (r"/skin/frontend/", "/skin/frontend/", "magento", 0.9),
    (r"img1\.wsimg\.com", "img1.wsimg.com", "godaddy", 0.85),
    (r"godaddy\.com/hosting", "godaddy.com/hosting", "godaddy", 0.85),
    (r"editmysite\.com", "editmysite.com", "weebly", 0.9),
    (r"weebly\.com", "weebly.com", "weebly", 0.9),
    (r"webflow\.io", "webflow.io", "webflow", 0.9),
    (r"Webflow\.require", "Webflow.require", "webflow", 0.9),
    (r"prestashop", "prestashop", "prestashop", 0.85),
    (r"/themes/default-bootstrap/", "/themes/default-bootstrap/", "prestashop", 0.85),
)


def html_simple_signals(html: str) -> List[Tuple[str, float, List[str]]]:
    """Run the simple regex-equality rules over ``html`` and emit matches."""
    out: List[Tuple[str, float, List[str]]] = []
    for pattern, signal, platform, confidence in _SIMPLE_HTML_RULES:
        if re.search(pattern, html, re.IGNORECASE):
            out.append((platform, confidence, [f"html:{signal}"]))
    return out


def html_woocommerce_signals(html: str) -> List[Tuple[str, float, List[str]]]:
    """Detect WooCommerce + the implied WordPress signal."""
    if not re.search(r"wp-content/plugins/woocommerce", html, re.IGNORECASE) \
       and not re.search(r"wc-api", html, re.IGNORECASE):
        return []
    sig = (
        "wp-content/plugins/woocommerce"
        if "woocommerce" in html.lower()
        else "wc-api"
    )
    return [
        ("woocommerce", 0.95, [f"html:{sig}"]),
        ("wordpress", 0.8, ["html:woocommerce->wordpress"]),
    ]


def html_wordpress_signals(html: str) -> List[Tuple[str, float, List[str]]]:
    """Detect bare WordPress (caller drops dup if WooCommerce already added)."""
    if not re.search(r"wp-content/", html, re.IGNORECASE) \
       and not re.search(r"wp-includes/", html, re.IGNORECASE):
        return []
    pat = "wp-content/" if "wp-content/" in html else "wp-includes/"
    return [("wordpress", 0.8, [f"html:{pat}"])]


# ─────────────────────────────────────────────
# Generic ecommerce fallback
# ─────────────────────────────────────────────


_ECOMMERCE_PATTERNS = (
    r"\$\d+\.\d{2}",
    r"add\s*to\s*cart",
    r"addtocart",
    r"data-product",
    r"product[_-]price",
    r"price[_-]amount",
    r"schema\.org/Product",
    r'"@type"\s*:\s*"Product"',
    r"og:price",
    r"product:price",
    r"class=[\"'][^\"']*product[^\"']*[\"']",
)


def has_generic_ecommerce_signals(html: str) -> bool:
    """Return True when ≥2 generic ecommerce patterns hit ``html``."""
    matches = 0
    for pattern in _ECOMMERCE_PATTERNS:
        if re.search(pattern, html, re.IGNORECASE):
            matches += 1
        if matches >= 2:
            return True
    html_lower = html.lower()
    return (
        html_lower.count("product") > 5
        and ("price" in html_lower or "cart" in html_lower)
    )


# ─────────────────────────────────────────────
# Detector dispatch
# ─────────────────────────────────────────────


def run_module_detectors(
    detectors: List[Tuple[str, Optional[Callable[..., Any]], float]],
    url: str,
    timeout: int,
) -> List[Tuple[str, float, List[str]]]:
    """Call each platform-module detector; collect ``(platform, conf, signals)``."""
    results: List[Tuple[str, float, List[str]]] = []
    for platform_name, detector, confidence in detectors:
        if detector is None:
            continue
        try:
            detected = detector(url, timeout=timeout)
        except DETECTOR_EXCEPTIONS as exc:
            logger.debug(
                "Detector %s failed for %s (%s): %s",
                platform_name,
                url,
                type(exc).__name__,
                exc,
            )
            continue
        if detected:
            results.append(
                (platform_name, confidence, [f"module_detector:{platform_name}"])
            )
    return results


def merge_scores(
    results: List[Tuple[str, float, List[str]]],
) -> Dict[str, Tuple[float, List[str]]]:
    """Merge multiple ``(platform, confidence, signals)`` tuples by platform."""
    scores: Dict[str, Tuple[float, List[str]]] = {}
    for platform, conf, signals in results:
        if platform in scores:
            existing_conf, existing_signals = scores[platform]
            merged_signals = existing_signals + [
                s for s in signals if s not in existing_signals
            ]
            scores[platform] = (max(existing_conf, conf), merged_signals)
        else:
            scores[platform] = (conf, list(signals))
    return scores
