"""LinkedIn DOM extraction primitives (async, selector-walking only).

Provides the text-only profile extractor used by ``/scrape/linkedin``
plus shared selector-walk helpers consumed by sibling structured /
company / jobs scrapers.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.linkedin.extract")

_LOGIN_INDICATORS: Tuple[str, ...] = (
    "linkedin.com/login",
    "linkedin.com/signup",
    "session_redirect",
    "checkpoint",
    "authwall",
)

LOCATION_HINTS: Tuple[str, ...] = (
    "area",
    "region",
    "city",
    "country",
    ", ",
)


def is_login_page(html: str, url: str) -> bool:
    """True if the page or URL look like LinkedIn's login wall."""
    url_lower = url.lower()
    if any(needle in url_lower for needle in _LOGIN_INDICATORS):
        return True
    html_lower = html.lower()
    if "sign in" in html_lower and (
        "password" in html_lower or "email" in html_lower
    ):
        return True
    return False


async def first_text(
    page_or_node: "Page | ElementHandle", selectors: Tuple[str, ...]
) -> Optional[str]:
    """First-matching descendant's stripped inner text, or ``None``."""
    for selector in selectors:
        elem = await page_or_node.query_selector(selector)
        if elem is None:
            continue
        text = (await elem.inner_text()).strip()
        if text:
            return text
    return None


async def first_text_with_hint(
    page: "Page",
    selectors: Tuple[str, ...],
    hints: Tuple[str, ...],
) -> Optional[str]:
    """First-matching text whose lowercased form contains any of ``hints``."""
    for selector in selectors:
        elem = await page.query_selector(selector)
        if elem is None:
            continue
        text = (await elem.inner_text()).strip()
        if text and any(hint in text.lower() for hint in hints):
            return text
    return None


# Selector tuples for the text-only ("flat") profile shape ------------------

_FLAT_NAME_SELECTORS = (
    "h1.inline.t-24.v-align-middle.break-words",
    "h1.top-card-layout__title",
    "h1.profile-card-name",
    "h1.text-heading-xlarge",
)
_FLAT_HEADLINE_SELECTORS = (
    "div.text-body-medium.break-words",
    "div.top-card-layout__headline",
    "div.profile-card-subline",
    "h2.text-heading-medium",
)
_FLAT_LOCATION_SELECTORS = (
    "span.text-body-small.inline.t-black--light.break-words",
    "span.top-card__subline-item",
    "div.profile-card-location",
)
_FLAT_ABOUT_SELECTORS = (
    "section.summary div.core-section-container__content p",
    "section.about div.core-section-container__content p",
    "div.inline-show-more-text--is-collapsed",
    "section#about p",
)


async def _flat_experience_text(item: Any) -> Optional[str]:
    """Format one flat-shape experience list-item into ``"<title> at <co>"``."""
    title = await item.query_selector(
        "h3, .experience-item__title, span[aria-hidden='true']"
    )
    if title is None:
        return None
    text = (await title.inner_text()).strip()
    company = await item.query_selector(
        "p, .experience-item__subtitle, .t-14"
    )
    if company is not None:
        comp_text = (await company.inner_text()).strip()
        if comp_text and comp_text != text:
            text = f"{text} at {comp_text}"
    return text if text and len(text) > 3 else None


async def _flat_collect_experience(page: Any) -> List[str]:
    """Collect up to 10 flat-shape experience strings."""
    section = await page.query_selector(
        "section#experience, section.experience"
    )
    if section is None:
        return []
    nodes = (await section.query_selector_all(
        "li, .experience-item, .profile-section-card"
    ))[:10]
    items: List[str] = []
    for node in nodes:
        try:
            text = await _flat_experience_text(node)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Experience item parse error: %s", exc)
            continue
        if text:
            items.append(text)
    return items


async def _flat_collect_education(page: Any) -> List[str]:
    """Collect up to 5 flat-shape education strings."""
    section = await page.query_selector(
        "section#education, section.education"
    )
    if section is None:
        return []
    nodes = (await section.query_selector_all(
        "li, .education__list-item, .profile-section-card"
    ))[:5]
    items: List[str] = []
    for node in nodes:
        try:
            school = await node.query_selector(
                "h3, .education__item-title, span[aria-hidden='true']"
            )
            if school is None:
                continue
            text = (await school.inner_text()).strip()
            if text and len(text) > 3:
                items.append(text)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Education item parse error: %s", exc)
    return items


async def _flat_collect_skills(page: Any) -> List[str]:
    """Collect up to 20 flat-shape skill strings."""
    section = await page.query_selector("section#skills, section.skills")
    if section is None:
        return []
    nodes = (await section.query_selector_all(
        "li, .skill-entity__skill-name, span[aria-hidden='true']"
    ))[:20]
    items: List[str] = []
    for node in nodes:
        try:
            text = (await node.inner_text()).strip()
            if text and 1 < len(text) < 100:
                items.append(text)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Skill item parse error: %s", exc)
    return items


async def extract_flat_profile(page: "Page") -> Dict[str, Any]:
    """Extract a text-only profile dict from a loaded LinkedIn page."""
    result: Dict[str, Any] = {
        "name": None,
        "headline": None,
        "location": None,
        "about": None,
        "experience": [],
        "education": [],
        "skills": [],
    }
    try:
        result["name"] = await first_text(page, _FLAT_NAME_SELECTORS)
        result["headline"] = await first_text(page, _FLAT_HEADLINE_SELECTORS)
        result["location"] = await first_text_with_hint(
            page, _FLAT_LOCATION_SELECTORS, LOCATION_HINTS
        )
        result["about"] = await first_text(page, _FLAT_ABOUT_SELECTORS)
        result["experience"] = await _flat_collect_experience(page)
        result["education"] = await _flat_collect_education(page)
        result["skills"] = await _flat_collect_skills(page)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Error extracting LinkedIn profile data: %s", exc)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return result
