"""
main.py - CrawlRX FastAPI application entry point
Self-hosted web API: crawl, scrape, search, extract and vision via Playwright
plus any OpenAI-compatible model server. Default port: 7337.
"""

import os
import sys
import io

# Force UTF-8 encoding to avoid cp1252 crashes on Windows with unicode chars
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import logging
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import asyncio

# Setup logging FIRST (before router imports that use logger)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("crawlrx")

# -- Timeout Middleware ------------------------------------------------------
class TimeoutMiddleware(BaseHTTPMiddleware):
    """Global request timeout middleware.

    Uses 180s for browser endpoints (crawl/scrape/search/batch/queue) to allow
    long-running Playwright operations to complete. 30s for everything else.
    """
    _LONG_PREFIXES = ("/crawl", "/scrape", "/search", "/batch", "/queue", "/map", "/screenshot", "/monitor")
    _MODEL_PREFIXES = ("/extract", "/vision")

    @classmethod
    def route_timeout(cls, path: str) -> float:
        """Seconds a request may run: covers model time and human captcha waits."""
        from .captcha_handoff import handoff_wait_seconds
        from .model_backend import llm_timeout_seconds

        if any(path.startswith(p) for p in cls._MODEL_PREFIXES):
            # page load + one call + one reasoning-off retry
            return 60.0 + 2 * llm_timeout_seconds() + 30.0 + handoff_wait_seconds()
        if any(path.startswith(p) for p in cls._LONG_PREFIXES):
            return 180.0 + handoff_wait_seconds()
        return 30.0

    async def dispatch(self, request: object, call_next: object) -> object:
        cap = self.route_timeout(request.url.path)
        try:
            return await asyncio.wait_for(call_next(request), timeout=cap)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"success": False, "error": f"Request timeout ({int(cap)}s)"},
                status_code=504
            )

# -"--"- Import routers -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
from .crawl import router as crawl_router
from .scrape import router as scrape_router
from .search import router as search_router
from .stats import router as stats_router, record_request as record_stats, get_stats
from .batch import router as batch_router
from .metadata import router as metadata_router
from .sessions import router as sessions_router
from .queue import router as queue_router, init_queue, shutdown_queue
from .learning import router as learning_router, init_db as init_learning_db
from .intelligence import (
    router as intelligence_router,
    start_intelligence, stop_intelligence,
)
from .gmaps import router as gmaps_router
from .linkedin import router as linkedin_router
from .tiktok import router as tiktok_router
from .instagram import router as instagram_router
from .sitemap import sitemap_router
from .extract import router as extract_router
from . import __version__
from .vision import router as vision_router
from .doctor import startup_summary
from .js_extract import router as js_extract_router
from .reddit import reddit_router
from .youtube import router as youtube_router
from .search_media import router as search_media_router
from .search_deep import router as search_deep_router
from .search_fusion import router as search_fusion_router
from .search_orchestrator import router as search_orchestrator_router
from .scrape_diagnostics import router as scrape_diagnostics_router
from .contacts import router as contacts_router
from .local_search import router as local_search_router
from .amazon import router as amazon_router
from .screenshot import router as screenshot_router
from .shopify import router as shopify_router
from .twitter import router as twitter_router
from .yelp import router as yelp_router
from .facebook import router as facebook_router
from .monitor import router as monitor_router
from .pwc import router as pwc_router
from .craigslist import router as craigslist_router
from .zillow import router as zillow_router
from .walmart import router as walmart_router
from .google_search import router as google_search_router
from .airbnb import router as airbnb_router
from .indeed import router as indeed_router
from .ebay import router as ebay_router
from .google_maps import router as google_maps_router
from .redfin import router as redfin_router
from .trulia import router as trulia_router
from .homedepot import router as homedepot_router
from .autotrader import router as autotrader_router
from .apartments import router as apartments_router
from .cars_com import router as cars_com_router
from .etsy import router as etsy_router
from .tripadvisor import router as tripadvisor_router
from .bestbuy import router as bestbuy_router
from .costco import router as costco_router
from .hotel_scraper import router as hotel_router
from .realtor import router as realtor_router
from .stockx import router as stockx_router
from .expedia import router as expedia_router
from .target import router as target_router
from .glassdoor import router as glassdoor_router
from .google_trends import router as google_trends_router
from .semrush import router as semrush_router
from .weather import router as weather_router
from .github_scraper import router as github_scraper_router
from .producthunt import router as producthunt_router
from .angellist import router as angellist_router
from .kayak import router as kayak_router
from .pinterest import router as pinterest_router
from .trustpilot import router as trustpilot_router
from .ecommerce_search import router as ecommerce_search_router
from .models import HealthResponse, CacheStatsResponse, ScrapeRequest
from .cache import get_cache
from .utils import get_all_circuit_breakers
from .platform_detect import detect_platform

# Platform scraper imports (for /scrape/platform routing)
try:
    from .shopify import scrape_shopify_products
except ImportError:
    scrape_shopify_products = None

try:
    from .woocommerce import scrape_woocommerce_products
except ImportError:
    scrape_woocommerce_products = None

try:
    from .wordpress import scrape_wordpress_products
except ImportError:
    scrape_wordpress_products = None

try:
    from .squarespace_scraper import scrape_squarespace_products
except ImportError:
    scrape_squarespace_products = None

try:
    from .wix_scraper import scrape_wix_products
except ImportError:
    scrape_wix_products = None

try:
    from .bigcommerce_scraper import scrape_bigcommerce_products
except ImportError:
    scrape_bigcommerce_products = None

try:
    from .magento_scraper import scrape_magento_products
except ImportError:
    scrape_magento_products = None

try:
    from .prestashop_scraper import scrape_prestashop_products
except ImportError:
    scrape_prestashop_products = None

try:
    from .godaddy_scraper import scrape_godaddy_products
except ImportError:
    scrape_godaddy_products = None

try:
    from .amazon_scraper import scrape_amazon_products
except ImportError:
    scrape_amazon_products = None

# Promo/Deal scraper imports (wrapped to prevent missing file crashes)
try:
    from .promo_scraper import router as promo_scraper_router
except ImportError as e:
    logger.warning("promo_scraper not loaded: %s", e)
    promo_scraper_router = None

try:
    from .honey_scraper import router as honey_scraper_router
