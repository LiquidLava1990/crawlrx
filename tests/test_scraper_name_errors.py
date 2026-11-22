"""Regression: helpers referenced names that were never imported (NameError at runtime)."""
from __future__ import annotations

import asyncio

from crawlrx import apartments, rakuten_scraper


def test_apartments_floor_plan_and_review_builders_run() -> None:
    plan = apartments._build_floor_plan({"name": "Studio", "beds": "1", "baths": "1", "sqft": "500", "price": "$1,200"})
    review = apartments._build_review({"author": "A", "rating": "4", "text": "Nice", "date": "2026-01-01"})
    assert plan is not None and plan.name == "Studio"
    assert review is not None and review.author == "A"


def test_rakuten_cookie_banner_failure_is_swallowed() -> None:
    from playwright.async_api import Error as PlaywrightError

    class BrokenPage:
        def locator(self, _selector: str) -> object:
            raise PlaywrightError("no banner")

    asyncio.run(rakuten_scraper._try_dismiss_cookie_banner(BrokenPage()))


def test_rakuten_cookie_banner_is_clicked() -> None:
    clicks: list[str] = []

    class Button:
        async def is_visible(self) -> bool:
            return True

        async def click(self) -> None:
            clicks.append("accept")

    class Locator:
        first = Button()

    class Page:
        def locator(self, _selector: str) -> Locator:
            return Locator()

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

    asyncio.run(rakuten_scraper._try_dismiss_cookie_banner(Page()))
    assert clicks == ["accept"]
