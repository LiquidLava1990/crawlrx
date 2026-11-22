"""Helpers + JS strings for yelp.py (split for the function-length limit)."""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger("crawlrx._yelp_helpers")


# ─────────────────────────────────────────────
# JS extraction strings (verbatim from prior implementation)
# ─────────────────────────────────────────────

SEARCH_EXTRACT_JS = r"""
(maxResults) => {
    const cards = Array.from(document.querySelectorAll(
        '[data-testid="serp-ia-card"]'
    )).slice(0, maxResults);
    return cards.map(card => {
        const biz = {
            name: '', rating: null, review_count: 0, price_range: '',
            categories: [], address: '', phone: '', url: '',
            is_closed: false, hours_summary: ''
        };
        const bizLink = card.querySelector('a[href*="/biz/"]');
        if (bizLink) {
            biz.url = 'https://www.yelp.com' + bizLink.getAttribute('href').split('?')[0];
        }
        const text = card.innerText || '';
        const firstLine = text.split('\n')[0] || '';
        biz.name = firstLine.replace(/^\d+\.\s*/, '').trim();
        for (const el of card.querySelectorAll('[aria-label]')) {
            const label = el.getAttribute('aria-label') || '';
            const m = label.match(/(\d+\.?\d*)\s*star/i);
            if (m) { biz.rating = parseFloat(m[1]); break; }
        }
        const revM = text.match(/\(([\d,\.]+k?)\s*reviews?\)/i);
        if (revM) {
            const raw = revM[1].replace(',', '');
            biz.review_count = raw.endsWith('k')
                ? Math.round(parseFloat(raw) * 1000) : parseInt(raw);
        }
        const priceM = text.match(/([$]{1,4})(?=\s|\n|Closed|Open)/);
        if (priceM) biz.price_range = priceM[1];
        biz.is_closed = text.includes('Closed') || text.includes('Temporarily closed');
        return biz.name ? biz : null;
    }).filter(Boolean);
}
"""


BUSINESS_EXTRACT_JS = r"""
() => {
    const d = {
        name: '', rating: null, review_count: 0, price_range: '',
        categories: [], address: '', phone: '', url: window.location.href,
        is_closed: false, hours_summary: '', hours: {}, about: '',
        top_reviews: [], photos_count: 0, website: ''
    };
    for (const sel of ['h1', '[data-testid="business-title"]']) {
        const el = document.querySelector(sel);
        if (el) { d.name = el.textContent.trim(); break; }
    }
    const ratingEl = document.querySelector('[aria-label*="star rating"], .i-stars');
    if (ratingEl) {
        const m = (ratingEl.getAttribute('aria-label') || '').match(/([\d.]+)/);
        if (m) d.rating = parseFloat(m[1]);
    }
    const reviewEl = document.querySelector('a[href*="/reviews"]');
    if (reviewEl) {
        const m = (reviewEl.textContent || '').match(/([\d,]+)/);
        if (m) d.review_count = parseInt(m[1].replace(',', ''));
    }
    for (const el of document.querySelectorAll('[aria-label]')) {
        const label = el.getAttribute('aria-label') || '';
        const m = label.match(/([$]+)\s*price/i) || label.match(/price.*?([$]+)/i);
        if (m) { d.price_range = m[1]; break; }
    }
    if (!d.price_range) {
        const bodyText = document.body.innerText;
        const pm = bodyText.match(/([$]{1,4})(?=\s*·|\s*\n)/);
        if (pm) d.price_range = pm[1];
    }
    document.querySelectorAll('a[href*="/c/"]').forEach(el => {
        const text = el.textContent.trim();
        if (text && text.length < 50 && !d.categories.includes(text)) d.categories.push(text);
    });
    for (const sel of ['address', '[data-testid="address-text"]']) {
        const el = document.querySelector(sel);
        if (el) { d.address = el.textContent.trim(); break; }
    }
    const phoneEl = document.querySelector('[data-testid="phone-text"], p[class*="phone"]');
    if (phoneEl) d.phone = phoneEl.textContent.trim();
    if (document.body.textContent.includes('Permanently closed') ||
        document.body.textContent.includes('Temporarily closed')) {
        d.is_closed = true;
    }
    const hoursEl = document.querySelector('[class*="hours"], table[class*="hours"]');
    if (hoursEl) {
        d.hours_summary = hoursEl.textContent.trim().substring(0, 200);
        hoursEl.querySelectorAll('tr').forEach(row => {
            const dayEl = row.querySelector('th, td:first-child');
            const timeEl = row.querySelector('td:last-child');
            if (dayEl && timeEl) d.hours[dayEl.textContent.trim()] = timeEl.textContent.trim();
        });
    }
    const aboutEl = document.querySelector('[class*="about"], [class*="specialties"], [class*="description"]');
    if (aboutEl) d.about = aboutEl.textContent.trim();
    const photosEl = document.querySelector('a[href*="/photos"]');
    if (photosEl) {
        const m = (photosEl.textContent || '').match(/([\d,]+)/);
        if (m) d.photos_count = parseInt(m[1].replace(',', ''));
    }
    const websiteEl = document.querySelector('a[href*="/biz_redir?"], [class*="website"] a');
    if (websiteEl) d.website = websiteEl.textContent.trim() || websiteEl.getAttribute('href') || '';
    return d;
}
"""


