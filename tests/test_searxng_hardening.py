"""Guardrails for the hardened CrawlRX -> SearXNG integration."""
from __future__ import annotations

import importlib
import json
import urllib.parse
from pathlib import Path


def _query_params(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def test_search_engine_searxng_url_uses_hardened_web_core() -> None:
    mod = importlib.import_module("crawlrx._search_engines")

    params = _query_params(mod._build_searxng_url("openai research", "en"))

    assert params["q"] == ["openai research"]
    assert params["format"] == ["json"]
    assert params["engines"] == ["bing,wikipedia"]
    assert "categories" not in params


def test_google_search_searxng_url_maps_categories_to_engine_groups() -> None:
    mod = importlib.import_module("crawlrx.google_search")

    news = _query_params(mod._build_searxng_url("openai", "en", None, "news"))
    images = _query_params(mod._build_searxng_url("openai", "en", None, "images"))
    videos = _query_params(mod._build_searxng_url("openai", "en", None, "videos"))
    general = _query_params(mod._build_searxng_url("openai", "en", None, "general"))

    assert news["engines"] == ["bing news,reuters,wikinews"]
    assert images["engines"] == ["bing images,openverse,wikicommons.images,lucide"]
    assert videos["engines"] == ["youtube,dailymotion,sepiasearch,wikicommons.videos"]
    assert general["engines"] == ["bing,wikipedia"]
    assert "categories" not in news


def test_searxng_settings_keep_only_removes_known_bad_default_core() -> None:
    settings = Path(Path(__file__).resolve().parents[1] / "deploy/searxng/settings.yml").read_text(encoding="utf-8")

    assert "keep_only:" in settings
    assert "- bing" in settings
    assert "- mojeek" in settings
    assert "- presearch" in settings
    assert "- duckduckgo" not in settings
    assert "- google" not in settings
    assert "- startpage" not in settings


def test_source_registry_is_the_search_source_of_truth() -> None:
    registry_path = Path(Path(__file__).resolve().parents[1] / "crawlrx/source_registry.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    groups = registry["searxng"]["groups"]
    checks = registry["diagnostics"]["checks"]

    assert groups["web"]["engines"] == "bing,wikipedia"
    assert groups["news"]["engines"] == "bing news,reuters,wikinews"
    assert len(checks) >= 5
    assert {item["group"] for item in checks} >= {"web", "news", "images", "videos"}
