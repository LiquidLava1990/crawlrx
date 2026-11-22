"""
Generic product extractor -- works on ANY ecommerce page.
Uses schema.org/Product JSON-LD, Open Graph, and HTML heuristics.
This is the fallback when no known platform is detected.
"""
import json as json_mod
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._scraper_helpers import (
    ELEMENT_PROBE_EXCEPTIONS,
    PARSE_EXCEPTIONS,
    network_exception_types,
    safe_extract,
)

logger = logging.getLogger('crawlrx.generic_product')

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/122.0.0.0 Safari/537.36'
)

_PRICE_REGEX = re.compile(r'[\$\€\£\¥]\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)')
_PRICE_REGEX_PLAIN = re.compile(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*(?:USD|EUR|GBP|\$|\€|\£)?')
_CURRENCY_MAP = {
    '$': 'USD',
    '€': 'EUR',
    '£': 'GBP',
    '¥': 'JPY',
}

_FETCH_EXCEPTIONS: tuple[type[BaseException], ...] = network_exception_types() + (
    requests.RequestException,
)

_PRODUCT_CONTAINER_SELECTORS: list[str] = [
    '[class*="product"]',
    '[class*="item"]',
    '[class*="card"]',
    '.product',
    '.product-item',
    '.product-card',
    '[data-product]',
    '[data-item]',
]


def _fetch_soup(url: str, timeout: int) -> Optional[BeautifulSoup]:
    """Fetch and parse a page, returning ``None`` on transport / parse failure."""
    headers = {'User-Agent': _USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, 'html.parser')
    except _FETCH_EXCEPTIONS as exc:
        logger.error('Failed to fetch %s: %s', url, exc)
        return None


def _dedupe_append(
    products: List[Dict[str, Any]],
    new_items: List[Dict[str, Any]],
    seen_titles: set,
) -> None:
    """Append unseen ``new_items`` (by lowercased title) into ``products``."""
    for product in new_items:
        title_key = product.get('title', '').lower().strip()
        if title_key and title_key not in seen_titles:
            seen_titles.add(title_key)
            products.append(product)


def _filter_by_query(products: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Keep products whose lowercased title contains ``query`` or any long word."""
    query_lower = query.lower()
    query_words = [word for word in query_lower.split() if len(word) > 2]
    filtered: List[Dict[str, Any]] = []
    for product in products:
        title = product.get('title', '').lower()
        if query_lower in title or any(word in title for word in query_words):
            filtered.append(product)
    return filtered


def _safe_collect(
    extractor_name: str,
    extractor: Any,
) -> List[Dict[str, Any]]:
    """Run a JSON-LD / OG / HTML extractor with narrow error handling."""
    try:
        return extractor() or []
    except (*PARSE_EXCEPTIONS, *ELEMENT_PROBE_EXCEPTIONS) as exc:
        logger.debug('%s extraction failed: %s', extractor_name, exc)
        return []


def extract_products_generic(
    url: str,
    query: Optional[str] = None,
    limit: int = 50,
    timeout: int = 10,
) -> List[Dict[str, Any]]:
    """
    Extract product data from ANY page using:
    1. JSON-LD schema.org/Product blocks (most reliable)
    2. Open Graph product tags (og:price:amount, og:title, etc.)
    3. HTML heuristics -- elements with price-like text near product titles

    Returns [] on total failure, never raises.
    Each product: {title, price (float or None), currency, url, image, source}
    """
    soup = _fetch_soup(url, timeout)
    if soup is None:
        return []

    all_products: List[Dict[str, Any]] = []
    seen_titles: set = set()

    _dedupe_append(
        all_products,
        _safe_collect('JSON-LD', lambda: extract_jsonld_products(soup, url)),
        seen_titles,
    )
    _dedupe_append(
        all_products,
        _safe_collect('Open Graph', lambda: extract_og_product(soup, url)),
        seen_titles,
    )

    remaining = limit - len(all_products)
    if remaining > 0:
        _dedupe_append(
            all_products,
            _safe_collect(
                'HTML heuristic',
                lambda: extract_html_products(soup, url, remaining),
            ),
            seen_titles,
        )

    if query and all_products:
        all_products = _filter_by_query(all_products, query)

    return all_products[:limit]


def _iter_jsonld_blocks(soup: BeautifulSoup) -> List[Any]:
    """Decode every ``<script type="application/ld+json">`` payload."""
    payloads: List[Any] = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            payloads.append(json_mod.loads(script.string or '{}'))
        except (json_mod.JSONDecodeError, TypeError):
            continue
    return payloads


def _extract_from_itemlist(data: Dict, page_url: str) -> List[Dict]:
    """Pull product entries out of a JSON-LD ``ItemList`` envelope."""
    items = data.get('itemListElement', [])
    products: List[Dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product_data = item.get('item', item) if 'item' in item else item
        if not isinstance(product_data, dict):
            continue
        product = _parse_jsonld_product(product_data, page_url)
        if product:
            products.append(product)
    return products


def _extract_from_jsonld_dict(data: Dict, page_url: str) -> List[Dict]:
    """Dispatch a single JSON-LD ``dict`` payload by its declared @type."""
    data_type = data.get('@type', '') or data.get('type', '')
    if not isinstance(data_type, str):
        return []
    if 'Product' in data_type:
        product = _parse_jsonld_product(data, page_url)
        return [product] if product else []
    if 'ItemList' in data_type:
        return _extract_from_itemlist(data, page_url)
    return []


def _extract_from_jsonld_list(data: list, page_url: str) -> List[Dict]:
    """Walk a JSON-LD ``list`` (``@graph`` shape) collecting products."""
    products: List[Dict] = []
    for item in data:
        if isinstance(item, dict):
            products.extend(_extract_from_jsonld_dict(item, page_url))
    return products


def extract_jsonld_products(soup: BeautifulSoup, page_url: str) -> List[Dict]:
    """Extract products from ``<script type="application/ld+json">`` blocks."""
    products: List[Dict] = []
    for data in _iter_jsonld_blocks(soup):
        if isinstance(data, dict):
            products.extend(_extract_from_jsonld_dict(data, page_url))
        elif isinstance(data, list):
            products.extend(_extract_from_jsonld_list(data, page_url))
    return products


def _jsonld_price_and_currency(data: Dict) -> tuple[Optional[float], str]:
    """Extract ``(price, currency)`` from a JSON-LD ``offers`` field."""
    offers = data.get('offers') or data.get('offer')
    if not offers:
        return None, 'USD'
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        return None, 'USD'
    price_val = offers.get('price')
    price = _parse_price_value(str(price_val)) if price_val else None
    currency = offers.get('priceCurrency', 'USD')
    return price, currency


def _jsonld_image(data: Dict) -> Optional[str]:
    """Extract a single image URL from a JSON-LD ``image`` field."""
    images = data.get('image')
    if not images:
        return None
    if isinstance(images, list):
        return images[0] if images else None
    if isinstance(images, str):
        return images
    if isinstance(images, dict):
        return images.get('url')
    return None


def _parse_jsonld_product(data: Dict, page_url: str) -> Optional[Dict]:
    """Parse a single JSON-LD product object into normalized format."""
    title = data.get('name') or data.get('title', '')
    if not title:
        return None

    price, currency = _jsonld_price_and_currency(data)
    image = _jsonld_image(data)

    product_url = data.get('url') or page_url
    if product_url and not product_url.startswith('http'):
        product_url = urljoin(page_url, product_url)

    return {
        'title': str(title).strip(),
        'price': price,
        'currency': currency,
        'url': product_url,
        'image': image,
        'source': 'json_ld',
    }


def _parse_price_value(value: str) -> Optional[float]:
    """Parse a price string into a float."""
    try:
        cleaned = (
            value.replace(',', '')
            .replace('$', '')
            .replace('€', '')
            .replace('£', '')
            .strip()
        )
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


_OG_PRICE_KEYS: tuple[str, ...] = (
    'og:price:amount',
    'product:price:amount',
    'product:original_price:amount',
    'twitter:data1',
)
_OG_CURRENCY_KEYS: tuple[str, ...] = (
    'og:price:currency',
    'product:price:currency',
)


def _og_first_price(soup: BeautifulSoup) -> Optional[float]:
    """First parseable price found in any of the known OG/Twitter meta keys."""
    for key in _OG_PRICE_KEYS:
        raw = _get_meta_content(soup, key)
        if not raw:
            continue
        parsed = _parse_price_value(raw)
        if parsed:
            return parsed
    return None


def _og_currency(soup: BeautifulSoup) -> str:
    """First currency found in OG meta keys, uppercased; defaults to USD."""
    for key in _OG_CURRENCY_KEYS:
        currency = _get_meta_content(soup, key)
        if currency:
            return currency.upper()
    return 'USD'


def extract_og_product(soup: BeautifulSoup, page_url: str) -> List[Dict]:
    """Extract a product entry from Open Graph / Twitter meta tags."""
    title = _get_meta_content(soup, 'og:title') or _get_meta_content(soup, 'title')
    if not title:
        return []

    price = _og_first_price(soup)
    currency = _og_currency(soup)
    image = _get_meta_content(soup, 'og:image') or _get_meta_content(soup, 'twitter:image')

    if not price and _get_meta_content(soup, 'og:type') != 'product':
        return []

    return [
        {
            'title': title,
            'price': price,
            'currency': currency,
            'url': page_url,
            'image': image,
            'source': 'og',
        }
    ]


def _get_meta_content(soup: BeautifulSoup, property_name: str) -> Optional[str]:
    """Get content from a meta tag by property or name attribute."""
    tag = soup.find('meta', property=property_name) or soup.find(
        'meta', attrs={'name': property_name}
    )
    if tag:
        return tag.get('content') or tag.get('value')
    return None


def _collect_candidate_elements(soup: BeautifulSoup, target: int) -> list:
    """Run each container selector, returning early once we have enough."""
    found: list = []
    for selector in _PRODUCT_CONTAINER_SELECTORS:
        try:
            found.extend(soup.select(selector))
        except (ValueError, TypeError, AttributeError):
            continue
        if len(found) >= target:
            break
    return found


def extract_html_products(
    soup: BeautifulSoup,
    page_url: str,
    limit: int,
) -> List[Dict]:
    """Heuristic extraction from product-shaped HTML containers."""
    products: List[Dict] = []
    seen_urls: set = set()
    candidates = _collect_candidate_elements(soup, limit * 2)

    for elem in candidates:
        if len(products) >= limit:
            break
        product = safe_extract(
            lambda elem=elem: _extract_product_from_element(elem, page_url),
            field='html_card',
            log=logger,
        )
        if not product or not product.get('title'):
            continue
        url_key = product.get('url', '')
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        products.append(product)
    return products


def _find_card_title(elem: Any) -> Optional[str]:
    """Pick the most title-shaped text out of an HTML card element."""
    for tag_name in ('h2', 'h3', 'h4', 'strong', 'a', 'span', 'div'):
        title_elem = elem.find(tag_name)
        if not title_elem:
            continue
        text = title_elem.get_text(strip=True)
        if 2 < len(text) < 200:
            return text
    text = elem.get_text(strip=True)
    if not text:
        return None
    return text[:200]


def _find_card_price(elem_text: str) -> tuple[Optional[float], str]:
    """Extract ``(price, currency)`` from a card's combined text."""
    match = _PRICE_REGEX.search(elem_text)
    if match:
        try:
            price = float(match.group(1).replace(',', ''))
            currency_sym = match.group(0)[0]
            return price, _CURRENCY_MAP.get(currency_sym, 'USD')
        except (ValueError, IndexError):
            pass
    match = _PRICE_REGEX_PLAIN.search(elem_text)
    if match:
        try:
            return float(match.group(1).replace(',', '')), 'USD'
        except ValueError:
            pass
    return None, 'USD'


def _resolve_image_url(elem: Any, page_url: str) -> Optional[str]:
    """Return a fully-qualified image URL or ``None``."""
    img = elem.find('img')
    if not img:
        return None
    image = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
    if not image:
        return None
    if image.startswith('//'):
        return 'https:' + image
    if not image.startswith('http'):
        return urljoin(page_url, image)
    return image


def _resolve_card_link(elem: Any, page_url: str) -> str:
    """Return a fully-qualified product URL, falling back to ``page_url``."""
    link = elem.find('a', href=True)
    if not link:
        return page_url
    href = link['href']
    if not href.startswith('http'):
        return urljoin(page_url, href)
    return href


def _extract_product_from_element(elem: Any, page_url: str) -> Optional[Dict]:
    """Extract product data from a single HTML element."""
    title = _find_card_title(elem)
    if not title or len(title) < 3:
        return None

    price, currency = _find_card_price(elem.get_text())
    image = _resolve_image_url(elem, page_url)
    product_url = _resolve_card_link(elem, page_url)

    return {
        'title': title,
        'price': price,
        'currency': currency,
        'url': product_url,
        'image': image,
        'source': 'html',
    }