except ImportError as e:
    logger.warning("honey_scraper not loaded: %s", e)
    honey_scraper_router = None

try:
    from .rakuten_scraper import router as rakuten_scraper_router
except ImportError as e:
    logger.warning("rakuten_scraper not loaded: %s", e)
    rakuten_scraper_router = None

try:
    from .slickdeals_scraper import router as slickdeals_scraper_router
except ImportError as e:
    logger.warning("slickdeals_scraper not loaded: %s", e)
    slickdeals_scraper_router = None

try:
    from .dealnews_scraper import router as dealnews_scraper_router
except ImportError as e:
    logger.warning("dealnews_scraper not loaded: %s", e)
    dealnews_scraper_router = None

try:
    from .camelcamelcamel_scraper import router as camelcamelcamel_scraper_router
except ImportError as e:
    logger.warning("camelcamelcamel_scraper not loaded: %s", e)
    camelcamelcamel_scraper_router = None

try:
    from .keepa_scraper import router as keepa_scraper_router
except ImportError as e:
    logger.warning("keepa_scraper not loaded: %s", e)
    keepa_scraper_router = None

try:
    from .oscommerce_scraper import router as oscommerce_scraper_router
except ImportError as e:
    logger.warning("oscommerce_scraper not loaded: %s", e)
    oscommerce_scraper_router = None

try:
    from .salesforce_scraper import router as salesforce_scraper_router
except ImportError as e:
    logger.warning("salesforce_scraper not loaded: %s", e)
    salesforce_scraper_router = None

try:
    from .hybris_scraper import router as hybris_scraper_router
except ImportError as e:
    logger.warning("hybris_scraper not loaded: %s", e)
    hybris_scraper_router = None

# Import unified promo search
try:
    from .promo_search import router as promo_search_router
except ImportError as e:
    logger.warning("promo_search not loaded: %s", e)
    promo_search_router = None

# New scrapers (knoji, coupons, capital one, google dork, price history, price compare, wayback)
try:
    from .knoji_scraper import router as knoji_scraper_router
except ImportError as e:
    logger.warning("knoji_scraper not loaded: %s", e)
    knoji_scraper_router = None

try:
    from .coupon_aggregator import router as coupon_aggregator_router
except ImportError as e:
    logger.warning("coupon_aggregator not loaded: %s", e)
    coupon_aggregator_router = None

try:
    from .capital_one_scraper import router as capital_one_scraper_router
except ImportError as e:
    logger.warning("capital_one_scraper not loaded: %s", e)
    capital_one_scraper_router = None

try:
    from .google_dork_scraper import router as google_dork_scraper_router
except ImportError as e:
    logger.warning("google_dork_scraper not loaded: %s", e)
    google_dork_scraper_router = None

try:
    from .price_history import router as price_history_router
except ImportError as e:
    logger.warning("price_history not loaded: %s", e)
    price_history_router = None

try:
    from .cross_retailer_price import router as cross_retailer_price_router
except ImportError as e:
    logger.warning("cross_retailer_price not loaded: %s", e)
    cross_retailer_price_router = None

try:
    from .wayback_scraper import router as wayback_scraper_router
except ImportError as e:
    logger.warning("wayback_scraper not loaded: %s", e)
    wayback_scraper_router = None

# -"--"- Import auth and logger -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
from .auth import APIKeyMiddleware
from .url_guard import UrlGuardMiddleware
from .logger import log_request
from .ratelimit import RateLimitMiddleware
from . import config as _cfg
from ._scraper_helpers import BROWSER_SCRAPE_EXCEPTIONS
from ._main_templates import LANDING_HTML
from .dashboard_ui import OPERATOR_DASHBOARD_HTML


# Track start time for uptime reporting
_START_TIME = time.time()
_DASHBOARD_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DASHBOARD_SEARCH_PROBE_TTL_S = int(
    os.getenv("CRAWLRX_DASHBOARD_SEARCH_PROBE_TTL_S", "900")
)

# Background thread pool for non-blocking logging
# Background request-log writer. Re-created per app lifespan so the app can be
# started more than once in one process (tests, reloaders).
_log_executor_slot: list[ThreadPoolExecutor] = []


def _log_executor() -> ThreadPoolExecutor:
    if not _log_executor_slot:
        _log_executor_slot.append(ThreadPoolExecutor(max_workers=2, thread_name_prefix="crawlrx_log_"))
    return _log_executor_slot[0]


def _shutdown_log_executor() -> None:
    if _log_executor_slot:
        _log_executor_slot.pop().shutdown(wait=False)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log all requests and track stats.
    Runs logging in background thread to avoid slowing responses.
    """

    async def dispatch(self, request: Request, call_next: object) -> object:
        start_time = time.time()
        method = request.method
        path = request.url.path

        # Extract params for logging (limited to POST bodies)
        params = {}
        if method == "POST" and request.headers.get("content-type") == "application/json":
            try:
                body = await request.body()
                if body:
                    import json
                    params = json.loads(body)
            except BROWSER_SCRAPE_EXCEPTIONS as _exc:
                logger.debug("Optional import json unavailable: %s", _exc)
                pass

        # Also include query params
        params.update(dict(request.query_params))

        error_msg = None
        success = True

        try:
            response = await call_next(request)
            status_code = response.status_code
            success = 200 <= status_code < 400
            if not success:
                error_msg = f"HTTP {status_code}"
            return response

        except BROWSER_SCRAPE_EXCEPTIONS as e:
            success = False
            error_msg = str(e)
            logger.error("Request failed: %s %s - %s", method, path, e)
            raise e

        finally:
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Log request in background thread (non-blocking)
            _log_executor().submit(
                log_request,
                endpoint=path,
                method=method,
                params=params,
                elapsed_ms=elapsed_ms,
                success=success,
                error=error_msg
            )

            # Record stats (thread-safe, fast enough for sync call)
            record_stats(path, elapsed_ms, success)


# -"--"- Lifespan (startup / shutdown) -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
@asynccontextmanager
def _verify_playwright_available() -> bool:
    """Best-effort check that playwright is importable; returns True on success."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as e:
        logger.warning(
            "[WARN] Playwright not installed (%s) -- browser endpoints will fail. "
            "Run: playwright install chromium", e,
        )
        return False
    logger.info("[START] Playwright available")
    return True


