"""_producthunt_js.py -- Page-context JS extractors for Product Hunt scraping.

Exports the three JS payloads driven from :mod:`_producthunt_pages`. Kept in
its own module so the long string literals don't push the page-driver code
past the the function-length limit / file-line ceilings.
"""
from __future__ import annotations

JS_EXTRACT_SEARCH_RESULTS = r"""
(maxResults) => {
    const products = [];
    const cards = document.querySelectorAll('[data-testid="product-card"], .styles_item__DqM1l, [class*="item_"], article');

    for (let i = 0; i < Math.min(cards.length, maxResults); i++) {
        const card = cards[i];
        const product = {
            name: '',
            tagline: '',
            upvotes: 0,
            comments_count: 0,
            product_url: '',
            image_url: null,
            topics: [],
            maker_name: null,
            launch_date: null
        };

        const nameEl = card.querySelector('h2, h3, [class*="title"], a[class*="link"]');
        if (nameEl) product.name = nameEl.textContent.trim();

        const taglineEl = card.querySelector('[class*="tagline"], p, [class*="description"]');
        if (taglineEl) product.tagline = taglineEl.textContent.trim();

        const linkEl = card.querySelector('a[href*="/posts/"], a[href*="/products/"]');
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            product.product_url = 'https://www.producthunt.com' + href;
        }

        const imgEl = card.querySelector('img');
        if (imgEl) product.image_url = imgEl.getAttribute('src');

        const voteEl = card.querySelector('[class*="vote"], [class*="upvote"], button[class*="_vote"]');
        if (voteEl) {
            const text = voteEl.textContent.trim();
            const match = text.match(/([\d.]+k?)/i);
            if (match) product.upvotes = match[1].toLowerCase().includes('k') ?
                parseFloat(match[1]) * 1000 : parseInt(match[1]);
        }

        const commentEl = card.querySelector('[class*="comment"], a[href*="#comments"]');
        if (commentEl) {
            const text = commentEl.textContent.trim();
            const match = text.match(/([\d.]+k?)\s*comment/i);
            if (match) product.comments_count = match[1].toLowerCase().includes('k') ?
                parseFloat(match[1]) * 1000 : parseInt(match[1]);
        }

        const topicEls = card.querySelectorAll('[class*="topic"], a[href*="/topics/"]');
        topicEls.forEach(el => {
            const text = el.textContent.trim();
            if (text && text.length < 50) product.topics.push(text);
        });

        const makerEl = card.querySelector('[class*="maker"], [class*="author"], a[href*="/@"]');
        if (makerEl) product.maker_name = makerEl.textContent.trim();

        if (product.name) products.push(product);
    }

    return products;
}
"""


JS_EXTRACT_TODAY_RESULTS = r"""
(maxResults) => {
    const products = [];
    const cards = document.querySelectorAll('[data-testid="product-card"], .styles_item__DqM1l, [class*="styles_item"], section article, [class*="_item"]');

    for (let i = 0; i < Math.min(cards.length, maxResults); i++) {
        const card = cards[i];
        const product = {
            name: '',
            tagline: '',
            upvotes: 0,
            comments_count: 0,
            product_url: '',
            image_url: null,
            topics: [],
            maker_name: null,
            launch_date: null
        };

        const nameSelectors = ['h2', 'h3', '[class*="title"]', 'a[data-testid="product-name"]', 'a strong'];
        for (const sel of nameSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                product.name = el.textContent.trim();
                break;
            }
        }

        const taglineSelectors = ['[class*="tagline"]', '[class*="description"]', 'p', '[class*="subtitle"]'];
        for (const sel of taglineSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                product.tagline = el.textContent.trim();
                break;
            }
        }

        const linkEl = card.querySelector('a[href*="/posts/"], a[href*="/products/"], a[href^="/"]');
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            if (href && !href.startsWith('http')) {
                product.product_url = 'https://www.producthunt.com' + href;
            } else if (href) {
                product.product_url = href;
            }
        }

        const imgEl = card.querySelector('img[src*="producthunt"], img[data-testid="product-image"], img');
        if (imgEl) product.image_url = imgEl.getAttribute('src');

        const voteSelectors = ['[data-testid="vote-button"]', '[class*="vote"]', 'button[class*="_vote"]', '[class*="upvoteCount"]'];
        for (const sel of voteSelectors) {
            const el = card.querySelector(sel);
            if (el) {
                const text = el.textContent.trim();
                const match = text.match(/([\d,k]+)/i);
                if (match) {
                    const num = match[1].toLowerCase().replace('k', '');
                    product.upvotes = match[1].includes('k') ? parseFloat(num) * 1000 : parseInt(num);
                    break;
                }
            }
        }

        const commentSelectors = ['[data-testid="comment-count"]', '[class*="comment"]', 'a[href*="#comments"]'];
        for (const sel of commentSelectors) {
            const el = card.querySelector(sel);
            if (el) {
                const text = el.textContent.trim();
                const match = text.match(/([\d,k]+)/);
                if (match) {
                    const num = match[1].toLowerCase().replace('k', '');
                    product.comments_count = match[1].includes('k') ? parseFloat(num) * 1000 : parseInt(num);
                    break;
                }
            }
        }

        const topicEls = card.querySelectorAll('a[href*="/topics/"], [class*="topic"]');
        topicEls.forEach(el => {
            const text = el.textContent.trim();
            if (text && text.length < 30 && !text.includes('product')) {
                product.topics.push(text);
            }
        });

        const makerSelectors = ['[data-testid="maker-name"]', '[class*="maker"]', 'a[href^="/@"]'];
        for (const sel of makerSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                product.maker_name = el.textContent.trim();
                break;
            }
        }

        if (product.name) products.push(product);
    }

    return products;
}
"""


