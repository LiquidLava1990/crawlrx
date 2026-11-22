"""LinkedIn company-page extraction (split from linkedin.py)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async

logger = logging.getLogger("crawlrx.linkedin.company")


_DETAIL_FIELD_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("industry",), "industry"),
    (("size", "employees"), "size"),
    (("founded",), "founded"),
    (("headquarters",), "headquarters"),
    (("specialties",), "specialties"),
)


async def _classify_detail(detail: Any) -> Optional[Tuple[str, str]]:
    """Map one company-detail node to ``(field_name, value)``."""
    text = (await detail.inner_text()).strip().lower()
    if not text:
        return None
    value_el = await detail.query_selector("+ dd, .value")
    if value_el is None:
        return None
    value = (await value_el.inner_text()).strip()
    for needles, field in _DETAIL_FIELD_RULES:
        if any(needle in text for needle in needles):
            return field, value
    if "website" in text or "www." in value:
        return "website", value
    return None


async def _collect_details(page: Any) -> Dict[str, Any]:
    """Walk the structured company-detail nodes into a dict."""
    out: Dict[str, Any] = {}
    nodes = await page.query_selector_all(
        ".org-page-details__definition-term, .company-details-item"
    )
    for node in nodes:
        try:
            classified = await _classify_detail(node)
        except BROWSER_SCRAPE_EXCEPTIONS:
            continue
        if classified is None:
            continue
        field, value = classified
        out[field] = (
            [s.strip() for s in value.split(",")]
            if field == "specialties"
            else value
        )
    return out


async def _build_company_post(post: Any, post_factory: Any) -> Optional[Any]:
    """Build one ``LinkedInCompanyPost`` from a feed item."""
    text_el = await post.query_selector(".feed-shared-text, .post-text")
    date_el = await post.query_selector(".feed-shared-time, .post-time")
    engagement_el = await post.query_selector(
        ".feed-shared-social-actions, .engagement"
    )
    return post_factory(
        text=(await text_el.inner_text()).strip() if text_el else None,
        date=(await date_el.inner_text()).strip() if date_el else None,
        engagement=(await engagement_el.inner_text()).strip()
        if engagement_el else None,
    )


async def _collect_recent_posts(page: Any, post_factory: Any) -> List[Any]:
    """Collect up to 3 recent company posts."""
    posts = (await page.query_selector_all(
        ".org-social-post, .feed-shared-update-v2"
    ))[:3]
    out: List[Any] = []
    for post in posts:
        entry = await safe_extract_async(
            lambda post=post: _build_company_post(post, post_factory),
            field="company_post",
            log=logger,
        )
        if entry:
            out.append(entry)
    return out


async def _populate_top_card(page: Any, result: Dict[str, Any]) -> None:
    """Populate name / tagline / about from the top card / about section."""
    name_el = await page.query_selector(
        "h1.org-top-card__name, h1[data-test-id='company-name']"
    )
    if name_el is not None:
        result["name"] = (await name_el.inner_text()).strip()

    tagline_el = await page.query_selector(
        ".org-top-card__tagline, [data-test-id='tagline']"
    )
    if tagline_el is not None:
        result["tagline"] = (await tagline_el.inner_text()).strip()

    about_section = await page.query_selector("section#about, .about-section")
    if about_section is not None:
        about_text = await about_section.query_selector("p, .description")
        if about_text is not None:
            result["about"] = (await about_text.inner_text()).strip()


async def extract_company(page: "Page", post_factory: "Callable[..., Any]") -> Dict[str, Any]:
    """Extract a company-page payload from a loaded LinkedIn page."""
    result: Dict[str, Any] = {
        "name": None,
        "tagline": None,
        "about": None,
        "industry": None,
        "size": None,
        "founded": None,
        "headquarters": None,
        "website": None,
        "followers": None,
        "specialties": [],
        "recent_posts": [],
    }
    await _populate_top_card(page, result)
    result.update(await _collect_details(page))

    followers_el = await page.query_selector(
        ".org-top-card__followers-count, [data-test-id='followers']"
    )
    if followers_el is not None:
        result["followers"] = (await followers_el.inner_text()).strip()

    result["recent_posts"] = await _collect_recent_posts(page, post_factory)
    return result
