"""DOM/token helper bodies for the Turnstile solver.

Split out of `turnstile_solver.py` to keep the orchestrator file under the
project size ceiling. Each function takes the same explicit dependencies
(page + selector lists) so the public TurnstileSolver methods are tiny
adapters.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from playwright.async_api import Page

from ._turnstile_strategies import DOM_QUERY_ERRORS


logger = logging.getLogger(__name__)


# JavaScript bodies live as module-level constants so the helper functions
# stay short and the strings can be reused without re-parsing.
_TOKEN_JS_SWEEP = """
() => {
    const inputs = document.querySelectorAll(
        'input[name*="turnstile"], input[id*="turnstile"]'
    );
    for (const input of inputs) {
        if (input.value && input.value.length > 20) return input.value;
    }
    const forms = document.querySelectorAll('form');
    for (const form of forms) {
        const formData = new FormData(form);
        for (const [key, value] of formData.entries()) {
            if (key.includes('turnstile') && value.length > 20) return value;
        }
    }
    if (window.turnstile && window.turnstile.getResponse) {
        return window.turnstile.getResponse();
    }
    return null;
}
"""

_INTERCEPTOR_JS = """
window.__capturedTurnstileToken = null;
const originalRender = window.turnstile?.render;
if (window.turnstile) {
    const originalTurnstileRender = window.turnstile.render;
    window.turnstile.render = function(container, params) {
        const originalCallback = params.callback;
        params.callback = function(token) {
            window.__capturedTurnstileToken = token;
            if (originalCallback) originalCallback(token);
        };
        return originalTurnstileRender.call(this, container, params);
    };
}
const observer = new MutationObserver((mutations) => {
    const tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
    if (tokenInput && tokenInput.value) {
        window.__capturedTurnstileToken = tokenInput.value;
    }
});
observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['value']
});
"""


async def read_selector_value(page: Page, selector: str) -> Optional[str]:
    """Single-selector lookup with bounded error suppression."""
    try:
        element = await page.query_selector(selector)
        if not element:
            return None
        value = await element.get_attribute('value')
        if value and len(value) > 20:
            return value
    except DOM_QUERY_ERRORS:
        logger.debug("read_selector_value swallowed DOM error for %s", selector)
    return None


async def extract_token_via_selectors(
    page: Page, selectors: List[str]
) -> Optional[str]:
    """Method 1: Direct input value via known token selectors."""
    for selector in selectors:
        value = await read_selector_value(page, selector)
        if value:
            return value
    return None


async def extract_token_via_js(page: Page) -> Optional[str]:
    """Method 2: JavaScript evaluation across forms / window globals."""
    token = await page.evaluate(_TOKEN_JS_SWEEP)
    if token and len(str(token)) > 20:
        return str(token)
    return None


async def extract_turnstile_token(
    page: Page, selectors: List[str]
) -> Optional[str]:
    """Extract Turnstile token from page using multiple methods."""
    try:
        value = await extract_token_via_selectors(page, selectors)
        if value:
            return value
        value = await extract_token_via_js(page)
        if value:
            return value
    except DOM_QUERY_ERRORS as e:
        logger.debug("Token extraction error: %s", e)
    return None


async def is_turnstile_present(
    page: Page, iframe_selectors: List[str]
) -> bool:
    """Check if Turnstile challenge is present on page."""
    try:
        for selector in iframe_selectors:
            element = await page.query_selector(selector)
            if element:
                return True
        content = await page.content()
        indicators = (
            'challenges.cloudflare.com/turnstile',
            'cf-turnstile',
            'data-cf-turnstile',
            'turnstile.js',
            'cf_chl_',
            '__cf_bm',
        )
        return any(ind in content for ind in indicators)
    except DOM_QUERY_ERRORS as e:
        logger.debug("Turnstile detection error: %s", e)
    return False


async def is_challenge_visible(page: Page) -> bool:
    """Check if a visible challenge is displayed."""
    try:
        challenge = await page.query_selector(
            '.cf-turnstile iframe, .cf-challenge iframe'
        )
        if challenge:
            return await challenge.is_visible()
    except DOM_QUERY_ERRORS:
        logger.debug("is_challenge_visible swallowed DOM error")
    return False


async def inject_token_interceptor(page: Page) -> None:
    """Inject script to intercept Turnstile token callbacks."""
    await page.add_init_script(_INTERCEPTOR_JS)