REVIEWS_EXTRACT_JS = r"""
() => {
    const reviews = [];
    const reviewEls = document.querySelectorAll(
        '[data-testid="review"], [class*="reviewCard"], [class*="review__"]'
    );
    let count = 0;
    reviewEls.forEach(el => {
        if (count >= 10) return;
        const r = { author: '', rating: 0, text: '', date: '' };
        const authorEl = el.querySelector('[class*="user"], a[href*="/user_details"]');
        if (authorEl) r.author = authorEl.textContent.trim();
        const ratingEl = el.querySelector('[aria-label*="star"]');
        if (ratingEl) {
            const m = (ratingEl.getAttribute('aria-label') || '').match(/([\d.]+)/);
            if (m) r.rating = parseFloat(m[1]);
        }
        const textEl = el.querySelector('p[class*="comment"], [class*="review-content"], span[class*="raw__"]');
        if (textEl) r.text = textEl.textContent.trim();
        const dateEl = el.querySelector('[class*="date"], time');
        if (dateEl) r.date = dateEl.textContent.trim();
        if (r.author || r.text) { reviews.push(r); count++; }
    });
    return reviews;
}
"""


# ─────────────────────────────────────────────
# HTTP fallback line-parsing helpers
# ─────────────────────────────────────────────


def _safe_int(text: str) -> Optional[int]:
    """Best-effort int parse; returns ``None`` on failure."""
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def _safe_float(text: str) -> Optional[float]:
    """Best-effort float parse; returns ``None`` on failure."""
    try:
        return float(text)
    except ValueError:
        return None


def _maybe_set_rating(line: str) -> Optional[float]:
    """Pull a star rating from one HTML line."""
    if "star rating" not in line.lower():
        return None
    match = re.search(r"([\d.]+)\s*star", line, re.IGNORECASE)
    return _safe_float(match.group(1)) if match else None


def _maybe_set_review_count(line: str) -> Optional[int]:
    """Pull a review count from one HTML line."""
    if "review" not in line.lower():
        return None
    match = re.search(r"([\d,]+)\s*reviews?", line, re.IGNORECASE)
    return _safe_int(match.group(1)) if match else None


def _maybe_price_range(line: str) -> Optional[str]:
    """Return a ``$`` price-range run if present and ≤4 chars."""
    if "$" not in line:
        return None
    match = re.search(r"([$]+)", line)
    if match and len(match.group(1)) <= 4:
        return match.group(1)
    return None


def _new_business(link_match: re.Match) -> dict:
    """Build a fresh business dict from a ``href="/biz/...">name`` match."""
    biz_url = "https://www.yelp.com%s" % link_match.group(1).split("?")[0]
    return {
        "name": link_match.group(2).strip(),
        "url": biz_url,
        "rating": None,
        "review_count": 0,
        "price_range": "",
    }


def _enrich_business(current: dict, line: str) -> None:
    """In-place update: fold rating / review-count / price-range out of one line."""
    rating = _maybe_set_rating(line)
    if rating is not None:
        current["rating"] = rating
    review_count = _maybe_set_review_count(line)
    if review_count is not None:
        current["review_count"] = review_count
    price_range = _maybe_price_range(line)
    if price_range is not None:
        current["price_range"] = price_range


def _try_match_link(line: str) -> Optional[re.Match]:
    """Return the ``/biz/`` href + name match for ``line`` (or ``None``)."""
    if "/biz/" not in line or "href" not in line:
        return None
    return re.search(r'href="(/biz/[^"]+)"[^>]*>([^<]+)', line)


def parse_mobile_search_html(html: str, limit: int) -> List[dict]:
    """Parse mobile-Yelp search HTML (line-oriented) into business dicts."""
    businesses: List[dict] = []
    current: Optional[dict] = None
    for line in html.split("\n"):
        link_match = _try_match_link(line)
        if link_match is None and current is not None:
            _enrich_business(current, line)
            continue
        if link_match is None:
            continue
        if current is not None:
            businesses.append(current)
        if len(businesses) >= limit:
            current = None
            break
        current = _new_business(link_match)
    if current is not None and len(businesses) < limit:
        businesses.append(current)
    return businesses[:limit]
