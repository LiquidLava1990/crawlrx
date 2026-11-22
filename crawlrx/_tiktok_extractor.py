"""TikTok DOM/JSON extraction helpers (sibling module).

Holds the SIGI_STATE / __UNIVERSAL_DATA_FOR_REHYDRATION__ extraction JS,
the URL/regex helpers, and the narrow exception tuples used by
:mod:`crawlrx.tiktok`.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page, Playwright


from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger("crawlrx.tiktok.extractor")

# Narrow exception tuple covering the surface of a TikTok scrape session:
# Playwright failures, JSON / structure parse failures, timeouts.
PLAYWRIGHT_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    PlaywrightError,
    PlaywrightTimeoutError,
    OSError,
    TimeoutError,
    RuntimeError,
)

PARSE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
    UnicodeDecodeError,
)

SCRAPE_EXCEPTIONS: Tuple[type[BaseException], ...] = (
    PLAYWRIGHT_EXCEPTIONS + PARSE_EXCEPTIONS
)

# JS evaluator script -- two-strategy extraction (UDR 2025+ then SIGI fallback).
# Held as a module-level constant so the parent function stays small.
SIGI_EXTRACTION_JS: str = r"""() => {
    const result = { user: null, videos: [], sigi_found: false };

    try {
        // --- Strategy 1: __UNIVERSAL_DATA_FOR_REHYDRATION__ (TikTok 2025+) ---
        const udrEl = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (udrEl) {
            try {
                const udr = JSON.parse(udrEl.textContent);
                const scope = udr?.__DEFAULT_SCOPE__ || {};
                const userDetail = scope['webapp.user-detail'];
                if (userDetail) {
                    result.sigi_found = true;
                    const userInfo = userDetail.userInfo || {};
                    const u = userInfo.user || {};
                    const s = userInfo.stats || {};
                    result.user = {
                        uniqueId: u.uniqueId || '',
                        nickname: u.nickname || '',
                        signature: u.signature || '',
                        verified: u.verified || false,
                        privateAccount: u.privateAccount || false,
                        avatarThumb: u.avatarThumb || '',
                        followerCount: s.followerCount || 0,
                        followingCount: s.followingCount || 0,
                        heartCount: s.heartCount || s.heart || 0,
                        videoCount: s.videoCount || 0,
                        diggCount: s.diggCount || 0
                    };
                    return result;
                }
            } catch(e) { result.udr_error = e.message; }
        }

        // --- Strategy 2: SIGI_STATE (legacy, pre-2025) ---
        const scripts = document.querySelectorAll('script');
        for (const script of scripts) {
            const text = script.textContent || '';
            if (text.includes('SIGI_STATE')) {
                result.sigi_found = true;
                try {
                    const match = text.match(/SIGI_STATE\s*=\s*(\{.+?});/s);
                    if (match) {
                        const data = JSON.parse(match[1]);
                        const userModule = data?.UserModule?.users;
                        if (userModule) {
                            const userKey = Object.keys(userModule)[0];
                            const u = userModule[userKey];
                            const s = data?.UserModule?.stats?.[userKey] || {};
                            result.user = {
                                uniqueId: u.uniqueId || '',
                                nickname: u.nickname || '',
                                signature: u.signature || '',
                                verified: u.verified || false,
                                privateAccount: u.privateAccount || false,
                                avatarThumb: u.avatarThumb || '',
                                followerCount: s.followerCount || 0,
                                followingCount: s.followingCount || 0,
                                heartCount: s.heartCount || s.heart || 0,
                                videoCount: s.videoCount || 0,
                                diggCount: s.diggCount || 0
                            };
                        }
                        const itemModule = data?.ItemModule;
                        if (itemModule) {
                            result.videos = Object.values(itemModule).map(item => ({
                                video_id: item.id,
                                caption: item.desc || '',
                                views: item.stats?.playCount?.toString() || '',
                                likes: item.stats?.diggCount?.toString() || '',
                                comments: item.stats?.commentCount?.toString() || '',
                                shares: item.stats?.shareCount?.toString() || '',
                                post_date: item.createTime ? new Date(item.createTime * 1000).toISOString() : '',
                                cover_url: item.video?.cover || '',
                                duration: item.video?.duration?.toString() || '',
                                hashtags: item.textExtra?.filter(e => e.hashtagName).map(e => '#' + e.hashtagName) || []
                            }));
                        }
                        break;
                    }
                } catch(e) { result.parse_error = e.message; }
            }
        }
    } catch(e) { result.error = e.message; }

    return result;
}"""


TRENDING_EXTRACTION_JS: str = r"""(maxItems) => {
    const items = [];
    const hashtagElements = document.querySelectorAll(
        '[data-e2e="discover-card"], [class*="hashtag"], [class*="trend"]'
    );

    hashtagElements.forEach((el, idx) => {
        if (items.length >= maxItems) return;
        const link = el.querySelector('a') || el;
        const href = link.getAttribute('href') || '';
        const text = el.textContent?.trim() || '';

        if (href.includes('/tag/') || text.includes('#')) {
            const name = text.match(/#?([^\s]+)/)?.[1] || '';
            const views = text.match(/([\d.MK]+)/)?.[1] || '';
            items.push({
                type: 'hashtag',
                name: name,
                views_or_posts: views,
                url: href.startsWith('http') ? href : 'https://tiktok.com' + href
            });
        } else if (href.includes('/music/') || text.toLowerCase().includes('sound')) {
            items.push({
                type: 'sound',
                name: text.substring(0, 50),
                views_or_posts: '',
                url: href.startsWith('http') ? href : 'https://tiktok.com' + href
            });
        }
    });

    return items;
}"""


async def extract_sigi_state_data(page: "Page") -> Dict[str, Any]:
    """Run the SIGI extractor JS on a Playwright page; return the raw dict."""
    return await page.evaluate(SIGI_EXTRACTION_JS)


async def extract_trending_items(page: "Page", limit: int) -> List[Dict[str, Any]]:
    """Run the trending extractor JS for up to ``limit`` items."""
    return await page.evaluate(TRENDING_EXTRACTION_JS, limit)


def parse_count(text: Optional[str]) -> str:
    """Parse count text like '1.2M' or '3.4K' into normalized format."""
    if not text:
        return ""
    cleaned = text.strip().replace(",", "").replace(" ", "")
    match = re.search(r"([\d.]+)([MK]?)", cleaned, re.IGNORECASE)
    if not match:
        return cleaned
    num, suffix = match.groups()
    if suffix.upper() in {"M", "K"}:
        return cleaned
    return num


def extract_hashtags(text: Optional[str]) -> List[str]:
    """Extract hashtags (``#word``) from free text."""
    if not text:
        return []
    return re.findall(r"#\w+", text)


_VIDEO_ID_PATTERNS: Tuple[str, ...] = (
    r"/video/(\d+)",
    r"/v/(\d+)",
    r"tiktok\.com/[^/]+/video/(\d+)",
)


def extract_video_id_from_url(url: str) -> Optional[str]:
    """Extract a TikTok video ID from any of the supported URL shapes."""
    for pattern in _VIDEO_ID_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None