async def _verify_chromium_binary() -> bool:
    """Best-effort: run sync_playwright in a subprocess to locate the Chromium binary."""
    try:
        import subprocess
        result = await asyncio.to_thread(
            subprocess.run,
            ["python", "-c", "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()"],
            capture_output=True, text=True, timeout=10,
        )
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.warning("[START] Could not verify Chromium: %s", e)
        return False
    browser_path = result.stdout.strip()
    if browser_path and Path(browser_path).exists():
        logger.info("[START] Chromium found at %s", browser_path)
        return True
    logger.error("[START] Chromium binary not found - run: playwright install chromium")
    return False


async def lifespan(app: FastAPI) -> object:
    logger.info("[START] CrawlRX starting on port %d", _cfg.PORT)
    logger.info("[START] Endpoints: /crawl  /scrape  /search  /search/deep  /search/news/google  /search/local  /extract/contacts  /metadata  /batch  /queue  /scrape/linkedin  /scrape/instagram  /health  /stats  /cache/stats  /sessions  /docs")
    logger.info("[START] %s", await asyncio.to_thread(startup_summary))
    init_queue()
    init_learning_db()
    start_intelligence()
    if _verify_playwright_available():
        await _verify_chromium_binary()
    yield
    logger.info("CrawlRX shutting down")
    stop_intelligence()
    shutdown_queue()
    _shutdown_log_executor()


