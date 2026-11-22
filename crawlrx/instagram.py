"""
instagram.py -- Instagram profile scraper
Endpoint: POST /scrape/instagram

Supports both CDP (logged-in browser) and headless modes.
CDP recommended for avoiding login walls and getting post data.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Optional, List, Dict, Any

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from fastapi import APIRouter
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from .models import InstagramRequest, InstagramResponse, InstagramData, InstagramPost
from .utils import timer_ms
from .stealth_context import make_stealth_context, close_stealth_context
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS

logger = logging.getLogger("crawlrx.instagram")
router = APIRouter(prefix="/scrape", tags=["scrape"])


def _extract_count(text: str) -> str:
    """Extract number from text like '1.2M' or '5,432'"""
    if not text:
        return ""
    # Remove whitespace and normalize
    text = text.strip().replace(",", "").replace(" ", "")
    # Match patterns like 1.2M, 5K, 1234
    match = re.search(r'([\d.]+)([MK]?)', text, re.IGNORECASE)
    if match:
        num, suffix = match.groups()
        if suffix.upper() == "M":
            return text  # Keep original format like "1.2M"
        elif suffix.upper() == "K":
            return text  # Keep original format like "5.1K"
        return num
    return text


def _empty_profile_data() -> Dict[str, Any]:
    """Skeleton dict every Instagram profile probe starts with."""
    return {
        "name": "", "bio": "", "followers": "", "following": "",
        "posts_count": "", "is_verified": False, "profile_pic": "",
        "recent_posts": [],
    }


async def _profile_name_from_title(page: Any) -> str:
    """Read 'Name' out of the 'Name (@user) - Instagram' page title."""
    title = await page.title()
    if not (title and "(@" in title):
        return ""
    match = re.match(r'(.+?)\s*\(@', title)
    return match.group(1).strip() if match else ""


def _parse_meta_desc_counts(desc: str, data: Dict[str, Any]) -> None:
    """Populate followers/following/posts_count from a meta-description blob."""
    if "Followers" not in desc:
        return
    parts = desc.split(" - ")[0]
    for label, key in (("Followers", "followers"), ("Following", "following"), ("Posts", "posts_count")):
        pattern = r"([\d,MK.]+)\s*" + re.escape(label)
        match = re.search(pattern, parts, re.IGNORECASE)
        if match:
            data[key] = match.group(1).strip()


async def _populate_from_meta(page: Any, data: Dict[str, Any]) -> None:
    """Read followers/following/posts_count from the page's meta description."""
    try:
        meta_desc = await page.query_selector("meta[name='description']")
        if meta_desc is None:
            return
        desc = meta_desc.get_attribute("content") or ""
        _parse_meta_desc_counts(desc, data)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error extracting meta description: %s", type(exc).__name__)


async def _populate_header_name(header: Any, data: Dict[str, Any]) -> None:
    """Pull the profile name out of a header h1/h2."""
    for tag in ("h1", "h2"):
        name_el = header.query_selector(tag)
        if name_el is None:
            continue
        text = (await name_el.inner_text()).strip()
        if text:
            data["name"] = text
            return


_STAT_SELECTORS = (
    "header section ul li",
    "header div div ul li",
    "main section ul li",
    "[role='main'] header ul li",
)
_BIO_SELECTORS = (
    "header section div span",
    "header div div span",
    "[role='main'] header section div",
)
_VERIFIED_SELECTORS = (
    "header svg[aria-label='Verified']",
    "header svg[aria-label='Verified badge']",
    "header span[aria-label='Verified']",
    "header [title='Verified']",
)
_IMG_SELECTORS = (
    "header img",
    "header div img[alt*='profile']",
    "header div img[alt*='photo']",
)


def _apply_stat_text(text: str, data: Dict[str, Any]) -> None:
    """Classify one stat-item text into followers/following/posts_count."""
    lowered = text.lower()
    if "follower" in lowered:
        data["followers"] = _extract_count(text)
    elif "following" in lowered:
        data["following"] = _extract_count(text)
    elif "post" in lowered:
        data["posts_count"] = _extract_count(text)


async def _populate_stats_from_list(page: Any, data: Dict[str, Any]) -> None:
    """Walk stat-list selectors to populate followers/following/posts_count."""
    for selector in _STAT_SELECTORS:
        stat_items = await page.query_selector_all(selector)
        if not stat_items:
            continue
        for item in stat_items:
            _apply_stat_text(item.inner_text().strip(), data)
        return


