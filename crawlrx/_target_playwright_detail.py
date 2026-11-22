"""Product-detail field population for the Target Playwright path."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    parse_rating_value as _hp_parse_rating,
    parse_review_count as _hp_parse_review_count,
)
from ._target_models import TargetFulfillmentOptions, TargetProductDetail

logger = logging.getLogger("crawlrx.target.playwright.detail")


async def _safe_query_inner_text(
    page: Any, selector: str, *, field: str
) -> Optional[str]:
    """Resolve ``page.query_selector(selector).inner_text()`` safely."""
    try:
        el = await page.query_selector(selector)
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("%s probe suppressed: %s: %s", field, type(exc).__name__, exc)
        return None
    if el is None:
        return None
    try:
        text = await el.inner_text()
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("%s text suppressed: %s: %s", field, type(exc).__name__, exc)
        return None
    return text.strip() if text else None


async def _set_optional_text(
    page: Any,
    selector: str,
    setter: Callable[[str], None],
    *,
    field: str,
) -> None:
    """Probe a selector and call ``setter`` with the stripped inner text."""
    text = await _safe_query_inner_text(page, selector, field=field)
    if text:
        setter(text)


@dataclass(frozen=True)
class _ValueProbe:
    """Bundled args for :func:`_set_optional_value` to keep the param count low."""

    selector: str
    setter: Callable[[Any], None]
    field: str
    transform: Callable[[str], Optional[Any]]


async def _set_optional_value(page: Any, probe: _ValueProbe) -> None:
    """Probe a selector and call ``probe.setter`` with a transformed value."""
    text = await _safe_query_inner_text(page, probe.selector, field=probe.field)
    if text is None:
        return
    value = probe.transform(text)
    if value is not None:
        probe.setter(value)


async def populate_basics(page: "Page", product: TargetProductDetail) -> None:
    """Populate title / price / description / brand on the detail object."""
    await _set_optional_text(
        page, "[data-test='product-title']",
        lambda v: setattr(product, "title", v), field="title",
    )
    await _set_optional_text(
        page, "[data-test='product-price']",
        lambda v: setattr(product, "price", v), field="price",
    )
    await _set_optional_text(
        page, "[data-test='product-regular-price']",
        lambda v: setattr(product, "original_price", v), field="original_price",
    )
    await _set_optional_text(
        page, "[data-test='product-description']",
        lambda v: setattr(product, "description", v), field="description",
    )
    await _set_optional_text(
        page, "[data-test='brand-name']",
        lambda v: setattr(product, "brand", v), field="brand",
    )


async def populate_metrics(page: "Page", product: TargetProductDetail) -> None:
    """Populate rating + review_count on the detail object."""
    await _set_optional_value(
        page,
        _ValueProbe(
            selector="[data-test='rating-value']",
            setter=lambda v: setattr(product, "rating", v),
            field="rating",
            transform=_hp_parse_rating,
        ),
    )
    await _set_optional_value(
        page,
        _ValueProbe(
            selector="[data-test='review-count']",
            setter=lambda v: setattr(product, "review_count", v),
            field="review_count",
            transform=_hp_parse_review_count,
        ),
    )


async def populate_availability(page: "Page", product: TargetProductDetail) -> None:
    """Populate the availability label, defaulting to In stock on miss."""
    text = await _safe_query_inner_text(
        page, "[data-test='availability-status']", field="availability"
    )
    product.availability = text or "In stock"


_FULFILLMENT_PROBES: tuple[tuple[str, str], ...] = (
    ("shipping", "text=/shipping/i"),
    ("pickup", "text=/pickup/i"),
    ("delivery", "text=/delivery/i"),
    ("same_day_delivery", "text=/same day/i"),
)


async def _resolve_fulfillment_flag(page: Any, selector: str, attr: str) -> bool:
    """Return True if a fulfillment selector matches anything."""
    try:
        el = await page.query_selector(selector)
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("fulfillment %s suppressed: %s: %s", attr, type(exc).__name__, exc)
        return False
    return el is not None


async def populate_fulfillment(page: "Page", product: TargetProductDetail) -> None:
    """Populate fulfillment options from text-match selectors."""
    fulfillment = TargetFulfillmentOptions()
    for attr, selector in _FULFILLMENT_PROBES:
        if await _resolve_fulfillment_flag(page, selector, attr):
            setattr(fulfillment, attr, True)
    product.fulfillment_options = fulfillment


async def _safe_get_attribute(element: Any, name: str) -> Optional[str]:
    """Return ``element.get_attribute(name)`` or ``None`` on probe failure."""
    try:
        return await element.get_attribute(name)
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("attr %s suppressed: %s: %s", name, type(exc).__name__, exc)
        return None


async def populate_images(page: "Page", product: TargetProductDetail) -> None:
    """Collect image src URLs into ``product.images``."""
    try:
        img_els = await page.query_selector_all("[data-test='product-image'] img")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("images suppressed: %s: %s", type(exc).__name__, exc)
        return
    images: List[str] = []
    for img in img_els:
        src = await _safe_get_attribute(img, "src")
        if src:
            images.append(src)
    product.images = images


async def _safe_inner_text(element: Any) -> str:
    """Return ``element.inner_text().strip()`` or ``""`` on probe failure."""
    try:
        text = await element.inner_text()
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("inner_text suppressed: %s: %s", type(exc).__name__, exc)
        return ""
    return text.strip() if text else ""


async def _apply_spec_row(row: Any, product: TargetProductDetail) -> None:
    """Apply a single spec row to ``product.specs``."""
    try:
        cells = await row.query_selector_all("td, th")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("spec row suppressed: %s: %s", type(exc).__name__, exc)
        return
    if len(cells) < 2:
        return
    key = await _safe_inner_text(cells[0])
    value = await _safe_inner_text(cells[1])
    if key and value:
        product.specs[key] = value


async def populate_specs(page: "Page", product: TargetProductDetail) -> None:
    """Populate the spec table from the detail page."""
    try:
        rows = await page.query_selector_all(
            "[data-test='item-details-specification'] tr"
        )
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("specs suppressed: %s: %s", type(exc).__name__, exc)
        return
    for row in rows:
        await _apply_spec_row(row, product)


async def populate_category(page: "Page", product: TargetProductDetail) -> None:
    """Stitch breadcrumbs into ``product.category``."""
    try:
        breadcrumbs = await page.query_selector_all("[data-test='breadcrumb-item']")
    except ELEMENT_PROBE_EXCEPTIONS as exc:
        logger.debug("breadcrumbs suppressed: %s: %s", type(exc).__name__, exc)
        return
    parts = [await _safe_inner_text(bc) for bc in breadcrumbs]
    parts = [part for part in parts if part]
    if parts:
        product.category = " > ".join(parts)


__all__ = (
    "populate_availability",
    "populate_basics",
    "populate_category",
    "populate_fulfillment",
    "populate_images",
    "populate_metrics",
    "populate_specs",
)