# -"--"- FastAPI App -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
app = FastAPI(
    title="CrawlRX",
    description=(
        "**Self-hosted web API** for crawling, scraping, search, LLM extraction and vision.\n\n"
        "Bring any OpenAI-compatible model (Ollama, LM Studio, vLLM or a cloud API); "
        "scraping and search need no model at all.\n\n"
        "---\n\n"
        "## Endpoints\n\n"
        "- **POST /crawl** - Full-page rendering: HTML, text, markdown, links, screenshot\n"
        "- **POST /scrape** - Targeted extraction with CSS selectors\n"
        "- **POST /search** - SERP scraping (Google, Bing, DuckDuckGo)\n"
        "- **POST /metadata** - Extract page metadata (title, OG tags, etc.)\n"
        "- **POST /batch** - Batch process multiple URLs concurrently\n"
        "- **POST /queue/submit** - Submit async job (non-blocking crawl/scrape)\n"
        "- **GET /queue/{job_id}** - Check job status and results\n"
        "- **GET /queue** - List recent jobs\n"
        "- **GET /queue/stats** - Queue statistics\n"
        "- **GET /cache/stats** - Cache statistics\n"
        "- **DELETE /cache** - Clear response cache\n"
        "- **GET /health** - Service status\n"
        "- **GET /stats** - Service statistics\n"
        "- **GET /sessions** - Browser session health (CDP connection status)\n"
        "- **POST /extract** - LLM structured extraction (needs a model)\n"
        "- **POST /vision/analyze** - Describe or question an image (needs a vision model)\n"
        "- **GET /model/status** - Which model endpoint is in use\n\n"
        "All browser operations use Playwright Chromium (headless or via CDP).\n"
        "Playwright sync API runs in a `ThreadPoolExecutor` - fully async-safe.\n\n"
        "## Queue System\n\n"
        "The `/queue` endpoints enable async job processing with automatic retries:\n"
        "- Submit a job, get a job_id, poll for results\n"
        "- Max 5 concurrent jobs with per-domain rate limiting (30 RPM)\n"
        "- Automatic retries (3 attempts) with exponential backoff\n"
        "- Job history retained for last 1000 jobs"
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# -- CORS: closed by default ---------------------------------------------------
# Browsers must not let arbitrary websites drive a local scraping/LLM API.
# Allow specific origins (e.g. a browser extension or your own UI) with
# CRAWLRX_CORS_ORIGINS="https://my.app,chrome-extension://<id>".
_CORS_ORIGINS = [o.strip() for o in os.getenv("CRAWLRX_CORS_ORIGINS", "").split(",") if o.strip()]
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials="*" not in _CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("[CORS] allowed origins: %s", ", ".join(_CORS_ORIGINS))

# -- Timeout Middleware (per-route caps; see TimeoutMiddleware.route_timeout) --
app.add_middleware(TimeoutMiddleware)
logger.info("[TIMEOUT] Global timeout middleware enabled (180s browser, 30s others)")

# -- SSRF guard: every URL in a body or query string must be public (runs after auth) --
app.add_middleware(UrlGuardMiddleware)

# -- API Key Auth (always wired - bypasses if CRAWLRX_API_KEYS not set) --
app.add_middleware(APIKeyMiddleware)
logger.info("[AUTH] API Key middleware active (local mode if no keys configured)")

# -- Rate Limiting Middleware -------------------------------------
app.add_middleware(RateLimitMiddleware)

# -"--"- Request Logging Middleware -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
app.add_middleware(RequestLoggingMiddleware)
logger.info("[LOG] Request logging enabled")

# -"--"- Register Routers -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
app.include_router(crawl_router)     # POST /crawl
app.include_router(scrape_router)    # POST /scrape
app.include_router(search_router)    # POST /search
app.include_router(metadata_router)  # POST /metadata
app.include_router(stats_router)     # GET /stats
app.include_router(learning_router)  # GET/POST /learn/*
app.include_router(intelligence_router)  # GET/POST /intel/*
app.include_router(batch_router)     # POST /batch
app.include_router(sessions_router)  # GET /sessions
app.include_router(queue_router)     # POST /queue/*
app.include_router(gmaps_router)       # POST /scrape/google-maps
app.include_router(linkedin_router)    # POST /scrape/linkedin
app.include_router(tiktok_router)      # POST /scrape/tiktok
app.include_router(instagram_router)   # POST /scrape/instagram
app.include_router(sitemap_router)     # POST /map
app.include_router(extract_router)     # POST /extract
app.include_router(vision_router)      # POST /vision/analyze, GET /model/status
app.include_router(js_extract_router)  # POST /scrape/extract-js
app.include_router(reddit_router)      # POST /scrape/reddit
app.include_router(youtube_router)     # POST /scrape/youtube
app.include_router(search_media_router) # POST /search/images, /search/news, /search/videos
app.include_router(search_deep_router)  # POST /search/deep, /search/news/google
app.include_router(search_fusion_router)  # POST /search/fusion
app.include_router(search_orchestrator_router)  # POST /search/orchestrate
app.include_router(scrape_diagnostics_router)  # GET /scrape/diagnostics, /scrape/status
app.include_router(contacts_router)    # POST /extract/contacts
app.include_router(local_search_router)  # POST /search/local
app.include_router(amazon_router)      # POST /scrape/amazon
app.include_router(screenshot_router)  # POST /screenshot
app.include_router(shopify_router)     # POST /scrape/shopify
app.include_router(twitter_router)     # POST /scrape/twitter/profile, /tweets, /search
app.include_router(yelp_router)        # POST /scrape/yelp/search, /business
app.include_router(facebook_router)    # POST /scrape/facebook/marketplace, POST /scrape/facebook/listing
app.include_router(monitor_router)     # POST/GET/DELETE /monitor
app.include_router(pwc_router)         # POST /pwc/value, /pwc/listings
app.include_router(craigslist_router)  # POST /scrape/craigslist/search, /scrape/craigslist/listing
app.include_router(zillow_router)      # POST /scrape/zillow/search, /scrape/zillow/property
app.include_router(walmart_router)     # POST /scrape/walmart/search, /scrape/walmart/product
app.include_router(google_search_router)  # POST /scrape/google/search, /scrape/google/news, /scrape/google/images
app.include_router(airbnb_router)      # POST /scrape/airbnb/search, /scrape/airbnb/listing
app.include_router(indeed_router)      # POST /scrape/indeed/search, /scrape/indeed/job
app.include_router(ebay_router)        # POST /scrape/ebay/search, /scrape/ebay/item
app.include_router(google_maps_router) # POST /scrape/google_maps/search, /scrape/google_maps/place
app.include_router(redfin_router)      # POST /scrape/redfin/search, /scrape/redfin/property
app.include_router(trulia_router)      # POST /scrape/trulia/search, /scrape/trulia/property
app.include_router(homedepot_router)   # POST /scrape/homedepot/search, /scrape/homedepot/product
app.include_router(autotrader_router)  # POST /scrape/autotrader/search, /scrape/autotrader/vehicle
app.include_router(apartments_router)  # POST /scrape/apartments/search, /scrape/apartments/listing
app.include_router(cars_com_router)    # POST /scrape/cars/search, /scrape/cars/vehicle
app.include_router(etsy_router)        # POST /scrape/etsy/search, /scrape/etsy/listing, /scrape/etsy/shop
app.include_router(tripadvisor_router) # POST /scrape/tripadvisor/search, /scrape/tripadvisor/reviews
app.include_router(bestbuy_router)     # POST /scrape/bestbuy/search, /scrape/bestbuy/product
app.include_router(costco_router)      # POST /scrape/costco/search, /scrape/costco/product
app.include_router(hotel_router)       # POST /scrape/hotel/search, /scrape/hotel/property
app.include_router(target_router)      # POST /scrape/target/search, /scrape/target/product
app.include_router(glassdoor_router)   # POST /scrape/glassdoor/company, /reviews, /salaries
app.include_router(realtor_router)     # POST /scrape/realtor/search, /scrape/realtor/property
app.include_router(stockx_router)      # POST /scrape/stockx/search, /scrape/stockx/product
app.include_router(expedia_router)     # POST /scrape/expedia/flights, /scrape/expedia/hotels
app.include_router(google_trends_router)  # POST /scrape/trends/interest, /related, /trending
app.include_router(semrush_router)     # POST /scrape/semrush/domain, /keywords
app.include_router(weather_router)     # POST /scrape/weather/current, /forecast, /historical
app.include_router(github_scraper_router)  # POST /scrape/github/repo, /user, /search
app.include_router(producthunt_router)  # POST /scrape/producthunt/search, /today, /product
app.include_router(angellist_router)   # POST /scrape/angellist/startup, /jobs
app.include_router(kayak_router)       # POST /scrape/kayak/flights, /hotels
app.include_router(pinterest_router)   # POST /scrape/pinterest/search, /profile, /board
app.include_router(trustpilot_router)  # POST /scrape/trustpilot/company, /reviews
app.include_router(ecommerce_search_router)  # POST /search/ecommerce
# linkedin_router already registered - extended with /scrape/linkedin/profile, /company, /jobs

# -- Promo/Deal Scraper Routers -------------------------------------------
if promo_scraper_router:
    app.include_router(promo_scraper_router)  # POST /scrape/promo
if honey_scraper_router:
    app.include_router(honey_scraper_router)  # POST /scrape/honey
if rakuten_scraper_router:
    app.include_router(rakuten_scraper_router)  # POST /scrape/rakuten
if slickdeals_scraper_router:
    app.include_router(slickdeals_scraper_router)  # POST /scrape/slickdeals
if dealnews_scraper_router:
    app.include_router(dealnews_scraper_router)  # POST /scrape/dealnews
if camelcamelcamel_scraper_router:
    app.include_router(camelcamelcamel_scraper_router)  # POST /scrape/camelcamelcamel
if keepa_scraper_router:
    app.include_router(keepa_scraper_router)  # POST /scrape/keepa
if oscommerce_scraper_router:
    app.include_router(oscommerce_scraper_router)  # POST /scrape/oscommerce
if salesforce_scraper_router:
    app.include_router(salesforce_scraper_router)  # POST /scrape/salesforce
if hybris_scraper_router:
    app.include_router(hybris_scraper_router)  # POST /scrape/hybris
if promo_search_router:
    app.include_router(promo_search_router)  # POST /search/promo

# New scraper routers
if knoji_scraper_router:
    app.include_router(knoji_scraper_router)  # POST /scrape/knoji
if coupon_aggregator_router:
    app.include_router(coupon_aggregator_router)  # POST /scrape/coupons
if capital_one_scraper_router:
    app.include_router(capital_one_scraper_router)  # POST /scrape/capital-one
if google_dork_scraper_router:
    app.include_router(google_dork_scraper_router)  # POST /scrape/google-dork
if price_history_router:
    app.include_router(price_history_router, prefix="/search")  # POST /search/price-history
if cross_retailer_price_router:
    app.include_router(cross_retailer_price_router)  # POST /search/price-compare
if wayback_scraper_router:
    app.include_router(wayback_scraper_router)  # POST /scrape/wayback


# -- Landing Page -------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing_page() -> object:
    """CrawlRX landing page."""
    return LANDING_HTML


# -"--"- Health Check -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
@app.get("/health", response_model=HealthResponse, summary="Health check", tags=["system"])
async def health() -> HealthResponse:
    """Returns service status, uptime, and endpoint list."""
    playwright_ok = True
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        playwright_ok = False

    return HealthResponse(
        status="ok",
        version=__version__,
        service="CrawlRX",
        playwright_available=playwright_ok,
        endpoints=["/crawl", "/scrape", "/search", "/search/local", "/extract/contacts", "/metadata", "/batch", "/queue", "/scrape/linkedin", "/scrape/instagram", "/scrape/amazon", "/screenshot", "/scrape/github", "/scrape/producthunt", "/scrape/angellist", "/cache/stats", "/health", "/stats", "/sessions"],
        uptime_seconds=round(time.time() - _START_TIME, 2),
    )


# -"--"- Cache Endpoints -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
@app.get("/cache/stats", response_model=CacheStatsResponse, summary="Cache statistics", tags=["cache"])
async def cache_stats() -> CacheStatsResponse:
    """Returns cache statistics: hits, misses, size, hit rate."""
    stats = get_cache().stats()
    return CacheStatsResponse(**stats)


@app.delete("/cache", summary="Clear cache", tags=["cache"])
async def cache_clear() -> object:
    """Clears all entries from the response cache."""
    get_cache().clear()
    return {"success": True, "message": "Cache cleared"}


# -- Platform Auto-Detection Scrape ------------------------------------------
@app.post("/scrape/platform", summary="Auto-detect platform and scrape products", tags=["scrape"])
def scrape_platform(request: ScrapeRequest) -> object:
    """
    Auto-detect the e-commerce platform of a URL and scrape products.

    Detects: shopify, woocommerce, wordpress, squarespace, wix,
             bigcommerce, magento, prestashop, godaddy

    Returns:
        {
            "success": bool,
            "platform": str,
            "products": list,
            "count": int,
            "url": str,
            "error": str | None
        }
    """
    url = request.url

    # Step 1: Detect platform
    detection = detect_platform(url, timeout=request.timeout // 1000 if request.timeout else 8)
    platform = detection.get("platform", "unknown")

    if platform == "unknown":
        return {
            "success": False,
            "platform": "unknown",
            "products": [],
            "count": 0,
            "url": url,
            "error": "Could not detect platform"
        }

    # Step 2: Route to correct scraper
    products = []
    error = None

    try:
        if platform == "shopify" and scrape_shopify_products:
            products = scrape_shopify_products(url, limit=50)
        elif platform == "woocommerce" and scrape_woocommerce_products:
            products = scrape_woocommerce_products(url, limit=50)
        elif platform == "wordpress" and scrape_wordpress_products:
            products = scrape_wordpress_products(url, limit=50)
        elif platform == "squarespace" and scrape_squarespace_products:
            products = scrape_squarespace_products(url, limit=50)
        elif platform == "wix" and scrape_wix_products:
            products = scrape_wix_products(url, limit=50)
        elif platform == "bigcommerce" and scrape_bigcommerce_products:
            products = scrape_bigcommerce_products(url, limit=50)
        elif platform == "magento" and scrape_magento_products:
            products = scrape_magento_products(url, limit=50)
        elif platform == "prestashop" and scrape_prestashop_products:
            products = scrape_prestashop_products(url, limit=50)
        elif platform == "godaddy" and scrape_godaddy_products:
            products = scrape_godaddy_products(url, limit=50)
        else:
            error = f"Platform '{platform}' detected but scraper not available"
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        error = str(e)
        logger.error("Platform scrape failed for %s (%s): %s", url, platform, e)
        return None  # explicit recovery: non-fatal: BROWSER_SCRAPE_EXCEPTIONS is recorded but does not abort the caller

    return {
        "success": error is None and len(products) > 0,
        "platform": platform,
        "products": products,
        "count": len(products),
        "url": url,
        "error": error
    }


# -"--"- Dashboard -"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"--"-
def _uptime_label(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours}h {minutes}m {secs}s"


def _service_row(
    name: str,
    status: str,
    detail: str,
    *,
    latency_ms: int | None = None,
    url: str = "",
    critical: bool = True,
    layer: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "latency_ms": latency_ms,
        "url": url,
        "critical": critical,
        "layer": layer,
    }


def _cached_dashboard_probe(key: str, ttl_s: int) -> dict[str, Any] | None:
    cached = _DASHBOARD_PROBE_CACHE.get(key)
    if not cached:
        return None
    ts, row = cached
    if time.time() - ts > ttl_s:
        return None
    return dict(row)


def _set_dashboard_probe_cache(key: str, row: dict[str, Any]) -> dict[str, Any]:
    _DASHBOARD_PROBE_CACHE[key] = (time.time(), dict(row))
    return row


def _searxng_warning_names(warnings: object, limit: int = 4) -> str:
    if not isinstance(warnings, list):
        return ""
    names: list[str] = []
    for item in warnings[:limit]:
        if isinstance(item, (list, tuple)) and item:
            names.append(str(item[0]))
        elif isinstance(item, dict):
            names.append(str(item.get("engine") or item.get("name") or "engine"))
        else:
            names.append(str(item).split(":", 1)[0])
    if len(warnings) > limit:
        names.append("+%d more" % (len(warnings) - limit))
    return ", ".join(name for name in names if name)


def _probe_http_service_sync(
    name: str,
    url: str,
    *,
    timeout_s: int = 4,
    critical: bool = True,
    layer: str = "",
) -> dict[str, Any]:
    import urllib.request

    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"X-Real-IP": "127.0.0.1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            status_code = getattr(response, "status", 200)
            response.read(4096)
        latency_ms = int((time.time() - started) * 1000)
        status = "ok" if 200 <= status_code < 400 else "warn"
        return _service_row(
            name,
            status,
            f"HTTP {status_code}",
            latency_ms=latency_ms,
            url=url,
            critical=critical,
            layer=layer,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            name,
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url=url,
            critical=critical,
            layer=layer,
        )


def _probe_json_service_sync(
    name: str,
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout_s: int = 4,
) -> dict[str, Any]:
    import json
    import urllib.request

    started = time.time()
    data = None
    headers = {"X-Real-IP": "127.0.0.1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw = response.read(65536)
            status_code = getattr(response, "status", 200)
        latency_ms = int((time.time() - started) * 1000)
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        detail = f"HTTP {status_code}"
        status = "ok" if 200 <= status_code < 400 else "warn"
        return _service_row(name, status, detail, latency_ms=latency_ms, url=url) | {
            "payload": payload,
        }
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            name,
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url=url,
        )


async def _probe_searxng() -> dict[str, Any]:
    cached = _cached_dashboard_probe(
        "searxng_upstreams", _DASHBOARD_SEARCH_PROBE_TTL_S,
    )
    if cached:
        return cached
    import urllib.parse

    query = urllib.parse.urlencode({
        "q": "openai",
        "format": "json",
        "engines": _cfg.SEARXNG_WEB_ENGINES,
    })
    url = _cfg.SEARXNG_URL.rstrip("/") + f"/search?{query}"
    row = await asyncio.to_thread(_probe_json_service_sync, "SearXNG Upstreams", url)
    row["critical"] = False
    row["layer"] = "external"
    payload = row.pop("payload", {}) if isinstance(row, dict) else {}
    warnings = payload.get("unresponsive_engines") if isinstance(payload, dict) else None
    result_count = len(payload.get("results", [])) if isinstance(payload, dict) else 0
    row["result_count"] = result_count
    if row.get("status") == "down":
        row["status"] = "warn"
        row["detail"] = f"External upstream probe unavailable: {row.get('detail', '')}"
    elif row.get("status") == "ok" and warnings:
        row["status"] = "warn"
        names = _searxng_warning_names(warnings)
        label = f"{len(warnings)} upstream warning(s)"
        if names:
            label += f": {names}"
        row["detail"] = (
            f"{label}, {result_count} result row(s)"
        )
    elif row.get("status") == "ok" and result_count == 0:
        row["status"] = "warn"
        row["detail"] = f"Hardened web core returned 0 result row(s): {_cfg.SEARXNG_WEB_ENGINES}"
    elif row.get("status") == "ok":
        row["detail"] = f"Hardened web core returned {result_count} result row(s)"
    return _set_dashboard_probe_cache("searxng_upstreams", row)


async def _probe_searxng_service() -> dict[str, Any]:
    url = _cfg.SEARXNG_URL.rstrip("/") + "/healthz"
    row = await asyncio.to_thread(
        _probe_http_service_sync,
        "SearXNG Service",
        url,
        critical=False,
        layer="infra",
    )
    if row.get("status") == "ok":
        row["detail"] = "HTTP app reachable"
    return row


async def _probe_search_router() -> dict[str, Any]:
    cached = _cached_dashboard_probe(
        "search_router", _DASHBOARD_SEARCH_PROBE_TTL_S,
    )
    if cached:
        return cached
    started = time.time()
    try:
        from .models import SearchRequest
        from .search import search as search_endpoint

        response = await search_endpoint(
            SearchRequest(
                query="crawlrx health",
                engine="searxng",
                num_results=3,
                timeout=15000,
                include_content=False,
            )
        )
        latency_ms = int((time.time() - started) * 1000)
        result_count = len(response.results)
        engine_used = response.engine
        fallback_used = not engine_used.startswith("searxng")
        if result_count and fallback_used:
            status = "warn"
            detail = f"Fallback returned {result_count} result(s) via {engine_used}"
        elif result_count:
            status = "ok"
            detail = f"Returned {result_count} result(s) via {engine_used}"
        else:
            status = "down"
            detail = response.error or "No search results returned"
        row = _service_row(
            "CrawlRX Search Router",
            status,
            detail,
            latency_ms=latency_ms,
            url="/search",
            layer="app",
        )
        row["engine_used"] = engine_used
        row["fallback_used"] = fallback_used
        row["result_count"] = result_count
        return _set_dashboard_probe_cache("search_router", row)
    except Exception as exc:  # dashboard probe must not take the dashboard down
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            "CrawlRX Search Router",
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url="/search",
            layer="app",
        )


async def _probe_search_orchestrator() -> dict[str, Any]:
    cached = _cached_dashboard_probe(
        "search_orchestrator", _DASHBOARD_SEARCH_PROBE_TTL_S,
    )
    if cached:
        return cached
    started = time.time()
    try:
        from .search_orchestrator import (
            SearchOrchestratorRequest,
            search_orchestrate,
        )

        response = await search_orchestrate(
            SearchOrchestratorRequest(
                query="openai",
                mode="web",
                count=3,
                timeout=5000,
                include_content=False,
            )
        )
        latency_ms = int((time.time() - started) * 1000)
        result_count = len(response.results)
        warn_count = len(response.warnings)
        if result_count:
            status = "ok"
            detail = (
                f"Intent {response.intent}, {result_count} result(s), "
                f"confidence {response.confidence}"
            )
            if warn_count:
                detail += f", {warn_count} fallback warning(s)"
        else:
            status = "down"
            detail = response.error or "No orchestrated search results"
        row = _service_row(
            "CrawlRX Search Brain",
            status,
            detail,
            latency_ms=latency_ms,
            url="/search/orchestrate",
            layer="app",
        )
        row["result_count"] = result_count
        row["engine_used"] = ", ".join(
            outcome.engine for outcome in response.routes if outcome.engine
        )
        row["route_count"] = len(response.routes)
        return _set_dashboard_probe_cache("search_orchestrator", row)
    except Exception as exc:  # dashboard probe must not take the dashboard down
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            "CrawlRX Search Brain",
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url="/search/orchestrate",
            layer="app",
        )


async def _probe_search_diagnostics() -> dict[str, Any]:
    cached = _cached_dashboard_probe(
        "search_diagnostics", _DASHBOARD_SEARCH_PROBE_TTL_S,
    )
    if cached:
        return cached
    started = time.time()
    try:
        from .search_orchestrator import run_search_diagnostics

        diagnostics = await run_search_diagnostics(force=False)
        latency_ms = int((time.time() - started) * 1000)
        summary = diagnostics.summary
        total = int(summary.get("total", 0))
        ok = int(summary.get("ok", 0))
        warn = int(summary.get("warn", 0))
        fail = int(summary.get("fail", 0))
        source_health = diagnostics.source_health or {}
        health_counts: dict[str, int] = {}
        for state in source_health.values():
            key = str(state.get("health_status", "unknown"))
            health_counts[key] = health_counts.get(key, 0) + 1
        status = "ok"
        if diagnostics.status == "fail" or health_counts.get("quarantined", 0):
            status = "down"
        elif diagnostics.status == "warn" or health_counts.get("degraded", 0):
            status = "warn"
        detail = "%d/%d golden checks green" % (ok, total)
        if fail:
            detail += ", %d failed" % fail
        if warn:
            detail += ", %d warning" % warn
        if health_counts:
            detail += ", %d source(s) healthy" % health_counts.get("healthy", 0)
        if health_counts.get("degraded", 0):
            detail += ", %d degraded" % health_counts["degraded"]
        if health_counts.get("quarantined", 0):
            detail += ", %d quarantined" % health_counts["quarantined"]
        if diagnostics.cache_hit:
            detail += " (cached)"
        row = _service_row(
            "Search Quality Proof",
            status,
            detail,
            latency_ms=latency_ms,
            url="/search/diagnostics",
            layer="app",
        )
        row["result_count"] = ok
        row["engine_used"] = ", ".join(diagnostics.source_groups.values())
        row["summary"] = summary
        row["source_health"] = source_health
        row["source_health_counts"] = health_counts
        return _set_dashboard_probe_cache("search_diagnostics", row)
    except Exception as exc:  # dashboard probe must not take the dashboard down
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            "Search Quality Proof",
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url="/search/diagnostics",
            layer="app",
        )