async def _populate_bio(page: Any, data: Dict[str, Any]) -> None:
    """Find the first non-stats span inside the profile header."""
    for selector in _BIO_SELECTORS:
        bio_el = await page.query_selector(selector)
        if bio_el is None:
            continue
        bio_text = (await bio_el.inner_text()).strip()
        if bio_text and not any(x in bio_text.lower() for x in ("follower", "following", " post")):
            data["bio"] = bio_text
            return


async def _populate_verified(page: Any, data: Dict[str, Any]) -> None:
    """Set ``is_verified`` when any verified-badge selector matches."""
    for selector in _VERIFIED_SELECTORS:
        if await page.query_selector(selector):
            data["is_verified"] = True
            return


async def _populate_profile_pic(page: Any, data: Dict[str, Any]) -> None:
    """Set ``profile_pic`` from the first matching profile-image selector."""
    for selector in _IMG_SELECTORS:
        img_el = await page.query_selector(selector)
        if img_el is None:
            continue
        src = await img_el.get_attribute("src")
        if src:
            data["profile_pic"] = src
            return


async def _populate_from_header(page: Any, data: Dict[str, Any]) -> None:
    """Drive the four header-derived field probes with one shared except."""
    try:
        header = await page.query_selector("header")
        if header is not None:
            await _populate_header_name(header, data)
        await _populate_stats_from_list(page, data)
        await _populate_bio(page, data)
        await _populate_verified(page, data)
        await _populate_profile_pic(page, data)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error extracting header data: %s", type(exc).__name__)


async def _extract_profile_data(page: Any, username: str) -> Dict[str, Any]:
    """Extract profile data from an Instagram profile page."""
    data = _empty_profile_data()
    try:
        data["name"] = await _profile_name_from_title(page)
        await _populate_from_meta(page, data)
        await _populate_from_header(page, data)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Error during profile extraction: %s", type(exc).__name__)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    return data


_POST_GRID_SELECTORS = (
    "main article a",
    "article a[href*='/p/']",
    "[role='main'] article a",
    "div[style*='grid'] a[href*='/p/']",
)


def _post_likes_from_link(link: Any) -> str:
    """Extract likes count from a post-grid link's overlay (best-effort)."""
    try:
        parent = link.query_selector("..")
        if parent is None:
            return ""
        overlay_text = parent.inner_text()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Likes overlay probe suppressed: %s", type(exc).__name__)
        return ""
    match = re.search(r'([\d,MK.]+)\s*like', overlay_text, re.IGNORECASE)
    return match.group(1) if match else ""


def _post_from_link(link: Any) -> Optional[InstagramPost]:
    """Render one post-grid link into ``InstagramPost`` (None on parse error)."""
    try:
        href = link.get_attribute("href")
        if not href or "/p/" not in href:
            return None
        post_url = href if href.startswith("http") else f"https://www.instagram.com{href}"
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error extracting post link: %s", type(exc).__name__)
        return None
    return InstagramPost(url=post_url, caption="", likes=_post_likes_from_link(link))


async def _post_links(page: Any, max_posts: int) -> list:
    """First non-empty list of post-grid anchors under the candidate selectors."""
    for selector in _POST_GRID_SELECTORS:
        links = await page.query_selector_all(selector)
        if links:
            return links[:max_posts]
    return []


async def _extract_recent_posts(page: Any, max_posts: int = 6) -> List[InstagramPost]:
    """Extract recent posts from Instagram profile grid."""
    posts: list[InstagramPost] = []
    try:
        for link in await _post_links(page, max_posts):
            post = _post_from_link(link)
            if post is not None:
                posts.append(post)
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.debug("Error extracting recent posts: %s", type(exc).__name__)
    return posts


_HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADLESS_VIEWPORT = {"width": 1920, "height": 1080}


async def _open_instagram_context(p: Any, agent_port: Optional[int]) -> tuple[Any, Any, Any]:
    """Boot either a CDP stealth context or a fresh headless context."""
    if agent_port is not None:
        logger.info("Connecting to CDP browser on port %d", agent_port)
        return await make_stealth_context(p)
    logger.info("Launching headless browser for Instagram scrape")
    browser = await p.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent=_HEADLESS_USER_AGENT, viewport=_HEADLESS_VIEWPORT,
    )
    page = await ctx.new_page()
    return browser, ctx, page


def _empty_scrape_result(username: str) -> dict:
    """Skeleton result dict every Instagram scrape starts with."""
    return {
        "success": False, "username": username,
        "data": InstagramData(), "error": None, "elapsed_ms": 0,
    }


def _login_or_missing_error(current_url: str, username: str) -> Optional[str]:
    """Return a user-facing error string if the URL is a login/404 redirect."""
    if "/accounts/login/" in current_url or "/login/" in current_url:
        logger.warning("Instagram login wall detected")
        return "Login required -- pass agent_port with logged-in session"
    if "/404" in current_url or "not-found" in current_url.lower():
        return f"Profile not found: @{username}"
    return None