JS_EXTRACT_PRODUCT_DETAIL = r"""
() => {
    const data = {
        name: '',
        tagline: '',
        description: '',
        upvotes: 0,
        comments_count: 0,
        product_url: window.location.href,
        image_url: null,
        topics: [],
        maker_name: null,
        makers: [],
        launch_date: null,
        comments: [],
        gallery_images: [],
        external_url: null,
        pricing: null
    };

    const nameSelectors = ['h1', '[data-testid="product-name"]', '[class*="product-name"]', '[class*="title_"]'];
    for (const sel of nameSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
            data.name = el.textContent.trim();
            break;
        }
    }

    const taglineSelectors = ['[data-testid="product-tagline"]', '[class*="tagline"]', '[class*="subtitle"]', 'h1 + p, h2 + p'];
    for (const sel of taglineSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
            data.tagline = el.textContent.trim();
            break;
        }
    }

    const descSelectors = ['[data-testid="product-description"]', '[class*="description_"]', '[class*="about"]', 'section p'];
    for (const sel of descSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim().length > 20) {
            data.description = el.textContent.trim();
            break;
        }
    }

    const voteSelectors = ['[data-testid="vote-count"]', '[class*="voteCount"]', '[class*="upvoteCount"]', 'button[class*="vote"]'];
    for (const sel of voteSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const text = el.textContent.trim();
            const match = text.match(/([\d,k]+)/i);
            if (match) {
                const num = match[1].toLowerCase().replace('k', '');
                data.upvotes = match[1].includes('k') ? parseFloat(num) * 1000 : parseInt(num);
                break;
            }
        }
    }

    const commentCountSelectors = ['[data-testid="comment-count"]', '[class*="commentCount"]', 'a[href*="#comments"]'];
    for (const sel of commentCountSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const text = el.textContent.trim();
            const match = text.match(/([\d,k]+)/i);
            if (match) {
                const num = match[1].toLowerCase().replace('k', '');
                data.comments_count = match[1].includes('k') ? parseFloat(num) * 1000 : parseInt(num);
                break;
            }
        }
    }

    const imgSelectors = ['[data-testid="product-image"]', 'img[class*="productImage"]', 'main img', 'article img'];
    for (const sel of imgSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            data.image_url = el.getAttribute('src');
            break;
        }
    }

    const galleryEls = document.querySelectorAll('[data-testid="gallery-image"], [class*="gallery"] img, [class*="screenshot"] img');
    galleryEls.forEach(el => {
        const src = el.getAttribute('src');
        if (src) data.gallery_images.push(src);
    });

    const topicEls = document.querySelectorAll('a[href*="/topics/"], [class*="topic"] a');
    topicEls.forEach(el => {
        const text = el.textContent.trim();
        if (text && text.length < 30) data.topics.push(text);
    });

    const makerEls = document.querySelectorAll('[data-testid="maker-name"], a[href^="/@"], [class*="maker"] a');
    makerEls.forEach(el => {
        const text = el.textContent.trim();
        if (text && text.length < 50 && !data.makers.includes(text)) {
            data.makers.push(text);
            if (!data.maker_name) data.maker_name = text;
        }
    });

    const externalSelectors = ['a[data-testid="get-it-button"]', 'a[rel*="noopener"]', 'a[href^="http"]:not([href*="producthunt.com"])'];
    for (const sel of externalSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const href = el.getAttribute('href');
            if (href && !href.includes('producthunt.com')) {
                data.external_url = href;
                break;
            }
        }
    }

    const priceSelectors = ['[data-testid="pricing"]', '[class*="pricing"]', '[class*="price"]'];
    for (const sel of priceSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
            data.pricing = el.textContent.trim();
            break;
        }
    }

    const dateSelectors = ['time', '[data-testid="launch-date"]', '[class*="date"]'];
    for (const sel of dateSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const datetime = el.getAttribute('datetime');
            if (datetime) {
                data.launch_date = datetime;
                break;
            }
            const text = el.textContent.trim();
            if (text && (text.includes('202') || text.includes('launched'))) {
                data.launch_date = text;
                break;
            }
        }
    }

    const commentEls = document.querySelectorAll('[data-testid="comment"], [class*="comment_"], [class*="Comment_"]');
    let count = 0;
    commentEls.forEach(el => {
        if (count >= 5) return;

        const comment = {
            author: '',
            text: '',
            likes: 0
        };

        const authorEl = el.querySelector('a[href^="/@"], [class*="author"]');
        if (authorEl) comment.author = authorEl.textContent.trim();

        const textEl = el.querySelector('p, [class*="text"], [class*="content"]');
        if (textEl) comment.text = textEl.textContent.trim();

        const likesEl = el.querySelector('[class*="like"], [class*="vote"]');
        if (likesEl) {
            const text = likesEl.textContent.trim();
            const match = text.match(/([\d,k]+)/);
            if (match) {
                const num = match[1].toLowerCase().replace('k', '');
                comment.likes = match[1].includes('k') ? parseFloat(num) * 1000 : parseInt(num);
            }
        }

        if (comment.text) {
            data.comments.push(comment);
            count++;
        }
    });

    return data;
}
"""