async def _probe_scrape_diagnostics() -> dict[str, Any]:
    cached = _cached_dashboard_probe(
        "scrape_diagnostics", _DASHBOARD_SEARCH_PROBE_TTL_S,
    )
    if cached:
        return cached
    started = time.time()
    try:
        from .scrape_diagnostics import run_scrape_diagnostics

        diagnostics = await run_scrape_diagnostics(force=False)
        latency_ms = int((time.time() - started) * 1000)
        summary = diagnostics.summary
        total = int(summary.get("total", 0))
        ok = int(summary.get("ok", 0))
        fail = int(summary.get("fail", 0))
        http_count = sum(1 for check in diagnostics.checks if check.rendered is False)
        browser_count = sum(1 for check in diagnostics.checks if check.rendered is True)
        from .scrape_site_health import get_scrape_site_health_summary
        site_health = get_scrape_site_health_summary()
        status = "ok" if diagnostics.status == "ok" else "down"
        detail = "%d/%d scrape checks green" % (ok, total)
        if fail:
            detail += ", %d failed" % fail
        detail += ", %d HTTP, %d browser" % (http_count, browser_count)
        if site_health.get("healthy", 0):
            detail += ", %d domain(s) healthy" % site_health["healthy"]
        if site_health.get("degraded", 0):
            detail += ", %d degraded" % site_health["degraded"]
        if site_health.get("quarantined", 0):
            detail += ", %d quarantined" % site_health["quarantined"]
        if diagnostics.cache_hit:
            detail += " (cached)"
        row = _service_row(
            "Scrape Quality Proof",
            status,
            detail,
            latency_ms=latency_ms,
            url="/scrape/diagnostics",
            layer="app",
        )
        row["result_count"] = ok
        row["summary"] = summary
        row["site_health_counts"] = site_health
        row["engine_used"] = ", ".join(
            sorted({str(check.source) for check in diagnostics.checks if check.source})
        )
        return _set_dashboard_probe_cache("scrape_diagnostics", row)
    except Exception as exc:  # dashboard probe must not take the dashboard down
        latency_ms = int((time.time() - started) * 1000)
        return _service_row(
            "Scrape Quality Proof",
            "down",
            f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
            url="/scrape/diagnostics",
            layer="app",
        )


