"""Knoji scraper for CrawlRX -- scrape coupon codes from knoji.com."""

import logging
import time
from typing import Any

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from pydantic import BaseModel

from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

CODE_SELECTORS = [
    ".coupon-code", "[data-coupon]", ".code", "span.code", ".promo-code", ".discount-code"
]
DISCOUNT_SELECTORS = [
    ".discount", ".savings", ".offer", ".deal", ".amount", ".percentage",
    "[class*='discount']", "[class*='save']", "[class*='off']",
]
DESC_SELECTORS = [
    ".description", ".details", ".info", ".text", "p", "[class*='desc']",
]


class KnojiRequest(BaseModel):
    merchant: str


class KnojiResponse(BaseModel):
    success: bool
    codes: list[dict]
    elapsed_ms: int


def _extract_domain_without_tld(merchant: str) -> str:
    """Return base domain name without TLD (e.g. 'bestbuy.com' -> 'bestbuy')."""
    if not merchant or not isinstance(merchant, str):
        return ""
    clean = merchant.strip().lower()
    if "://" in clean:
        clean = clean.split("://")[1]
    if clean.startswith("www."):
        clean = clean[4:]
    if "/" in clean:
        clean = clean.split("/")[0]
    parts = clean.split(".")
    return parts[0] if len(parts) >= 2 else clean


def _first_text(parent, selectors: list) -> str:
    """Return first non-empty text found via CSS selectors within parent."""
    for sel in selectors:
        elem = parent.select_one(sel)
        if elem:
            text = elem.get_text(strip=True)
            if text:
                return text
    return ""


def _parse_code_element(elem) -> dict | None:
    """Parse a BS4 element into a coupon dict. Returns None if code is invalid."""
    code = elem.get_text(strip=True) or elem.get("data-coupon", "").strip()
    if not code or len(code) > 50:
        return None
    parent = elem.find_parent(["div", "li", "article", "section"]) or elem
    discount = _first_text(parent, DISCOUNT_SELECTORS)
    description = _first_text(parent, DESC_SELECTORS)
    if not description:
        description = discount
    if not discount:
        discount = description
    return {
        "code": code,
        "discount": discount,
        "description": description,
        "source": "knoji",
        "verified": False,
    }


_REQUEST_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
_FALLBACK_PARENT_CLASSES: tuple[str, ...] = ("coupon", "code", "promo", "deal")


def _fetch_knoji_html(url: str, merchant: str) -> str:
    """GET the Knoji coupons page; '' on error / 404."""
    try:
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        logger.warning("Knoji timeout for %s", merchant)
        return ""
    except requests.exceptions.RequestException as exc:
        logger.warning("Knoji request failed for %s: %s", merchant, exc)
        return ""
    if resp.status_code == 404:
        logger.info("Knoji 404 for %s", merchant)
        return ""
    try:
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Knoji status error for %s: %s", merchant, exc)
        return ""
    return resp.text


def _primary_code_results(soup: BeautifulSoup) -> list[dict]:
    """Run the primary CSS-selector pass over a Knoji page."""
    results: list[dict] = []
    code_elements: list[Any] = []
    for selector in CODE_SELECTORS:
        code_elements = soup.select(selector)
        if code_elements:
            logger.debug("Knoji: %d elements via selector '%s'", len(code_elements), selector)
            break
    for elem in code_elements:
        try:
            entry = _parse_code_element(elem)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Knoji element parse error: %s", exc)
            continue
        if entry:
            results.append(entry)
    return results


def _fallback_uppercase_codes(soup: BeautifulSoup) -> list[dict]:
    """Fallback pass: find UPPERCASE-token candidates inside coupon containers."""
    results: list[dict] = []
    for text in soup.find_all(text=lambda t: t and 4 <= len(t.strip()) < 30):
        text_clean = text.strip()
        if not text_clean.isupper():
            continue
        parent = text.find_parent(["div", "li", "span"])
        if not parent:
            continue
        cls = " ".join(parent.get("class", [])).lower()
        if not any(needle in cls for needle in _FALLBACK_PARENT_CLASSES):
            continue
        results.append({
            "code": text_clean,
            "discount": "",
            "description": "",
            "source": "knoji",
            "verified": False,
        })
    return results


def scrape_knoji(merchant: str) -> list[dict]:
    """Scrape coupon codes from knoji.com for the given merchant domain."""
    if not merchant or not isinstance(merchant, str):
        logger.warning("Invalid merchant provided to scrape_knoji: %s", merchant)
        return []
    domain_clean = _extract_domain_without_tld(merchant)
    if not domain_clean:
        logger.warning("Could not extract domain from merchant: %s", merchant)
        return []
    url = f"https://knoji.com/coupons/{domain_clean}/"
    logger.info("Scraping Knoji for %s: %s", merchant, url)
    html = _fetch_knoji_html(url, merchant)
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Knoji parse error for %s: %s", merchant, exc)
        return []
    results = _primary_code_results(soup)
    if not results:
        logger.debug("Knoji fallback search for %s", merchant)
        results = _fallback_uppercase_codes(soup)
    logger.info("Knoji: %d codes found for %s", len(results), merchant)
    return results


@router.post("/scrape/knoji")
def api_scrape_knoji(request: KnojiRequest) -> dict:
    """POST /scrape/knoji -- scrape coupon codes from knoji.com."""
    start_time = time.time()
    logger.info("POST /scrape/knoji - merchant: %s", request.merchant)
    try:
        codes = scrape_knoji(request.merchant)
        return {"success": True, "codes": codes, "elapsed_ms": int((time.time() - start_time) * 1000)}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("api_scrape_knoji error: %s", e)
        return {"success": False, "codes": [], "elapsed_ms": int((time.time() - start_time) * 1000)}
