"""crawlrx-stealth engine: in-house stealth browser facade.

Replaces the pip ``botright`` dependency behind the stealth solver. The
engine launches persistent Chromium contexts with coherent forged identities,
applies CDP-level user-agent-brand overrides, humanized-input stealth, and
exposes the ``solve_hcaptcha`` / ``solve_recaptcha`` hooks backed by
CrawlRX's own bridges.

Lane selection (strangler fig): ``CRAWLRX_STEALTH_ENGINE=own`` (default) uses
this engine; ``=botright`` keeps the legacy pip-botright path working while
it exists. See ``solvers._botright_browser.get_botright_client``.
"""
from .engine import StealthEngine

__all__ = ["StealthEngine"]