async def _probe_model_backend() -> dict[str, Any]:
    """Dashboard row for the configured AI model endpoint (optional service)."""
    from . import model_backend

    started = time.time()
    status, detail = "ok", ""
    try:
        resolved = await asyncio.to_thread(model_backend.require_settings)
        detail = f"{resolved.model or resolved.vision_model} via {resolved.base_url}"
    except model_backend.ModelBackendUnavailable as exc:
        status, detail = "warn", str(exc).splitlines()[0] + " Run `crawlrx doctor`."
    return _service_row("AI Model", status, detail, latency_ms=int((time.time() - started) * 1000),
                        url="/model/status", critical=False, layer="model")


def _recent_request_rows(stats: object) -> list[dict[str, Any]]:
    rows = []
    for endpoint, count in sorted(
        stats.requests_by_endpoint.items(), key=lambda item: -item[1],
    )[:10]:
        rows.append({
            "endpoint": endpoint,
            "count": count,
            "avg_ms": f"{stats.avg_response_ms.get(endpoint, 0):.1f}ms",
        })
    return rows


def _circuit_rows() -> list[dict[str, Any]]:
    rows = []
    for domain, breaker in sorted(get_all_circuit_breakers().items()):
        data = breaker.as_dict()
        rows.append({
            "domain": domain,
            "state": str(data.get("state", "")).upper(),
            "failure_count": data.get("failure_count", 0),
            "last_failure": str(data.get("last_failure", "") or "-"),
        })
    return rows


