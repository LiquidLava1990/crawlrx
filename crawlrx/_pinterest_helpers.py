"""Pinterest scraping helpers (split from pinterest.py)."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS, safe_extract_async

logger = logging.getLogger("crawlrx.pinterest.helpers")

PINTEREST_BASE_URL = "https://www.pinterest.com"

PIN_CARD_SELECTORS: Tuple[str, ...] = (
    "[data-test-id='pin']",
    ".Pin",
    "[data-test-id='pinWrapper']",
    ".GrowthPin",
)


async def random_delay(min_ms: int = 500, max_ms: int = 1500) -> None:
    """Sleep for a random duration between ``min_ms`` and ``max_ms``."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


def parse_count(count_text: Optional[str]) -> Optional[str]:
    """Normalise ``"1.2k"`` / ``"5m"`` count text down to its numeric token."""
    if not count_text:
        return None
    text = count_text.strip().lower()
    match = re.search(r"([\d.]+)([km]?)", text)
    return match.group(0) if match else text


async def first_query(page: "Page", *selectors: str) -> Optional[Any]:
    """Return the first matching element on ``page`` across ``selectors``."""
    for selector in selectors:
        elem = await page.query_selector(selector)
        if elem is not None:
            return elem
    return None


async def find_pin_cards(page: "Page") -> List[Any]:
    """Try the known Pinterest pin-card selectors in order, return first hit."""
    for selector in PIN_CARD_SELECTORS:
        cards = await page.query_selector_all(selector)
        if cards:
            return cards
    return []


async def scroll_grid(page: "Page", repeats: int = 5) -> None:
    """Scroll a Pinterest pin grid to surface lazy-loaded items."""
    for _ in range(max(repeats, 0)):
        await page.evaluate("window.scrollBy(0, 1000)")
        await random_delay(500, 1000)


async def _resolve_pin_url(card: Any) -> Optional[str]:
    """Return the absolute pin URL from a card's first ``<a href>``."""
    link = await card.query_selector("a[href]")
    if link is None:
        return None
    href = await link.get_attribute("href")
    if not href:
        return None
    return href if href.startswith("http") else f"{PINTEREST_BASE_URL}{href}"


async def _resolve_image_url(card: Any) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(img_element, image_url)`` for a card."""
    img = await card.query_selector(
        "img, [data-test-id='pin-image'] img"
    )
    if img is None:
        return None, None
    image_url = await img.get_attribute("src") or await img.get_attribute("data-src")
    return img, image_url


async def _resolve_pin_title(card: Any, img: Optional[Any]) -> Optional[str]:
    """Return the most descriptive title text for a pin card."""
    if img is not None:
        alt = await img.get_attribute("alt")
        if alt:
            return alt
    title_el = await card.query_selector("[title]")
    if title_el is not None:
        return await title_el.get_attribute("title")
    return None


async def _resolve_pinner_name(card: Any) -> Optional[str]:
    """Return the pinner's display name from a card."""
    pinner = await card.query_selector(
        "[data-test-id='pinner-name'], [class*='pinner'], a[href^='/'] img"
    )
    if pinner is None:
        return None
    alt = await pinner.get_attribute("alt")
    if alt:
        return alt
    text = (await pinner.inner_text()).strip()
    return text or None


async def _resolve_save_count(card: Any) -> Optional[str]:
    """Return the parsed save-count token for a card."""
    save_el = await card.query_selector(
        "[data-test-id='save-button'], [class*='save'], [class*='repinCount']"
    )
    if save_el is None:
        return None
    text = (await save_el.inner_text()).strip()
    return parse_count(text)


async def _resolve_external_link(card: Any) -> Optional[str]:
    """Return the external link URL on a card, or ``None``."""
    link = await card.query_selector("a[href^='http']")
    if link is None:
        return None
    return await link.get_attribute("href")


async def _resolve_text(card: Any, *selectors: str) -> Optional[str]:
    """First-matching descendant's stripped inner text on ``card``."""
    for selector in selectors:
        elem = await card.query_selector(selector)
        if elem is None:
            continue
        text = (await elem.inner_text()).strip()
        if text:
            return text
    return None


async def _build_pin(card: Any, pin_factory: Any) -> Tuple[Optional[str], Any]:
    """Return ``(pin_url, PinterestPin)`` parsed from one card."""
    pin_url = await _resolve_pin_url(card)
    img, image_url = await _resolve_image_url(card)
    title = await _resolve_pin_title(card, img)
    description = await _resolve_text(
        card, "[data-test-id='description']", "[class*='description']"
    )
    pinner_name = await _resolve_pinner_name(card)
    board_name = await _resolve_text(card, "[class*='board']")
    save_count = await _resolve_save_count(card)
    link_url = await _resolve_external_link(card)

    pin = pin_factory(
        title=title,
        description=description,
        image_url=image_url,
        pin_url=pin_url,
        pinner_name=pinner_name,
        board_name=board_name,
        save_count=save_count,
        link_url=link_url,
    )
    return pin_url, pin


