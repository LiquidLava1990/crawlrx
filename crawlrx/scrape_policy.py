"""Scrape request planning policy.

This module decides how generic `/scrape` should start. It does not fetch
anything; it explains the strategy so execution and diagnostics can prove what
happened.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


ScrapeStrategy = Literal["http_first", "browser_first", "blocked"]


class ScrapeLike(Protocol):
    url: str
    wait_for: str | None
    scroll_to_bottom: bool
    click_selector: str | None
    agent: str | None
    js_inject: str | None
    wait_for_text: str | None
    wait_for_gone: str | None
    wait_for_timeout_extra: int
    screenshot_selector: str | None
    shadow_selector: str | None


@dataclass(frozen=True)
class ScrapePlan:
    """Execution plan selected before a scrape starts."""

    strategy: ScrapeStrategy
    reason: str
    required_features: tuple[str, ...] = ()
    quality_threshold: float = 0.2


def _required_browser_features(request: ScrapeLike) -> tuple[str, ...]:
    features: list[str] = []
    if request.wait_for:
        features.append("wait_for")
    if request.scroll_to_bottom:
        features.append("scroll")
    if request.click_selector:
        features.append("click")
    if request.agent:
        features.append("agent")
    if request.js_inject:
        features.append("js_inject")
    if request.wait_for_text:
        features.append("wait_for_text")
    if request.wait_for_gone:
        features.append("wait_for_gone")
    if request.wait_for_timeout_extra > 0:
        features.append("wait_for_timeout_extra")
    if request.screenshot_selector:
        features.append("screenshot_selector")
    if request.shadow_selector:
        features.append("shadow_selector")
    return tuple(features)


def plan_scrape(request: ScrapeLike) -> ScrapePlan:
    """Return the first strategy CrawlRX should try for this scrape request."""
    features = _required_browser_features(request)
    if features:
        return ScrapePlan(
            strategy="browser_first",
            reason="browser-only feature(s): %s" % ", ".join(features),
            required_features=features,
        )
    return ScrapePlan(
        strategy="http_first",
        reason="static extraction can be attempted before browser render",
        required_features=(),
    )