@app.get("/dashboard/status", include_in_schema=False)
async def dashboard_status() -> object:
    """Return the JSON payload used by the CrawlRX operator dashboard."""
    stats = get_stats()
    cache_stats = get_cache().stats()
    uptime_seconds = round(time.time() - _START_TIME, 2)
    playwright_ok = _verify_playwright_available()
    probes = await asyncio.gather(
        _probe_searxng_service(),
        _probe_search_router(),
        _probe_search_orchestrator(),
        _probe_search_diagnostics(),
        _probe_scrape_diagnostics(),
        _probe_model_backend(),
        return_exceptions=True,
    )
    external_checks = await asyncio.gather(
        _probe_searxng(), return_exceptions=True,
    )
    services = [
        _service_row(
            "CrawlRX API",
            "ok" if playwright_ok else "warn",
            "Playwright ready" if playwright_ok else "Playwright import failed",
            latency_ms=0,
            url="/health",
            layer="app",
        ),
    ]
    for item in probes:
        if isinstance(item, BaseException):
            services.append(_service_row("Dependency", "down", type(item).__name__))
        else:
            services.append(item)
    external = []
    for item in external_checks:
        if isinstance(item, BaseException):
            external.append(_service_row("External Check", "warn", type(item).__name__))
        else:
            external.append(item)
    critical_services = [item for item in services if item.get("critical", True)]
    overall = "ok"
    if any(item["status"] in {"down", "fail"} for item in critical_services):
        overall = "down"
    elif any(
        item["status"] in {"warn", "stale", "unknown"} for item in critical_services
    ):
        overall = "warn"

    return {
        "summary": {
            "overall": overall,
            "success_rate_pct": round(stats.success_rate * 100, 1),
        },
        "uptime": {
            "seconds": uptime_seconds,
            "label": _uptime_label(uptime_seconds),
        },
        "services": services,
        "external_checks": external,
        "stats": {
            "total_requests": stats.total_requests,
            "success_rate": stats.success_rate,
            "errors_today": stats.errors_today,
        },
        "cache": cache_stats,
        "recent_requests": _recent_request_rows(stats),
        "circuit_breakers": _circuit_rows(),
    }


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> object:
    """Return the CrawlRX operator dashboard."""
    return OPERATOR_DASHBOARD_HTML


