"""LinkedIn structured-profile extraction (split from linkedin.py).

Collects experience / education with typed Pydantic models, skills,
and connections count.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._linkedin_extract import (
    LOCATION_HINTS,
    first_text,
    first_text_with_hint,
)
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async

logger = logging.getLogger("crawlrx.linkedin.profile")

_NAME_SELECTORS = (
    "h1",
    "h1.text-heading-xlarge",
    ".pv-top-card__name",
)
_HEADLINE_SELECTORS = (
    ".text-body-medium",
    ".pv-top-card__headline",
    "[data-test-id='headline']",
)
_LOCATION_SELECTORS = (
    ".pv-top-card__location",
    ".text-body-small",
    "[data-test-id='location']",
)
_ABOUT_SELECTORS = (
    "section#about p",
    ".pv-about__summary-text",
    "[data-test-id='about-section']",
)


async def _build_experience_entry(
    item: Any, experience_factory: Any
) -> Optional[Any]:
    """Build one ``LinkedInExperience`` row from a list item."""
    title_el = await item.query_selector("h3, .t-bold")
    if title_el is None:
        return None
    company_el = await item.query_selector(
        ".t-14, .pv-entity__secondary-title"
    )
    duration_el = await item.query_selector(
        ".pv-entity__date-range, .date-range"
    )
    location_el = await item.query_selector(".pv-entity__location")
    return experience_factory(
        title=(await title_el.inner_text()).strip(),
        company=(await company_el.inner_text()).strip() if company_el else "",
        duration=(await duration_el.inner_text()).strip() if duration_el else None,
        location=(await location_el.inner_text()).strip() if location_el else None,
    )


async def _build_education_entry(
    item: Any, education_factory: Any
) -> Optional[Any]:
    """Build one ``LinkedInEducation`` row from a list item."""
    school_el = await item.query_selector("h3, .t-bold")
    if school_el is None:
        return None
    degree_el = await item.query_selector(
        ".pv-entity__degree-name, .t-14"
    )
    years_el = await item.query_selector(".pv-entity__dates")
    return education_factory(
        school=(await school_el.inner_text()).strip(),
        degree=(await degree_el.inner_text()).strip() if degree_el else None,
        years=(await years_el.inner_text()).strip() if years_el else None,
    )


async def _collect_experience(page: Any, experience_factory: Any) -> List[Any]:
    """Collect structured experience entries (typed Pydantic model)."""
    section = await page.query_selector(
        "section#experience, .experience-section"
    )
    if section is None:
        return []
    items = (await section.query_selector_all(
        "li.artdeco-list__item, .experience-item"
    ))[:10]
    out: List[Any] = []
    for item in items:
        entry = await safe_extract_async(
            lambda item=item: _build_experience_entry(item, experience_factory),
            field="li_experience",
            log=logger,
        )
        if entry:
            out.append(entry)
    return out


async def _collect_education(page: Any, education_factory: Any) -> List[Any]:
    """Collect structured education entries (typed Pydantic model)."""
    section = await page.query_selector(
        "section#education, .education-section"
    )
    if section is None:
        return []
    items = (await section.query_selector_all(
        "li.artdeco-list__item, .education-item"
    ))[:5]
    out: List[Any] = []
    for item in items:
        entry = await safe_extract_async(
            lambda item=item: _build_education_entry(item, education_factory),
            field="li_education",
            log=logger,
        )
        if entry:
            out.append(entry)
    return out


async def _collect_skills(page: Any) -> List[str]:
    """Collect skill strings (top 10)."""
    section = await page.query_selector("section#skills, .skills-section")
    if section is None:
        return []
    items = (await section.query_selector_all(
        ".pv-skill-category-entity__name, .skill-item"
    ))[:10]
    out: List[str] = []
    for item in items:
        try:
            text = (await item.inner_text()).strip()
            if text and 1 < len(text) < 100:
                out.append(text)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Skill parse error: %s", exc)
    return out


async def extract_structured_profile(
    page: "Page",
    experience_factory: "Callable[..., Any]",
    education_factory: "Callable[..., Any]",
) -> Dict[str, Any]:
    """Extract a structured profile dict (with typed experience/education)."""
    result: Dict[str, Any] = {
        "name": None,
        "headline": None,
        "location": None,
        "about": None,
        "experience": [],
        "education": [],
        "skills": [],
        "connections_approx": None,
    }
    result["name"] = await first_text(page, _NAME_SELECTORS)
    result["headline"] = await first_text(page, _HEADLINE_SELECTORS)
    result["location"] = await first_text_with_hint(
        page, _LOCATION_SELECTORS, LOCATION_HINTS
    )
    result["about"] = await first_text(page, _ABOUT_SELECTORS)
    result["experience"] = await _collect_experience(page, experience_factory)
    result["education"] = await _collect_education(page, education_factory)
    result["skills"] = await _collect_skills(page)
    conn_el = await page.query_selector(
        ".pv-top-card__connections, [data-test-id='connections']"
    )
    if conn_el is not None:
        result["connections_approx"] = (await conn_el.inner_text()).strip()
    return result