async def _collect_with_builder(
    page: Any,
    limit: int,
    builder: Any,
    field: str,
    label: str,
) -> List[Any]:
    """Generic ``cards -> dedup-by-url -> capped list`` collector."""
    cards = await find_pin_cards(page)
    logger.info("Found %d Pinterest %s cards", len(cards), label)
    pins: List[Any] = []
    seen_urls: set = set()
    for card in cards[: limit * 2]:
        result = await safe_extract_async(
            lambda card=card: builder(card),
            field=field,
            log=logger,
        )
        if result is None:
            continue
        pin_url, pin = result
        if pin_url and pin_url in seen_urls:
            continue
        if pin_url:
            seen_urls.add(pin_url)
        pins.append(pin)
        if len(pins) >= limit:
            break
    return pins


async def collect_pins(
    page: "Page",
    limit: int,
    pin_factory: "Callable[..., Any]",
) -> List[Any]:
    """Collect up to ``limit`` ``PinterestPin`` rows from rendered cards."""
    return await _collect_with_builder(
        page,
        limit,
        lambda card: _build_pin(card, pin_factory),
        field="pinterest_card",
        label="pin",
    )


async def _build_board_pin(card: Any, pin_factory: Any, board_name: Optional[str]) -> Tuple[Optional[str], Any]:
    """Return ``(pin_url, PinterestPin)`` parsed from one board card."""
    pin_url = await _resolve_pin_url(card)
    img, image_url = await _resolve_image_url(card)
    title = await img.get_attribute("alt") if img is not None else None
    description = await _resolve_text(card, "[class*='description']")
    save_count = await _resolve_save_count(card)
    pin = pin_factory(
        title=title,
        description=description,
        image_url=image_url,
        pin_url=pin_url,
        board_name=board_name,
        save_count=save_count,
    )
    return pin_url, pin


async def collect_board_pins(
    page: "Page",
    limit: int,
    pin_factory: "Callable[..., Any]",
    board_name: Optional[str],
) -> List[Any]:
    """Collect pins on a Pinterest board page."""
    return await _collect_with_builder(
        page,
        limit,
        lambda card: _build_board_pin(card, pin_factory, board_name),
        field="pinterest_board_card",
        label="board",
    )


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

async def _resolve_profile_text(page: Any, *selectors: str) -> Optional[str]:
    """Return the first matching profile-page element's inner text."""
    elem = await first_query(page, *selectors)
    if elem is None:
        return None
    text = (await elem.inner_text()).strip()
    return text or None


async def populate_profile_basics(page: "Page", profile: object) -> None:
    """Populate display name + bio."""
    profile.display_name = (
        await _resolve_profile_text(
            page,
            "h1",
            "[data-test-id='profile-name']",
            "[data-test-id='fullname']",
        )
        or profile.username
    )
    profile.bio = await _resolve_profile_text(
        page,
        "[data-test-id='profile-bio']",
        "[data-test-id='about']",
        "[class*='bio']",
    )


def _parse_first_int(text: Optional[str]) -> Optional[int]:
    """Return the first integer token in ``text``, or ``None``."""
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def populate_profile_counts(page: "Page", profile: object) -> None:
    """Populate followers / following / monthly_views / pin_count."""
    text = await _resolve_profile_text(
        page, "[data-test-id='follower-count']", "text=/follower/i"
    )
    if text:
        profile.followers = parse_count(text)
    text = await _resolve_profile_text(
        page, "[data-test-id='following-count']", "text=/following/i"
    )
    if text:
        profile.following = parse_count(text)
    text = await _resolve_profile_text(page, "text=/monthly view/i")
    if text:
        profile.monthly_views = parse_count(text)
    pin_count = _parse_first_int(await _resolve_profile_text(page, "text=/pin/i"))
    if pin_count is not None:
        profile.pin_count = pin_count


async def _board_pin_count(card: Any) -> Optional[int]:
    """Return the pin-count integer rendered on a board card."""
    elem = await card.query_selector("[class*='pinCount']")
    if elem is None:
        return None
    return _parse_first_int((await elem.inner_text()).strip())


async def _build_board(card: Any, board_factory: Any) -> Optional[Any]:
    """Build one ``PinterestBoard`` row from a board card."""
    name_el = await card.query_selector(
        "[data-test-id='board-name'], h3, [class*='title']"
    )
    if name_el is None:
        return None
    name = (await name_el.inner_text()).strip()
    if not name:
        return None
    return board_factory(name=name, pin_count=await _board_pin_count(card))


async def collect_boards(page: "Page", board_factory: "Callable[..., Any]") -> List[Any]:
    """Collect up to 20 boards visible on a profile page."""
    cards = await page.query_selector_all(
        "[data-test-id='board-card'], .Board"
    )
    boards: List[Any] = []
    for card in cards[:20]:
        try:
            board = await _build_board(card, board_factory)
        except BROWSER_SCRAPE_EXCEPTIONS as exc:
            logger.debug("Pinterest board parse error: %s", exc)
            continue
        if board is not None:
            boards.append(board)
    return boards
