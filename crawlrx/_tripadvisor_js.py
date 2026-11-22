"""Inline JavaScript blobs used by ``tripadvisor.py``.

Split out so the orchestrator file stays under the 500-line ceiling.
"""
from __future__ import annotations


SEARCH_EXTRACT_JS = """
(maxResults) => {
    const results = [];
    const containers = document.querySelectorAll(
        '[data-testid="card"], [data-automation="search-result-card"], ' +
        '.result-card, [class*="result"], .listing'
    );

    containers.forEach((card, idx) => {
        if (results.length >= maxResults) return;

        const item = {
            name: '',
            category: '',
            rating: null,
            review_count: 0,
            price_range: '',
            address: '',
            listing_url: '',
            image_url: null,
            ranking: null,
            awards: []
        };

        const nameSelectors = [
            'a[target="_blank"] h3', 'h3 a', 'a h3', '.title a',
            'a[class*="title"]', '[data-testid="title"]'
        ];
        for (let sel of nameSelectors) {
            const el = card.querySelector(sel);
            if (el) {
                item.name = el.textContent.trim();
                const href = el.getAttribute('href');
                if (href) {
                    item.listing_url = href.startsWith('http') ? href : 'https://www.tripadvisor.com' + href;
                }
                break;
            }
        }

        if (!item.name) {
            const anyLink = card.querySelector('a');
            if (anyLink) {
                item.name = anyLink.textContent.trim();
                const href = anyLink.getAttribute('href');
                if (href) {
                    item.listing_url = href.startsWith('http') ? href : 'https://www.tripadvisor.com' + href;
                }
            }
        }

        const ratingEl = card.querySelector('[aria-label*="bubble"], [title*="bubble"], .ui_bubble_rating');
        if (ratingEl) {
            const label = ratingEl.getAttribute('aria-label') || ratingEl.getAttribute('title') || '';
            const match = label.match(/(\\d\\.?\\d*)/);
            if (match) item.rating = parseFloat(match[1]);
        }

        const reviewEl = card.querySelector('[class*="review"], span:contains("review")');
        if (reviewEl) {
            const text = reviewEl.textContent || '';
            const match = text.match(/([\\d,]+)/);
            if (match) item.review_count = parseInt(match[1].replace(',', ''));
        }

        const priceEl = card.querySelector('[class*="price"], span:contains("$")');
        if (priceEl) {
            const text = priceEl.textContent || '';
            const match = text.match(/([$]+)/);
            if (match) item.price_range = match[1];
        }

        const addrEl = card.querySelector('[class*="address"], address, [data-testid="address"]');
        if (addrEl) item.address = addrEl.textContent.trim();

        const imgEl = card.querySelector('img');
        if (imgEl) {
            item.image_url = imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || imgEl.getAttribute('data-lazy-src');
        }

        const rankEl = card.querySelector('[class*="rank"], .ranking');
        if (rankEl) item.ranking = rankEl.textContent.trim();

        const awardEls = card.querySelectorAll('[class*="award"], [class*="badge"]');
        awardEls.forEach(el => {
            const text = el.textContent.trim();
            if (text) item.awards.push(text);
        });

        if (item.listing_url) {
            if (item.listing_url.includes('Hotel')) item.category = 'hotel';
            else if (item.listing_url.includes('Restaurant')) item.category = 'restaurant';
            else if (item.listing_url.includes('Attraction')) item.category = 'attraction';
        }

        if (item.name && item.name.length > 2) {
            results.push(item);
        }
    });

    return results;
}
"""


REVIEWS_EXTRACT_JS = """
(maxReviews) => {
    const reviews = [];
    const reviewContainers = document.querySelectorAll(
        '[data-testid="review"], .review-container, [class*="review"], ' +
        '[data-automation="review-card"]'
    );

    reviewContainers.forEach((container, idx) => {
        if (reviews.length >= maxReviews) return;

        const review = {
            title: '',
            rating: null,
            text: '',
            author: '',
            date: '',
            trip_type: null,
            helpful_votes: 0
        };

        const titleSelectors = [
            '[data-testid="review-title"]', '.title', 'h3',
            '[class*="title"]', 'a[class*="review"]'
        ];
        for (let sel of titleSelectors) {
            const el = container.querySelector(sel);
            if (el && el.textContent.trim()) {
                review.title = el.textContent.trim();
                break;
            }
        }

        const ratingEl = container.querySelector('[aria-label*="bubble"], [class*="bubble"], svg[title]');
        if (ratingEl) {
            const label = ratingEl.getAttribute('aria-label') || ratingEl.getAttribute('title') || '';
            const match = label.match(/(\\d\\.?\\d*)/);
            if (match) review.rating = parseFloat(match[1]);
        }

        const textSelectors = [
            '[data-testid="review-text"]', '[class*="review-text"]',
            '.entry', '[class*="content"]', 'p'
        ];
        for (let sel of textSelectors) {
            const el = container.querySelector(sel);
            if (el && el.textContent.trim().length > 20) {
                review.text = el.textContent.trim();
                break;
            }
        }

        const authorSelectors = [
            '[data-testid="review-author"]', '[class*="author"]',
            '[class*="username"]', 'a[href*="/Profile"]'
        ];
        for (let sel of authorSelectors) {
            const el = container.querySelector(sel);
            if (el && el.textContent.trim()) {
                review.author = el.textContent.trim();
                break;
            }
        }

        const dateSelectors = [
            '[data-testid="review-date"]', '[class*="date"]',
            'time', 'span[class*="date"]'
        ];
        for (let sel of dateSelectors) {
            const el = container.querySelector(sel);
            if (el && el.textContent.trim()) {
                review.date = el.textContent.trim();
                break;
            }
        }

        const tripTypeSelectors = [
            '[class*="trip-type"]', '[class*="stay-type"]',
            '[class*="travel-type"]'
        ];
        for (let sel of tripTypeSelectors) {
            const el = container.querySelector(sel);
            if (el && el.textContent.trim()) {
                review.trip_type = el.textContent.trim();
                break;
            }
        }

        const helpfulEl = container.querySelector('[class*="helpful"], [aria-label*="helpful"]');
        if (helpfulEl) {
            const text = helpfulEl.textContent || '';
            const match = text.match(/(\\d+)/);
            if (match) review.helpful_votes = parseInt(match[1]);
        }

        if (review.text || review.title) {
            reviews.push(review);
        }
    });

    return reviews;
}
"""