class _QuickSearchBody(BaseModel):
    query: str
    num_results: int = 10


class _TravelFlightsBody(BaseModel):
    origin: str
    destination: str
    depart_date: str = ''
    return_date: str = ''


class _TravelHotelsBody(BaseModel):
    location: str
    checkin: str = ''
    checkout: str = ''


class _TravelPromoBody(BaseModel):
    merchant: str


class _WordPressScrapeBody(BaseModel):
    url: str
    limit: int = 50


class _WooCommerceProductsBody(BaseModel):
    url: str
    limit: int = 50
    category: str = ''


_PRIVATE_HELPERS = (
    _QuickSearchBody,
    _TravelFlightsBody,
    _TravelHotelsBody,
    _TravelPromoBody,
    _WordPressScrapeBody,
    _WooCommerceProductsBody,
)


@app.post('/scrape/travel/flights', tags=['travel'])
async def scrape_flights(body: _TravelFlightsBody) -> object:
    """Search flight deals across Google Flights, Kayak."""
    from .travel_scraper import search_flight_deals
    return await search_flight_deals(body.origin, body.destination, body.depart_date, body.return_date)

@app.post('/scrape/travel/hotels', tags=['travel'])
async def scrape_hotels(body: _TravelHotelsBody) -> object:
    """Search hotel deals across Kayak.com, Hotels.com."""
    from .travel_scraper import search_hotel_deals
    return await search_hotel_deals(body.location, body.checkin, body.checkout)

@app.post('/scrape/travel/promo', tags=['travel'])
async def scrape_promo(body: _TravelPromoBody) -> object:
    """Scrape promo codes for a merchant from RetailMeNot, Coupons.com."""
    from .travel_scraper import search_promo_codes
    return await search_promo_codes(body.merchant)
@app.post('/search/quick', tags=['search'])
async def search_quick(body: _QuickSearchBody) -> object:
    """DDGS-only, 5s hard cap, no Playwright."""
    from .search import _do_search_ddgs
    try:
        result = await asyncio.wait_for(
            _do_search_ddgs(body.query, body.num_results or 10),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        result = {"success": False, "error": "timeout (5s)", "results": [], "total_results": 0, "engine": "ddgs"}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        result = {"success": False, "error": str(e), "results": [], "total_results": 0, "engine": "ddgs"}
    return result


@app.post('/search/google-shopping', tags=['search'])
async def search_google_shopping(body: _QuickSearchBody) -> object:
    """Google Shopping search via Playwright CDP."""
    from .ecommerce_search import _search_google_shopping_playwright, _google_shopping_to_product
    try:
        raw_results = await _search_google_shopping_playwright(body.query, body.num_results or 10)
        products = []
        for item in raw_results:
            product = _google_shopping_to_product(item)
            if product:
                products.append(product.model_dump())
        return {"success": True, "query": body.query, "results": products, "total": len(products), "source": "google_shopping"}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        return {"success": False, "error": str(e), "results": [], "total": 0}


@app.post('/scrape/wordpress', tags=['scrape'])
async def scrape_wordpress_endpoint(body: _WordPressScrapeBody) -> object:
    """Scrape posts from a WordPress site."""
    from .wordpress import scrape_wordpress_posts
    try:
        result = await scrape_wordpress_posts({"url": body.url, "count": body.limit})
        return {"success": result.get("success", False), "platform": "wordpress", "posts": result.get("posts", []), "count": result.get("total", 0), "url": body.url}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        return {"success": False, "error": str(e), "posts": [], "count": 0, "url": body.url}


@app.post('/scrape/woocommerce/products', tags=['scrape'])
async def scrape_woocommerce_products_endpoint(body: _WooCommerceProductsBody) -> object:
    """Scrape products from a WooCommerce store."""
    from .woocommerce import scrape_woocommerce_products as _scrape_wc
    from .utils import run_sync
    try:
        products = await run_sync(_scrape_wc, body.url, body.category or None, body.limit)
        return {"success": True, "platform": "woocommerce", "products": products, "count": len(products), "url": body.url}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        return {"success": False, "error": str(e), "products": [], "count": 0, "url": body.url}


class AmazonSearchRequest(BaseModel):
    query: str
    max_results: int = 10

@app.post("/scrape/amazon/search", summary="Search Amazon products", tags=["scrape"])
async def search_amazon(request: AmazonSearchRequest) -> object:
    """Search Amazon for products. Returns list of product results."""
    if not scrape_amazon_products:
        return {"success": False, "query": request.query, "results": [], "total": 0, "error": "Amazon scraper not available"}
    try:
        results = await scrape_amazon_products(request.query, request.max_results)
        return {"success": True, "query": request.query, "results": results, "total": len(results)}
    except BROWSER_SCRAPE_EXCEPTIONS as e:
        logger.error("Amazon search failed: %s", e)
        return {"success": False, "query": request.query, "results": [], "total": 0, "error": str(e)}


