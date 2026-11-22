"""Promo-code extractors per source (RetailMeNot, Coupons.com).

Per-card parse failures land in :func:`_travel_helpers.safe_card_parse`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import _travel_helpers as _th


# Compile-once: every promo card extractor calls these patterns; compiling
# them inside the helpers re-paid the cost on every coupon site fetch.
_CODE_CLASS_RE = re.compile(r"code|coupon", re.I)
_DESC_CLASS_RE = re.compile(r"description|title|discount", re.I)
_DISCOUNT_RE = re.compile(
    r"(\d+%|\$\d+|\d+\s*%%|\d+\s*percent)",
    re.IGNORECASE,
)
_EXPIRY_LABEL_RE = re.compile(r"expires?|ends?|valid until", re.I)
_EXPIRY_DATE_RE = re.compile(
    r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}-\d{2}-\d{2})",
)
_VERIFIED_RE = re.compile(r"verified|confirmed", re.I)
_RETAILMENOT_CARD_RE = re.compile(r"coupon|offer|code|deal", re.I)
_COUPONS_COM_CARD_RE = re.compile(r"coupon|offer|code", re.I)


@dataclass
class _PromoCtx:
    """Per-source context for promo-card collection."""

    source: str
    cap: int
    seen: Set[str] = field(default_factory=set)
    card_cap: int = 20
    extract_expiry: bool = True


def _extract_code(card) -> Optional[str]:
    code_el = card.find(
        ["span", "div", "code"],
        class_=_CODE_CLASS_RE,
    )
    code = code_el.get_text(strip=True) if code_el else None
    if not code:
        code = card.get("data-clipboard-text") or card.get("data-code")
    return code.strip().upper() if code else None


def _extract_description(card) -> str:
    desc_el = card.find(
        ["div", "p", "span"],
        class_=_DESC_CLASS_RE,
    )
    return desc_el.get_text(strip=True) if desc_el else ""


def _extract_discount(description: str) -> Optional[str]:
    match = _DISCOUNT_RE.search(description)
    return match.group(1) if match else None


def _extract_expiry(card) -> Optional[str]:
    exp_el = card.find(string=_EXPIRY_LABEL_RE)
    if not exp_el:
        return None
    exp_match = _EXPIRY_DATE_RE.search(exp_el)
    return exp_match.group(1) if exp_match else None


def _is_verified(card) -> bool:
    return bool(card.find(string=_VERIFIED_RE))


def _build_promo_record(card, ctx: _PromoCtx) -> Optional[Dict]:
    code = _extract_code(card)
    if not code or len(code) <= 2:
        return None
    description = _extract_description(card)
    discount = _extract_discount(description)
    expires = _extract_expiry(card) if ctx.extract_expiry else None
    verified = _is_verified(card)
    return {
        "code": code,
        "discount": discount,
        "description": description,
        "expires": expires,
        "source": ctx.source,
        "verified": verified,
    }


def _collect_promos(cards: list, ctx: _PromoCtx) -> List[Dict]:
    out: List[Dict] = []
    for card in cards[:ctx.card_cap]:
        record = _th.safe_card_parse(
            lambda c=card: _build_promo_record(c, ctx),
            where=f"promo card ({ctx.source})",
        )
        if record is None or record["code"] in ctx.seen:
            continue
        ctx.seen.add(record["code"])
        out.append(record)
        if len(out) >= ctx.cap:
            break
    return out


def _fetch_promo_cards(url: str, class_pattern: "re.Pattern[str]") -> Optional[list]:
    """GET ``url`` and return promo-card soup elements (None on failure)."""
    resp = _th.requests_get(url, timeout=10)
    if not resp:
        return None
    soup = _th.soup_of(resp)
    return soup.find_all(
        ["div", "li"],
        class_=class_pattern,
    )


def collect_retailmenot(*, merchant: str, cap: int) -> List[Dict]:
    cards = _fetch_promo_cards(
        f"https://www.retailmenot.com/view/{merchant}.com",
        _RETAILMENOT_CARD_RE,
    )
    if cards is None:
        return []
    return _collect_promos(cards, _PromoCtx(source="retailmenot", cap=cap))


def collect_coupons_com(
    *, merchant: str, cap: int, seen_codes: Set[str],
) -> List[Dict]:
    cards = _fetch_promo_cards(
        f"https://www.coupons.com/coupon-codes/{merchant}",
        _COUPONS_COM_CARD_RE,
    )
    if cards is None:
        return []
    ctx = _PromoCtx(
        source="coupons.com",
        cap=cap,
        seen=seen_codes,
        card_cap=15,
        extract_expiry=False,
    )
    return _collect_promos(cards, ctx)