async def _populate_scrape_data(
    page: Any, username: str, is_cdp: bool, result: dict,
) -> None:
    """Fill ``result`` with profile + (CDP-only) posts data."""
    raw_data = await _extract_profile_data(page, username)
    data = InstagramData(**raw_data)
    if is_cdp:
        data.recent_posts = await _extract_recent_posts(page, max_posts=6)
    result["success"] = True
    result["data"] = data


async def _settle_profile_page(page: Any, profile_url: str, timeout: int) -> None:
    """Navigate + best-effort networkidle wait + dynamic-content sleep."""
    await page.goto(profile_url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 20000))
    except PWTimeout:
        pass
    await page.wait_for_timeout(2000)


async def _safe_close_one(obj: Any, name: str) -> None:
    """Close a single browser object, swallowing recoverable errors."""
    if obj is None:
        return
    try:
        await obj.close()
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        logger.warning("Error closing %s: %s", name, type(exc).__name__)
        return None  # explicit recovery: cleanup is best-effort; BROWSER_SCRAPE_EXCEPTIONS is non-fatal


async def close_headless_context(browser: "Browser | BrowserContext | Page | None", ctx: "BrowserContext | None", page: "Page") -> None:
    """Close (page, ctx, browser) for the headless code path."""
    await _safe_close_one(page, "page")
    await _safe_close_one(ctx, "context")
    await _safe_close_one(browser, "browser")


async def _drive_instagram_scrape(
    username: str, agent_port: Optional[int], timeout: int, result: dict,
) -> None:
    """Run the Playwright pipeline; mutate ``result`` in place."""
    from urllib.parse import quote
    is_cdp = agent_port is not None
    browser = ctx = page = None
    try:
        async with async_playwright() as p:
            browser, ctx, page = await _open_instagram_context(p, agent_port)
            profile_url = f"https://www.instagram.com/{quote(username)}/"
            logger.info("Navigating to Instagram profile: %s", profile_url)
            await _settle_profile_page(page, profile_url, timeout)
            error = _login_or_missing_error(page.url, username)
            if error is not None:
                result["error"] = error
                return
            await _populate_scrape_data(page, username, is_cdp, result)
            logger.info("Successfully scraped Instagram profile: @%s", username)
    except PWTimeout as exc:
        result["error"] = "Timeout loading Instagram profile: %s" % exc
        logger.error("Timeout for Instagram profile @%s: %s", username, exc)
        return None  # explicit recovery: non-fatal: PWTimeout is recorded but does not abort the caller
    except BROWSER_SCRAPE_EXCEPTIONS as exc:
        result["error"] = "Scrape failed: %s" % exc
        logger.error("Error scraping Instagram @%s: %s", username, type(exc).__name__)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller
    finally:
        if is_cdp:
            await close_stealth_context(browser, ctx, page)
        else:
            await close_headless_context(browser, ctx, page)


async def _do_scrape_instagram(
    username: str, agent_port: Optional[int], timeout: int,
) -> dict:
    """Async Playwright scrape of an Instagram profile."""
    start = timer_ms()
    result = _empty_scrape_result(username)
    await _drive_instagram_scrape(username, agent_port, timeout, result)
    result["elapsed_ms"] = int(timer_ms() - start)
    return result


@router.post(
    "/instagram",
    response_model=InstagramResponse,
    summary="Scrape Instagram profile",
)
async def scrape_instagram(request: InstagramRequest) -> InstagramResponse:
    """
    Scrape an Instagram profile for name, bio, follower counts, and recent posts.

    - `username`: Instagram handle (without @), e.g. "natgeo"
    - `agent_port`: Optional CDP port of a logged-in browser (e.g. 9222).
      If provided, connects via CDP instead of headless.
    - `timeout`: Timeout in milliseconds (default 30000)

    **Note:** Instagram often blocks headless browsers with a login wall.
    For best results, pass `agent_port` with a logged-in browser session.

    Start Chrome with ``--remote-debugging-port=9222`` to get a CDP port.
    """
    wall_timeout = request.timeout // 1000 + 10

    try:
        raw = await asyncio.wait_for(
            _do_scrape_instagram(request.username, request.agent_port, request.timeout),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        logger.error("Wall-clock timeout exceeded for Instagram scrape")
        return InstagramResponse(
            success=False,
            username=request.username,
            error="Global timeout exceeded (%ds wall-clock)" % wall_timeout,
            elapsed_ms=wall_timeout * 1000,
        )

    return InstagramResponse(**raw)
