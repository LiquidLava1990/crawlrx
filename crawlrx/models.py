"""
CrawlRX Pydantic Models -- Unified 
============================================================
All request/response models for CrawlRX API endpoints.
Single source of truth -- import only from here.
"""

from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# CRAWL
# ─────────────────────────────────────────────

class CrawlRequest(BaseModel):
    """Request model for POST /crawl"""
    url: str = Field(..., description="URL to crawl")
    wait_for: Optional[str] = Field(
        default="networkidle",
        description="Load state or CSS selector to wait for before extracting"
    )
    selector: Optional[str] = Field(
        default=None,
        description="CSS selector to wait for (when wait_for='selector')"
    )
    scroll: bool = Field(default=True, description="Scroll to bottom to trigger lazy-load")
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")
    extract_links: bool = Field(default=True, description="Return list of links found on page")
    extract_text: bool = Field(default=True, description="Return plain text")
    extract_markdown: bool = Field(default=True, description="Return markdown version")
    include_links: bool = Field(default=True, description="Alias for extract_links")
    include_images: bool = Field(default=False, description="Return image URLs")
    javascript: bool = Field(default=True, description="Execute JavaScript")
    screenshot: bool = Field(default=False, description="Return base64-encoded screenshot")
    user_agent: Optional[str] = Field(default=None, description="Custom User-Agent")
    headers: Optional[Dict[str, str]] = Field(default=None, description="Extra HTTP headers")
    session_id: Optional[str] = Field(default=None, description="Session ID for state persistence")
    agent: Optional[str] = Field(default=None, description="Named CDP browser from CRAWLRX_CDP_PORTS (omit for headless)")
    respect_robots: bool = Field(default=True, description="Honour robots.txt (set false only for sites you own or may scrape)")
    cache: bool = Field(default=True, description="Enable response caching")
    cache_ttl: int = Field(default=600, ge=60, le=3600, description="Cache TTL in seconds")
    structured_data: bool = Field(default=False, description="Extract JSON-LD and Open Graph structured data")
    stealth: bool = Field(default=True, description="Enable stealth mode (environment-check deterrence)")
    readable: bool = Field(default=False, description="Extract clean readable article content (no nav/ads/footer)")
    # ── Feature 1: Pre-extraction JavaScript ──────────────────────────────
    js_inject: Optional[str] = Field(default=None, description="JavaScript to execute after page load, before content extraction")
    js_inject_timeout: int = Field(default=5000, ge=500, le=30000, description="Timeout for js_inject in ms")
    # ── Feature 2: Advanced Wait Conditions ───────────────────────────────
    wait_for_text: Optional[str] = Field(default=None, description="Wait until this text appears on the page")
    wait_for_gone: Optional[str] = Field(default=None, description="Wait until this CSS selector disappears (e.g., loading spinner)")
    wait_for_timeout_extra: int = Field(default=0, ge=0, le=30000, description="Extra ms to wait after primary wait_for completes")
    same_domain_only: bool = Field(default=False, description="Only return links from same domain as URL")
    max_links: int = Field(default=500, ge=1, le=2000, description="Maximum links to return")
    max_images: int = Field(default=200, ge=1, le=1000, description="Maximum images to return")
    # ── Engine Selection ──────────────────────────────────────────────────
    engine: str = Field(default="default", description="Extraction engine: 'default' (Playwright+BS4) or 'crawl4ai' (LLM-optimized markdown)")


class CrawlResponse(BaseModel):
    """Response model for POST /crawl"""
    success: bool = Field(..., description="Whether crawl succeeded")
    url: str = Field(..., description="Crawled URL")
    title: Optional[str] = Field(default=None, description="Page title")
    markdown: Optional[str] = Field(default=None, description="Markdown content")
    html: Optional[str] = Field(default=None, description="Raw HTML")
    text: Optional[str] = Field(default=None, description="Plain text")
    links: List[str] = Field(default_factory=list, description="Extracted links")
    images: List[str] = Field(default_factory=list, description="Extracted image URLs")
    screenshot_b64: Optional[str] = Field(default=None, description="Base64 screenshot PNG")
    status_code: Optional[int] = Field(default=None, description="HTTP status code")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    error_type: Optional[str] = Field(default=None, description="Error classification: timeout | blocked | captcha_required | circuit_open | parse_error | network_error | unknown")
    captcha: Optional[Dict[str, Any]] = Field(default=None, description="Set when a captcha needs a human: url, kind, handoff, action")
    cache_hit: bool = Field(default=False, description="Whether response was served from cache")
    final_url: Optional[str] = Field(default=None, description="Final URL after redirects")
    redirected: bool = Field(default=False, description="Whether any redirects occurred")
    structured_data: Optional[Dict[str, Any]] = Field(default=None, description="Extracted JSON-LD and Open Graph data")
    readable_content: Optional[Dict[str, Any]] = Field(default=None, description="Clean readable content extraction result")
    # ── Feature 1: js_inject return value ─────────────────────────────────
    js_result: Optional[Any] = Field(default=None, description="Return value from js_inject (if dict/list)")


# ─────────────────────────────────────────────
# SCRAPE
# ─────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    """Request model for POST /scrape"""
    url: str = Field(..., description="URL to scrape")
    selector: Optional[str] = Field(
        default=None,
        description="CSS selector to extract specific element(s). If None, extracts whole page."
    )
    selectors: Optional[Dict[str, str]] = Field(
        default=None,
        description="Named CSS selectors dict (key -> selector). Alternative to single selector."
    )
    extract: Optional[str] = Field(
        default="all",
        description="What to extract: 'all' | 'text' | 'html' | 'markdown' | 'links' | 'images'"
    )
    wait_for: Optional[str] = Field(default=None, description="CSS selector or load state to wait for")
    scroll: bool = Field(default=False, description="Scroll to bottom before extracting")
    scroll_to_bottom: bool = Field(default=False, description="Alias for scroll")
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")
    javascript: bool = Field(default=True, description="Execute JavaScript")
    user_agent: Optional[str] = Field(default=None, description="Custom User-Agent")
    headers: Optional[Dict[str, str]] = Field(default=None, description="Extra HTTP headers")
    click_selector: Optional[str] = Field(default=None, description="CSS selector to click before extracting")
    session_id: Optional[str] = Field(default=None, description="Session ID")
    agent: Optional[str] = Field(default=None, description="Agent browser to use")
    respect_robots: bool = Field(default=True, description="Honour robots.txt (set false only for sites you own or may scrape)")
    cache: bool = Field(default=True, description="Enable response caching")
    cache_ttl: int = Field(default=600, ge=60, le=3600, description="Cache TTL in seconds")
    structured_data: bool = Field(default=False, description="Extract JSON-LD and Open Graph structured data")
    stealth: bool = Field(default=True, description="Enable stealth mode (environment-check deterrence)")
    # ── Feature 1: Pre-extraction JavaScript ──────────────────────────────
    js_inject: Optional[str] = Field(default=None, description="JavaScript to execute after page load, before content extraction")
    js_inject_timeout: int = Field(default=5000, ge=500, le=30000, description="Timeout for js_inject in ms")
    # ── Feature 2: Advanced Wait Conditions ───────────────────────────────
    wait_for_text: Optional[str] = Field(default=None, description="Wait until this text appears on the page")
    wait_for_gone: Optional[str] = Field(default=None, description="Wait until this CSS selector disappears (e.g., loading spinner)")
    wait_for_timeout_extra: int = Field(default=0, ge=0, le=30000, description="Extra ms to wait after primary wait_for completes")
    # ── Feature 3: Element Screenshot ─────────────────────────────────────
    screenshot_selector: Optional[str] = Field(default=None, description="CSS selector of element to screenshot (crops to that element)")
    # ── Feature 4: Shadow DOM Support ─────────────────────────────────────
    shadow_selector: Optional[str] = Field(default=None, description="Extract text from Shadow DOM -- pass host element CSS selector")


class ScrapeItem(BaseModel):
    """Single extracted element result"""
    selector: Optional[str] = None
    text: Optional[str] = None
    html: Optional[str] = None
    markdown: Optional[str] = None
    links: Optional[List[str]] = None
    images: Optional[List[str]] = None
    count: int = 0


# Alias for backwards compatibility
ScrapeResult = ScrapeItem


class ScrapeResponse(BaseModel):
    """Response model for POST /scrape"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Scraped URL")
    title: Optional[str] = Field(default=None, description="Page title")
    results: List[ScrapeItem] = Field(default_factory=list, description="Extracted elements")
    data: Dict[str, Any] = Field(default_factory=dict, description="Named selector results (if selectors dict used)")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    error_type: Optional[str] = Field(default=None, description="Error classification: timeout | blocked | captcha_required | circuit_open | parse_error | network_error | unknown")
    captcha: Optional[Dict[str, Any]] = Field(default=None, description="Set when a captcha needs a human: url, kind, handoff, action")
    cache_hit: bool = Field(default=False, description="Whether response was served from cache")
    status_code: Optional[int] = Field(default=None, description="HTTP status code if available")
    final_url: Optional[str] = Field(default=None, description="Final URL after redirects if available")
    source: Optional[str] = Field(default=None, description="Fetch/render source used for the scrape")
    rendered: bool = Field(default=True, description="Whether a browser-rendered DOM was used")
    strategy: Optional[str] = Field(default=None, description="Selected scrape strategy: http_first | browser_first | http_then_browser")
    provenance: Dict[str, Any] = Field(default_factory=dict, description="Execution proof metadata for the scrape")
    structured_data: Optional[Dict[str, Any]] = Field(default=None, description="Extracted JSON-LD and Open Graph data")
    # ── Feature 1: js_inject return value ─────────────────────────────────
    js_result: Optional[Any] = Field(default=None, description="Return value from js_inject (if dict/list)")
    # ── Feature 3: Element screenshot ─────────────────────────────────────
    screenshot_b64: Optional[str] = Field(default=None, description="Base64 PNG of screenshotted element")
    # ── Feature 4: Shadow DOM content ─────────────────────────────────────
    shadow_content: Optional[str] = Field(default=None, description="Text extracted from Shadow DOM of shadow_selector element")


# ─────────────────────────────────────────────
# JS EXTRACT (for CrawlRX price/data extraction)
# ─────────────────────────────────────────────

class JSExtractRequest(BaseModel):
    """Request model for POST /scrape/extract-js"""
    url: str = Field(..., description="URL to scrape")
    wait_for: Optional[str] = Field(default=None, description="domcontentloaded (default), networkidle, or selector string")
    timeout: int = Field(default=15000, ge=1000, le=120000, description="Timeout in milliseconds")
    js: str = Field(..., description="JavaScript expression to evaluate -- must return JSON-serializable value")
    scroll_to_bottom: bool = Field(default=False, description="Scroll to bottom before extracting")
    stealth: bool = Field(default=True, description="Enable stealth mode")
    cache: bool = Field(default=True, description="Enable response caching")
    cache_ttl: Optional[int] = Field(default=None, ge=60, le=3600, description="Cache TTL in seconds")


class JSExtractResponse(BaseModel):
    """Response model for POST /scrape/extract-js"""
    success: bool = Field(..., description="Whether extraction succeeded")
    url: str = Field(..., description="Scraped URL")
    result: Optional[Any] = Field(default=None, description="Whatever the JS returned")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    cache_hit: bool = Field(default=False, description="Whether response was served from cache")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────

class SearchRequest(BaseModel):
    """Request model for POST /search"""
    query: str = Field(..., min_length=1, description="Search query string")
    engine: str = Field(default="searxng", description="Search engine: 'searxng' | 'bing' | 'duckduckgo' | 'google'. CrawlRX defaults to its hardened local SearXNG core.")
    num_results: int = Field(default=10, ge=1, le=50, description="Number of results to return")
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")
    safe_search: bool = Field(default=True, description="Enable safe search filtering")
    language: Optional[str] = Field(default=None, description="Language code (e.g. 'en', 'fr')")
    country: Optional[str] = Field(default=None, description="Country code (e.g. 'us', 'gb')")
    include_content: bool = Field(default=False, description="Fetch and include page content for each result")
    session_id: Optional[str] = Field(default=None, description="Session ID")
    agent: Optional[str] = Field(default=None, description="Agent browser to use")


class SearchResult(BaseModel):
    """Individual search result"""
    title: str = Field(..., description="Result title")
    url: str = Field(..., description="Result URL")
    snippet: Optional[str] = Field(default=None, description="Result snippet/description")
    content: Optional[str] = Field(default=None, description="Full page content (if include_content=True)")
    position: int = Field(default=0, description="Position in results (1-indexed)")


# ─────────────────────────────────────────────
# DEEP SEARCH
# ─────────────────────────────────────────────

class DeepSearchRequest(BaseModel):
    """Request model for POST /search/deep"""
    query: str = Field(..., min_length=1, description="Search query string")
    site_filter: Optional[str] = Field(default=None, description="Restrict to site (e.g. 'github.com'). Adds site: operator.")
    file_type: Optional[str] = Field(default=None, description="File type filter (pdf, doc, csv, xls). Adds filetype: operator.")
    exact_phrase: Optional[str] = Field(default=None, description="Exact phrase to require in results. Adds \"phrase\" quoting.")
    exclude_terms: Optional[List[str]] = Field(default=None, description="Terms to exclude from results. Adds -term operators.")
    inurl: Optional[str] = Field(default=None, description="Keyword that must appear in URL. Adds inurl: operator.")
    intitle: Optional[str] = Field(default=None, description="Keyword that must appear in page title. Adds intitle: operator.")
    num_results: int = Field(default=10, ge=1, le=50, description="Number of results to return")
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")


class DeepSearchResult(BaseModel):
    """Individual deep search result with scoring"""
    title: str = Field(..., description="Result title")
    url: str = Field(..., description="Result URL")
    snippet: Optional[str] = Field(default=None, description="Result snippet/description")
    date: Optional[str] = Field(default=None, description="Publication or indexed date (when available)")
    source: str = Field(default="", description="Source engine that returned this result (google/ddg)")
    score: float = Field(default=0.0, description="Composite relevance score (0.0-1.0)")


class DeepSearchResponse(BaseModel):
    """Response model for POST /search/deep"""
    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original query")
    dork_query: str = Field(default="", description="Built dork query string that was executed")
    results: List[DeepSearchResult] = Field(default_factory=list, description="Merged, scored results")
    total_found: int = Field(default=0, description="Total results found before trimming to num_results")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# NEWS DEEP (GOOGLE NEWS)
# ─────────────────────────────────────────────

class NewsDeepRequest(BaseModel):
    """Request model for POST /search/news/google"""
    query: str = Field(..., min_length=1, description="Search query string")
    num_results: int = Field(default=10, ge=1, le=50, description="Number of results to return")
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")


class NewsDeepResult(BaseModel):
    """Single Google News result"""
    headline: str = Field(default="", description="News article headline")
    source: str = Field(default="", description="Publisher/source name")
    date: str = Field(default="", description="Published date string")
    url: str = Field(default="", description="Article URL")
    snippet: str = Field(default="", description="Article snippet")
    score: float = Field(default=0.0, description="Relevance + freshness score (0.0-1.0)")


class NewsDeepResponse(BaseModel):
    """Response model for POST /search/news/google"""
    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original query")
    results: List[NewsDeepResult] = Field(default_factory=list, description="News results")
    total_found: int = Field(default=0, description="Total results found")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class SearchResponse(BaseModel):
    """Response model for POST /search"""
    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original query")
    engine: str = Field(default="google", description="Engine used: 'searxng', 'google', 'bing', or 'duckduckgo'")
    results: List[SearchResult] = Field(default_factory=list, description="Search results")
    total_results: Optional[str] = Field(default=None, description="Estimated total results string")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# METADATA
# ─────────────────────────────────────────────

class MetadataRequest(BaseModel):
    """Request model for POST /metadata"""
    url: str = Field(..., description="URL to extract metadata from")
    agent: Optional[str] = Field(default=None, description="Named CDP browser from CRAWLRX_CDP_PORTS (omit for headless)")
    timeout: int = Field(default=15000, ge=1000, le=60000, description="Timeout in milliseconds")


class MetadataResponse(BaseModel):
    """Response model for POST /metadata"""
    success: bool = Field(..., description="Whether metadata extraction succeeded")
    url: str = Field(..., description="Target URL")
    title: Optional[str] = Field(default=None, description="Page title")
    description: Optional[str] = Field(default=None, description="Meta description")
    og_title: Optional[str] = Field(default=None, description="OpenGraph title")
    og_description: Optional[str] = Field(default=None, description="OpenGraph description")
    og_image: Optional[str] = Field(default=None, description="OpenGraph image URL")
    canonical: Optional[str] = Field(default=None, description="Canonical URL")
    author: Optional[str] = Field(default=None, description="Meta author")
    keywords: Optional[str] = Field(default=None, description="Meta keywords")
    favicon: Optional[str] = Field(default=None, description="Favicon URL")
    word_count: Optional[int] = Field(default=None, description="Word count of page content")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# ERROR DETAIL
# ─────────────────────────────────────────────

class ErrorDetail(BaseModel):
    """Structured error detail for failed crawl/scrape operations."""
    error: str = Field(..., description="Human-readable error message")
    error_type: str = Field(
        ...,
        description=(
            "Error classification: "
            "timeout | blocked | circuit_open | parse_error | network_error | unknown"
        )
    )
    url: str = Field(..., description="URL that failed")
    elapsed_ms: int = Field(..., description="Time elapsed before failure in milliseconds")
    retries: int = Field(default=0, description="Number of retry attempts made")


# ─────────────────────────────────────────────
# SYSTEM
# ─────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Response model for GET /health"""
    status: str = Field(default="ok", description="Service status")
    version: str = Field(default="1.0.0", description="API version")
    service: str = Field(default="CrawlRX", description="Service name")
    playwright_available: bool = Field(default=True, description="Whether Playwright is installed")
    endpoints: List[str] = Field(
        default_factory=lambda: ["/crawl", "/scrape", "/search", "/health"],
        description="Available endpoints"
    )
    uptime_seconds: Optional[float] = Field(default=None, description="Service uptime")


class ErrorResponse(BaseModel):
    """Generic error response"""
    success: bool = Field(default=False)
    error: str = Field(..., description="Error message")
    error_type: Optional[str] = Field(default=None)
    details: Optional[Dict[str, Any]] = Field(default=None)


# ─────────────────────────────────────────────
# BATCH
# ─────────────────────────────────────────────

class BatchRequest(BaseModel):
    """Request model for POST /batch"""
    urls: List[str] = Field(..., min_length=1, max_length=20, description="URLs to process")
    mode: Literal["crawl", "scrape"] = Field(default="crawl", description="Processing mode")
    # crawl options
    extract_text: bool = Field(default=True, description="Extract plain text")
    extract_markdown: bool = Field(default=True, description="Extract markdown")
    extract_links: bool = Field(default=False, description="Extract links")
    screenshot: bool = Field(default=False, description="Capture screenshot")
    # scrape options
    selector: Optional[str] = Field(default=None, description="CSS selector for scrape mode")
    extract: Optional[str] = Field(default="text", description="What to extract in scrape mode")
    # shared
    timeout: int = Field(default=30000, ge=1000, le=120000, description="Timeout in milliseconds")
    agent: Optional[str] = Field(default=None, description="Agent browser to use")
    max_concurrency: int = Field(default=3, ge=1, le=10, description="Max concurrent requests")
    # webhook options
    webhook_url: Optional[str] = Field(default=None, description="POST results to this URL when batch completes")
    webhook_secret: Optional[str] = Field(default=None, description="Optional HMAC-SHA256 signing secret")


class BatchResult(BaseModel):
    """Individual result for a single URL in batch processing"""
    url: str = Field(..., description="URL that was processed")
    success: bool = Field(..., description="Whether processing succeeded")
    title: Optional[str] = Field(default=None, description="Page title")
    text: Optional[str] = Field(default=None, description="Plain text content")
    markdown: Optional[str] = Field(default=None, description="Markdown content")
    links: List[str] = Field(default_factory=list, description="Extracted links")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


class BatchResponse(BaseModel):
    """Response model for POST /batch"""
    success: bool = Field(..., description="Whether all URLs succeeded")
    total: int = Field(..., description="Total URLs processed")
    succeeded: int = Field(..., description="Number of successful requests")
    failed: int = Field(..., description="Number of failed requests")
    results: List[BatchResult] = Field(default_factory=list, description="Individual results")
    elapsed_ms: int = Field(default=0, description="Total time taken in milliseconds")
    webhook_url: Optional[str] = Field(default=None, description="Webhook URL that was notified on completion")
    webhook_delivered: Optional[bool] = Field(default=None, description="Whether webhook delivery succeeded")


class CacheStatsResponse(BaseModel):
    """Response model for GET /cache/stats"""
    hits: int = Field(..., description="Number of cache hits")
    misses: int = Field(..., description="Number of cache misses")
    size: int = Field(..., description="Current number of cached entries")
    max_size: int = Field(..., description="Maximum cache size")
    hit_rate_percent: float = Field(..., description="Cache hit rate percentage")
    ttl_seconds: int = Field(..., description="Default TTL for cache entries")


# ─────────────────────────────────────────────
# LINKEDIN
# ─────────────────────────────────────────────

class LinkedInRequest(BaseModel):
    """Request model for POST /scrape/linkedin"""
    url: str = Field(..., description="Full LinkedIn profile URL e.g. https://www.linkedin.com/in/username")
    timeout: int = Field(default=30000, ge=5000, le=120000, description="Timeout in milliseconds")


class LinkedInResponse(BaseModel):
    """Response model for POST /scrape/linkedin"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Profile URL")
    name: Optional[str] = Field(default=None, description="Full name")
    headline: Optional[str] = Field(default=None, description="Professional headline")
    location: Optional[str] = Field(default=None, description="Location")
    about: Optional[str] = Field(default=None, description="About/summary section")
    experience: List[str] = Field(default_factory=list, description="Experience entries")
    education: List[str] = Field(default_factory=list, description="Education entries")
    skills: List[str] = Field(default_factory=list, description="Skills list")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# GOOGLE MAPS
# ─────────────────────────────────────────────

class GoogleMapsRequest(BaseModel):
    """Request model for POST /scrape/google-maps"""
    query: str = Field(..., description="Search query, e.g. 'coffee shops in Louisville KY'")
    max_results: int = Field(default=10, ge=1, le=50, description="Maximum results to return")
    timeout: int = Field(default=30000, ge=5000, le=120000, description="Timeout in milliseconds")


class GoogleMapsBusiness(BaseModel):
    """Single Google Maps business listing"""
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    rating: Optional[str] = None
    reviews: Optional[str] = None
    website: Optional[str] = None
    category: Optional[str] = None
    hours: Optional[str] = None


class GoogleMapsResponse(BaseModel):
    """Response model for POST /scrape/google-maps"""
    success: bool = Field(..., description="Whether scrape succeeded")
    query: str = Field(..., description="Original search query")
    results: List[GoogleMapsBusiness] = Field(default_factory=list, description="Business listings")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# CONTACTS EXTRACTION
# ─────────────────────────────────────────────

class ContactsRequest(BaseModel):
    """Request model for POST /extract/contacts"""
    url: str = Field(..., description="URL to extract contacts from")
    session_id: Optional[str] = Field(default=None, description="Session ID for state persistence")


class ContactsResponse(BaseModel):
    """Response model for POST /extract/contacts"""
    success: bool = Field(..., description="Whether extraction succeeded")
    url: str = Field(..., description="Target URL")
    emails: List[str] = Field(default_factory=list, description="Extracted email addresses")
    phones: List[str] = Field(default_factory=list, description="Extracted phone numbers")
    social_links: Dict[str, str] = Field(default_factory=dict, description="Extracted social media links")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# LOCAL SEARCH
# ─────────────────────────────────────────────

class LocalSearchRequest(BaseModel):
    """Request model for POST /search/local"""
    query: str = Field(..., description="Search query, e.g. 'pizza'")
    zip_code: str = Field(..., description="ZIP code to search near, e.g. '40202'")
    limit: int = Field(default=10, ge=1, le=20, description="Maximum results to return")
    session_id: Optional[str] = Field(default=None, description="Session ID for state persistence")


class LocalSearchResult(BaseModel):
    """Single local business search result"""
    name: str = Field(..., description="Business name")
    address: Optional[str] = Field(default=None, description="Business address")
    rating: Optional[float] = Field(default=None, description="Rating (e.g. 4.5)")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    phone: Optional[str] = Field(default=None, description="Phone number")
    website: Optional[str] = Field(default=None, description="Website URL")
    category: Optional[str] = Field(default=None, description="Business category")
    hours: Optional[str] = Field(default=None, description="Business hours")


class LocalSearchResponse(BaseModel):
    """Response model for POST /search/local"""
    success: bool = Field(..., description="Whether search succeeded")
    query: str = Field(..., description="Original search query")
    zip_code: str = Field(..., description="ZIP code searched")
    results: List[LocalSearchResult] = Field(default_factory=list, description="Business listings")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# AMAZON
# ─────────────────────────────────────────────

class AmazonScrapeRequest(BaseModel):
    """Request model for POST /scrape/amazon"""
    url: str = Field(..., description="Amazon product URL")
    session_id: Optional[str] = Field(default=None, description="Session ID")


class AmazonScrapeResponse(BaseModel):
    """Response model for POST /scrape/amazon"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Product URL")
    asin: Optional[str] = Field(default=None, description="Amazon ASIN")
    title: Optional[str] = Field(default=None, description="Product title")
    price: Optional[str] = Field(default=None, description="Product price")
    rating: Optional[float] = Field(default=None, description="Star rating (1-5)")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    availability: Optional[str] = Field(default=None, description="Stock availability text")
    brand: Optional[str] = Field(default=None, description="Brand name")
    image_url: Optional[str] = Field(default=None, description="Primary product image URL (deprecated, use images[0])")
    images: List[str] = Field(default_factory=list, description="All product image URLs")
    description: Optional[str] = Field(default=None, description="Product description text")
    features: List[str] = Field(default_factory=list, description="Bullet-point feature list")
    variants: List[Dict[str, Any]] = Field(default_factory=list, description="Product variants (size/color)")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# SCREENSHOT
# ─────────────────────────────────────────────

class ScreenshotRequest(BaseModel):
    """Request model for POST /screenshot"""
    url: str = Field(..., description="URL to screenshot")
    full_page: bool = Field(default=True, description="Capture full page or viewport")
    width: int = Field(default=1280, ge=320, le=3840, description="Viewport width")
    height: int = Field(default=800, ge=240, le=2160, description="Viewport height")
    session_id: Optional[str] = Field(default=None, description="Session ID")


class ScreenshotResponse(BaseModel):
    """Response model for POST /screenshot"""
    success: bool = Field(..., description="Whether screenshot succeeded")
    url: str = Field(..., description="Target URL")
    screenshot_base64: Optional[str] = Field(default=None, description="Base64-encoded PNG")
    width: int = Field(default=1280, description="Viewport width used")
    height: int = Field(default=800, description="Viewport height used")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# TIKTOK
# ─────────────────────────────────────────────

class TikTokVideo(BaseModel):
    """Single TikTok video data"""
    video_id: Optional[str] = Field(default=None, description="TikTok video ID")
    url: Optional[str] = Field(default=None, description="Video URL")
    caption: Optional[str] = Field(default=None, description="Video caption/description")
    views: Optional[str] = Field(default=None, description="View count")
    likes: Optional[str] = Field(default=None, description="Like count")
    comments: Optional[str] = Field(default=None, description="Comment count")
    shares: Optional[str] = Field(default=None, description="Share count")
    post_date: Optional[str] = Field(default=None, description="Post date")
    duration: Optional[str] = Field(default=None, description="Video duration")
    hashtags: List[str] = Field(default_factory=list, description="Hashtags in caption")


class TikTokMusic(BaseModel):
    """Music/sound info for TikTok video"""
    title: Optional[str] = Field(default=None, description="Music title")
    artist: Optional[str] = Field(default=None, description="Music artist")
    url: Optional[str] = Field(default=None, description="Music URL")
    is_original: bool = Field(default=False, description="Whether this is original sound")


class TikTokAuthor(BaseModel):
    """Author info for TikTok video"""
    username: str = Field(..., description="Username")
    display_name: Optional[str] = Field(default=None, description="Display name")
    avatar_url: Optional[str] = Field(default=None, description="Avatar URL")


class TikTokRequest(BaseModel):
    """Request model for POST /scrape/tiktok"""
    username: str = Field(..., description="TikTok username (no @), e.g. 'carbon_6_vi'")
    include_videos: bool = Field(default=True, description="Include video list in response")
    max_videos: int = Field(default=10, ge=1, le=50, description="Maximum videos to extract")
    timeout: int = Field(default=30000, ge=5000, le=120000, description="Timeout in milliseconds")


class TikTokResponse(BaseModel):
    """Response model for POST /scrape/tiktok (legacy)"""
    success: bool = Field(..., description="Whether scrape succeeded")
    username: str = Field(..., description="TikTok username")
    display_name: Optional[str] = Field(default=None, description="Display name")
    bio: Optional[str] = Field(default=None, description="Profile bio")
    followers: Optional[str] = Field(default=None, description="Followers count")
    following: Optional[str] = Field(default=None, description="Following count")
    likes: Optional[str] = Field(default=None, description="Total likes")
    video_count: Optional[str] = Field(default=None, description="Video count")
    videos: List[TikTokVideo] = Field(default_factory=list, description="List of videos")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# New TikTok API Models

class TikTokProfileRequest(BaseModel):
    """Request model for POST /scrape/tiktok/profile"""
    username: str = Field(..., description="TikTok username (no @), e.g. 'carbon_six'")
    timeout: int = Field(default=30000, ge=10000, le=60000, description="Timeout in milliseconds")


class TikTokProfile(BaseModel):
    """TikTok profile data"""
    username: str = Field(..., description="TikTok username")
    display_name: Optional[str] = Field(default=None, description="Display name")
    bio: Optional[str] = Field(default=None, description="Profile bio")
    followers: Optional[str] = Field(default=None, description="Followers count")
    following: Optional[str] = Field(default=None, description="Following count")
    total_likes: Optional[str] = Field(default=None, description="Total likes on all videos")
    video_count: Optional[str] = Field(default=None, description="Number of videos")
    verified: bool = Field(default=False, description="Whether account is verified")
    profile_url: str = Field(..., description="Full profile URL")
    avatar_url: Optional[str] = Field(default=None, description="Avatar/profile picture URL")


class TikTokProfileResponse(BaseModel):
    """Response model for POST /scrape/tiktok/profile"""
    success: bool = Field(..., description="Whether scrape succeeded")
    profile: Optional[TikTokProfile] = Field(default=None, description="Profile data")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class TikTokVideosRequest(BaseModel):
    """Request model for POST /scrape/tiktok/videos"""
    username: str = Field(..., description="TikTok username (no @)")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum videos to return")
    timeout: int = Field(default=30000, ge=10000, le=60000, description="Timeout in milliseconds")


class TikTokVideosResponse(BaseModel):
    """Response model for POST /scrape/tiktok/videos"""
    success: bool = Field(..., description="Whether scrape succeeded")
    username: str = Field(..., description="TikTok username")
    videos: List[TikTokVideo] = Field(default_factory=list, description="List of videos")
    count: int = Field(default=0, description="Number of videos returned")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class TikTokVideoRequest(BaseModel):
    """Request model for POST /scrape/tiktok/video"""
    url: Optional[str] = Field(default=None, description="Full TikTok video URL")
    video_id: Optional[str] = Field(default=None, description="TikTok video ID (e.g., '1234567890')")
    timeout: int = Field(default=30000, ge=10000, le=60000, description="Timeout in milliseconds")


class TikTokVideoDetail(TikTokVideo):
    """Detailed TikTok video info"""
    music: Optional[TikTokMusic] = Field(default=None, description="Music/sound info")
    author: Optional[TikTokAuthor] = Field(default=None, description="Video author info")
    full_caption: Optional[str] = Field(default=None, description="Full caption text")
    thumbnail_url: Optional[str] = Field(default=None, description="Video thumbnail URL")


class TikTokVideoResponse(BaseModel):
    """Response model for POST /scrape/tiktok/video"""
    success: bool = Field(..., description="Whether scrape succeeded")
    video: Optional[TikTokVideoDetail] = Field(default=None, description="Video details")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class TikTokTrendingRequest(BaseModel):
    """Request model for POST /scrape/tiktok/trending"""
    limit: int = Field(default=20, ge=1, le=50, description="Maximum trending items")
    timeout: int = Field(default=30000, ge=10000, le=60000, description="Timeout in milliseconds")


class TikTokTrendingItem(BaseModel):
    """Single trending item (hashtag or sound)"""
    type: str = Field(..., description="Type: 'hashtag' or 'sound'")
    name: str = Field(..., description="Hashtag or sound name")
    views_or_posts: Optional[str] = Field(default=None, description="View count or post count")
    url: Optional[str] = Field(default=None, description="URL to trending item")


class TikTokTrendingResponse(BaseModel):
    """Response model for POST /scrape/tiktok/trending"""
    success: bool = Field(..., description="Whether scrape succeeded")
    items: List[TikTokTrendingItem] = Field(default_factory=list, description="Trending items")
    count: int = Field(default=0, description="Number of items")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class TikTokDeepProfileRequest(BaseModel):
    """Request model for POST /scrape/tiktok/deep-profile"""
    username: str = Field(..., description="TikTok handle (no @)")
    max_videos: int = Field(default=100, ge=1, le=200, description="Max videos to scrape (1-200)")
    timeout: int = Field(default=120000, ge=30000, le=300000, description="Timeout in milliseconds")


class TikTokDeepProfileResponse(BaseModel):
    """Response model for POST /scrape/tiktok/deep-profile"""
    success: bool = Field(..., description="Whether scrape succeeded")
    username: str = Field(default="", description="TikTok username")
    scraped_at: str = Field(default="", description="ISO timestamp of scrape")
    profile: Dict[str, Any] = Field(default_factory=dict, description="Full user profile from TikTokApi")
    videos: List[Dict[str, Any]] = Field(default_factory=list, description="Video list with full stats")
    aggregates: Dict[str, Any] = Field(default_factory=dict, description="Computed aggregate analytics")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class TikTokNicheAnalyzeRequest(BaseModel):
    """Request model for POST /scrape/tiktok/niche-analyze"""
    username: str = Field(..., description="TikTok handle (no @)")
    max_videos: int = Field(default=50, ge=1, le=200, description="Videos to analyze (1-200)")
    timeout: int = Field(default=120000, ge=30000, le=300000, description="Timeout in milliseconds")


class TikTokNicheAnalyzeResponse(BaseModel):
    """Response model for POST /scrape/tiktok/niche-analyze"""
    success: bool = Field(..., description="Whether analysis succeeded")
    username: str = Field(default="", description="TikTok username")
    current_profile: Dict[str, Any] = Field(default_factory=dict, description="Account baseline stats")
    detected_niches: List[str] = Field(default_factory=list, description="Niches detected in content")
    niche_scores: List[Dict[str, Any]] = Field(default_factory=list, description="Ranked niche recommendations")
    summary: str = Field(default="", description="Plain English 2-3 sentence summary")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# INSTAGRAM
# ─────────────────────────────────────────────

class InstagramPost(BaseModel):
    """Single Instagram post data"""
    url: Optional[str] = None
    caption: Optional[str] = None
    likes: Optional[str] = None


class InstagramRequest(BaseModel):
    """Request model for POST /scrape/instagram"""
    username: str = Field(..., description="Instagram username (no @), e.g. 'natgeo'")
    agent_port: Optional[int] = Field(default=None, description="Optional CDP port for logged-in browser session")
    timeout: int = Field(default=30000, ge=5000, le=120000, description="Timeout in milliseconds")


class InstagramData(BaseModel):
    """Instagram profile data"""
    name: str = Field(default="", description="Display name")
    bio: str = Field(default="", description="Profile bio")
    followers: str = Field(default="", description="Followers count")
    following: str = Field(default="", description="Following count")
    posts_count: str = Field(default="", description="Number of posts")
    is_verified: bool = Field(default=False, description="Whether account is verified")
    profile_pic: str = Field(default="", description="Profile picture URL")
    recent_posts: List[InstagramPost] = Field(default_factory=list, description="Recent posts")


class InstagramResponse(BaseModel):
    """Response model for POST /scrape/instagram"""
    success: bool = Field(..., description="Whether scrape succeeded")
    username: str = Field(..., description="Instagram username")
    data: InstagramData = Field(default_factory=InstagramData, description="Profile data")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# SHOPIFY
# ─────────────────────────────────────────────

class ShopifyRequest(BaseModel):
    """Request model for POST /scrape/shopify"""
    url: str = Field(..., description="Shopify store URL (e.g., https://store.myshopify.com)")
    limit: int = Field(default=50, ge=1, le=250, description="Number of products to return")
    page: int = Field(default=1, ge=1, description="Page number for pagination")


class ShopifyProduct(BaseModel):
    """Single Shopify product data"""
    id: int = Field(..., description="Product ID")
    title: str = Field(..., description="Product title")
    vendor: Optional[str] = Field(default=None, description="Product vendor/brand")
    product_type: Optional[str] = Field(default=None, description="Product type/category")
    handle: Optional[str] = Field(default=None, description="Product handle (URL slug)")
    price: Optional[str] = Field(default=None, description="Product price")
    compare_at_price: Optional[str] = Field(default=None, description="Original/compare price")
    sku: Optional[str] = Field(default=None, description="Product SKU")
    available: bool = Field(default=True, description="Whether product is available")
    image_url: Optional[str] = Field(default=None, description="Primary product image URL")
    product_url: Optional[str] = Field(default=None, description="Full product page URL")


class ShopifyResponse(BaseModel):
    """Response model for POST /scrape/shopify"""
    success: bool = Field(..., description="Whether scrape succeeded")
    store_url: str = Field(..., description="Store URL that was scraped")
    product_count: int = Field(default=0, description="Number of products returned")
    page: int = Field(default=1, description="Page number")
    products: List[ShopifyProduct] = Field(default_factory=list, description="List of products")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


class ShopifyProductDetail(BaseModel):
    """Full Shopify product detail with variants and all images"""
    id: int = Field(default=0, description="Shopify product ID")
    title: str = Field(default="", description="Product title")
    vendor: Optional[str] = Field(default=None, description="Vendor/brand")
    product_type: Optional[str] = Field(default=None, description="Product type/category")
    handle: Optional[str] = Field(default=None, description="URL handle/slug")
    price: Optional[str] = Field(default=None, description="Price of first variant")
    compare_at_price: Optional[str] = Field(default=None, description="Compare-at price (original)")
    sku: Optional[str] = Field(default=None, description="SKU of first variant")
    available: bool = Field(default=True, description="Whether any variant is available")
    image_url: Optional[str] = Field(default=None, description="Primary image URL")
    image_urls: List[str] = Field(default_factory=list, description="All product image URLs")
    product_url: Optional[str] = Field(default=None, description="Full product page URL")
    description: Optional[str] = Field(default=None, description="Product description (may include HTML)")
    tags: List[str] = Field(default_factory=list, description="Product tags")
    variants: List[Dict[str, Any]] = Field(default_factory=list, description="All variants with price/sku/availability")


class ShopifyProductDetailRequest(BaseModel):
    """Request model for POST /scrape/shopify/product"""
    url: str = Field(..., description="Full product URL (e.g., https://example-store.com/products/some-handle)")
    timeout: int = Field(default=30000, ge=5000, le=90000, description="Timeout in milliseconds")


class ShopifyProductDetailResponse(BaseModel):
    """Response model for POST /scrape/shopify/product"""
    success: bool = Field(..., description="Whether scrape succeeded")
    url: str = Field(..., description="Product URL that was scraped")
    product: Optional[ShopifyProductDetail] = Field(default=None, description="Product detail data")
    source: Optional[str] = Field(default=None, description="Data source: 'json' or 'dom'")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")


# ─────────────────────────────────────────────
# MONITOR
# ─────────────────────────────────────────────

class MonitorRequest(BaseModel):
    """Request model for POST /monitor"""
    url: str = Field(..., description="URL to monitor for changes")
    interval_seconds: int = Field(default=300, ge=60, le=86400, description="Check interval in seconds")
    webhook_url: str = Field(..., description="Webhook URL to call when changes detected")
    selector: Optional[str] = Field(default=None, description="CSS selector to monitor (optional)")
    label: Optional[str] = Field(default=None, description="Optional label for this monitor")


class MonitorStatus(BaseModel):
    """Status of a single monitor"""
    monitor_id: str = Field(..., description="Unique monitor ID")
    url: str = Field(..., description="Monitored URL")
    label: Optional[str] = Field(default=None, description="Monitor label")
    interval_seconds: int = Field(..., description="Check interval in seconds")
    webhook_url: str = Field(..., description="Webhook URL")
    status: str = Field(..., description="Monitor status: active|paused|error")
    last_checked: Optional[str] = Field(default=None, description="ISO timestamp of last check")
    last_changed: Optional[str] = Field(default=None, description="ISO timestamp of last change")


class MonitorResponse(BaseModel):
    """Response model for POST /monitor"""
    monitor_id: str = Field(..., description="Unique monitor ID")
    url: str = Field(..., description="Monitored URL")
    interval_seconds: int = Field(..., description="Check interval")
    status: str = Field(..., description="Monitor status")


class MonitorListResponse(BaseModel):
    """Response model for GET /monitor"""
    monitors: List[MonitorStatus] = Field(default_factory=list, description="List of monitors")
    count: int = Field(default=0, description="Number of monitors")


# ─────────────────────────────────────────────
# EBAY
# ─────────────────────────────────────────────

class EbaySearchRequest(BaseModel):
    """Request model for POST /scrape/ebay/search"""
    query: str = Field(..., description="Search query")
    condition: Literal["new", "used", "any"] = Field(default="any", description="Item condition filter")
    min_price: Optional[float] = Field(default=None, description="Minimum price")
    max_price: Optional[float] = Field(default=None, description="Maximum price")
    listing_type: Literal["auction", "buy_it_now", "any"] = Field(default="any", description="Listing type filter")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results to return")
    sort: Literal["best_match", "price_asc", "price_desc", "ending_soon"] = Field(default="best_match", description="Sort order")


class EbayListing(BaseModel):
    """Single eBay listing"""
    title: str = Field(..., description="Item title")
    price: str = Field(..., description="Item price")
    condition: Optional[str] = Field(default=None, description="Item condition")
    shipping: Optional[str] = Field(default=None, description="Shipping cost/info")
    location: Optional[str] = Field(default=None, description="Seller location")
    seller_name: Optional[str] = Field(default=None, description="Seller name")
    seller_rating: Optional[str] = Field(default=None, description="Seller rating")
    listing_url: str = Field(..., description="eBay listing URL")
    image_url: Optional[str] = Field(default=None, description="Primary image URL")
    bids: Optional[int] = Field(default=None, description="Number of bids (if auction)")
    time_left: Optional[str] = Field(default=None, description="Time left (if auction)")
    item_id: Optional[str] = Field(default=None, description="eBay item ID")


class EbaySearchResponse(BaseModel):
    """Response model for POST /scrape/ebay/search"""
    success: bool = Field(..., description="Whether scrape succeeded")
    listings: List[EbayListing] = Field(default_factory=list, description="eBay listings")
    count: int = Field(default=0, description="Number of listings")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class EbayItemRequest(BaseModel):
    """Request model for POST /scrape/ebay/item"""
    url: Optional[str] = Field(default=None, description="Full eBay item URL")
    item_id: Optional[str] = Field(default=None, description="eBay item ID")


class EbayItemResponse(BaseModel):
    """Response model for POST /scrape/ebay/item"""
    success: bool = Field(..., description="Whether scrape succeeded")
    title: Optional[str] = Field(default=None, description="Item title")
    price: Optional[str] = Field(default=None, description="Current price")
    condition: Optional[str] = Field(default=None, description="Item condition")
    description: Optional[str] = Field(default=None, description="Item description")
    seller: Optional[Dict[str, Any]] = Field(default=None, description="Seller info")
    shipping_options: List[str] = Field(default_factory=list, description="Shipping options")
    images: List[str] = Field(default_factory=list, description="Image URLs")
    specs: Dict[str, str] = Field(default_factory=dict, description="Item specifications")
    sold_count: Optional[int] = Field(default=None, description="Number sold")
    watchers: Optional[int] = Field(default=None, description="Number of watchers")
    url: Optional[str] = Field(default=None, description="Item URL")
    item_id: Optional[str] = Field(default=None, description="eBay item ID")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# GOOGLE MAPS (EXTENDED)
# ─────────────────────────────────────────────

class GoogleMapsSearchRequest(BaseModel):
    """Request model for POST /scrape/google_maps/search"""
    query: str = Field(..., description="Search query e.g. 'coffee shops in Louisville'")
    location: Optional[str] = Field(default=None, description="Location context")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    type: Literal["restaurant", "hotel", "gas_station", "any"] = Field(default="any", description="Place type filter")


class GoogleMapsPlace(BaseModel):
    """Single Google Maps place result"""
    name: str = Field(..., description="Business name")
    rating: Optional[float] = Field(default=None, description="Star rating")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    address: Optional[str] = Field(default=None, description="Full address")
    phone: Optional[str] = Field(default=None, description="Phone number")
    website: Optional[str] = Field(default=None, description="Website URL")
    hours_open_now: Optional[bool] = Field(default=None, description="Currently open")
    price_level: Optional[str] = Field(default=None, description="Price level ($ to $$$$)")
    categories: List[str] = Field(default_factory=list, description="Business categories")
    maps_url: str = Field(..., description="Google Maps URL")
    coordinates: Optional[Dict[str, float]] = Field(default=None, description="Lat/lng coordinates")


class GoogleMapsSearchResponse(BaseModel):
    """Response model for POST /scrape/google_maps/search"""
    success: bool = Field(..., description="Whether scrape succeeded")
    places: List[GoogleMapsPlace] = Field(default_factory=list, description="Place results")
    count: int = Field(default=0, description="Number of places")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class GoogleMapsPlaceRequest(BaseModel):
    """Request model for POST /scrape/google_maps/place"""
    place_url: Optional[str] = Field(default=None, description="Direct Google Maps place URL")
    query: Optional[str] = Field(default=None, description="Query to search for")
    location: Optional[str] = Field(default=None, description="Location for query search")


class GoogleMapsPlaceReview(BaseModel):
    """Single review from Google Maps place"""
    author: Optional[str] = Field(default=None, description="Reviewer name")
    rating: Optional[int] = Field(default=None, description="Star rating (1-5)")
    text: Optional[str] = Field(default=None, description="Review text")
    date: Optional[str] = Field(default=None, description="Review date")


class GoogleMapsPlaceDetail(BaseModel):
    """Detailed place information"""
    name: str = Field(..., description="Business name")
    rating: Optional[float] = Field(default=None, description="Star rating")
    review_count: Optional[int] = Field(default=None, description="Number of reviews")
    address: Optional[str] = Field(default=None, description="Full address")
    phone: Optional[str] = Field(default=None, description="Phone number")
    website: Optional[str] = Field(default=None, description="Website URL")
    hours: Dict[str, str] = Field(default_factory=dict, description="Opening hours by day")
    hours_open_now: Optional[bool] = Field(default=None, description="Currently open")
    price_level: Optional[str] = Field(default=None, description="Price level")
    categories: List[str] = Field(default_factory=list, description="Business categories")
    coordinates: Optional[Dict[str, float]] = Field(default=None, description="Lat/lng coordinates")
    reviews: List[GoogleMapsPlaceReview] = Field(default_factory=list, description="Top reviews")
    photos_count: Optional[int] = Field(default=None, description="Number of photos")
    popular_times: Optional[Dict[str, Any]] = Field(default=None, description="Popular times summary")
    qa_highlights: List[str] = Field(default_factory=list, description="Q&A highlights")


class GoogleMapsPlaceResponse(BaseModel):
    """Response model for POST /scrape/google_maps/place"""
    success: bool = Field(..., description="Whether scrape succeeded")
    place: Optional[GoogleMapsPlaceDetail] = Field(default=None, description="Place details")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# REDFIN
# ─────────────────────────────────────────────

class RedfinSearchRequest(BaseModel):
    """Request model for POST /scrape/redfin/search"""
    location: str = Field(..., description="Location to search (city, zip, address)")
    listing_type: Literal["for_sale", "sold", "for_rent"] = Field(default="for_sale", description="Type of listing")
    min_price: Optional[int] = Field(default=None, description="Minimum price")
    max_price: Optional[int] = Field(default=None, description="Maximum price")
    min_beds: Optional[int] = Field(default=None, ge=0, description="Minimum bedrooms")
    min_baths: Optional[float] = Field(default=None, ge=0, description="Minimum bathrooms")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results")


class RedfinListing(BaseModel):
    """Single Redfin listing"""
    address: str = Field(..., description="Property address")
    price: Optional[int] = Field(default=None, description="Listing price")
    beds: Optional[float] = Field(default=None, description="Number of bedrooms")
    baths: Optional[float] = Field(default=None, description="Number of bathrooms")
    sqft: Optional[int] = Field(default=None, description="Square footage")
    price_per_sqft: Optional[int] = Field(default=None, description="Price per sqft")
    days_on_market: Optional[int] = Field(default=None, description="Days since listed")
    listing_url: str = Field(..., description="Redfin listing URL")
    image_url: Optional[str] = Field(default=None, description="Primary image URL")
    status: Optional[str] = Field(default=None, description="Listing status")
    year_built: Optional[int] = Field(default=None, description="Year built")
    lot_size: Optional[str] = Field(default=None, description="Lot size")


class RedfinSearchResponse(BaseModel):
    """Response model for POST /scrape/redfin/search"""
    success: bool = Field(..., description="Whether scrape succeeded")
    listings: List[RedfinListing] = Field(default_factory=list, description="Property listings")
    count: int = Field(default=0, description="Number of listings")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class RedfinPropertyRequest(BaseModel):
    """Request model for POST /scrape/redfin/property"""
    url: str = Field(..., description="Redfin property URL")


class RedfinPriceHistory(BaseModel):
    """Price history entry"""
    date: str = Field(..., description="Date of price change")
    event: str = Field(..., description="Event type (Listed, Sold, Price Change)")
    price: Optional[int] = Field(default=None, description="Price at time of event")


class RedfinTaxHistory(BaseModel):
    """Tax history entry"""
    year: int = Field(..., description="Tax year")
    taxes: Optional[int] = Field(default=None, description="Tax amount")
    assessment: Optional[int] = Field(default=None, description="Assessed value")


class RedfinSchool(BaseModel):
    """Nearby school"""
    name: str = Field(..., description="School name")
    rating: Optional[float] = Field(default=None, description="School rating")
    distance: Optional[str] = Field(default=None, description="Distance from property")
    grades: Optional[str] = Field(default=None, description="Grade levels")


class RedfinPropertyResponse(BaseModel):
    """Response model for POST /scrape/redfin/property"""
    success: bool = Field(..., description="Whether scrape succeeded")
    address: Optional[str] = Field(default=None, description="Property address")
    price: Optional[int] = Field(default=None, description="Current price")
    beds: Optional[float] = Field(default=None, description="Bedrooms")
    baths: Optional[float] = Field(default=None, description="Bathrooms")
    sqft: Optional[int] = Field(default=None, description="Square footage")
    lot_size: Optional[str] = Field(default=None, description="Lot size")
    year_built: Optional[int] = Field(default=None, description="Year built")
    description: Optional[str] = Field(default=None, description="Property description")
    price_history: List[RedfinPriceHistory] = Field(default_factory=list, description="Price history")
    tax_history: List[RedfinTaxHistory] = Field(default_factory=list, description="Tax history")
    schools: List[RedfinSchool] = Field(default_factory=list, description="Nearby schools")
    walk_score: Optional[int] = Field(default=None, description="Walk Score")
    transit_score: Optional[int] = Field(default=None, description="Transit Score")
    images: List[str] = Field(default_factory=list, description="Property images")
    url: Optional[str] = Field(default=None, description="Property URL")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


# ─────────────────────────────────────────────
# LINKEDIN (EXTENDED)
# ─────────────────────────────────────────────

class LinkedInProfileRequest(BaseModel):
    """Request model for POST /scrape/linkedin/profile"""
    url: Optional[str] = Field(default=None, description="LinkedIn profile URL")
    username: Optional[str] = Field(default=None, description="LinkedIn username (from linkedin.com/in/username)")


class LinkedInExperience(BaseModel):
    """LinkedIn experience entry"""
    title: str = Field(..., description="Job title")
    company: str = Field(..., description="Company name")
    duration: Optional[str] = Field(default=None, description="Employment duration")
    location: Optional[str] = Field(default=None, description="Job location")


class LinkedInEducation(BaseModel):
    """LinkedIn education entry"""
    school: str = Field(..., description="School name")
    degree: Optional[str] = Field(default=None, description="Degree earned")
    years: Optional[str] = Field(default=None, description="Years attended")


class LinkedInProfileResponse(BaseModel):
    """Response model for POST /scrape/linkedin/profile"""
    success: bool = Field(..., description="Whether scrape succeeded")
    name: Optional[str] = Field(default=None, description="Full name")
    headline: Optional[str] = Field(default=None, description="Professional headline")
    location: Optional[str] = Field(default=None, description="Location")
    about: Optional[str] = Field(default=None, description="About section")
    experience: List[LinkedInExperience] = Field(default_factory=list, description="Work experience")
    education: List[LinkedInEducation] = Field(default_factory=list, description="Education")
    skills: List[str] = Field(default_factory=list, description="Top skills")
    connections_approx: Optional[str] = Field(default=None, description="Approximate connection count")
    profile_url: Optional[str] = Field(default=None, description="LinkedIn profile URL")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class LinkedInCompanyRequest(BaseModel):
    """Request model for POST /scrape/linkedin/company"""
    url: Optional[str] = Field(default=None, description="LinkedIn company URL")
    company_name: Optional[str] = Field(default=None, description="Company name to search")


class LinkedInCompanyPost(BaseModel):
    """LinkedIn company post"""
    text: Optional[str] = Field(default=None, description="Post content")
    date: Optional[str] = Field(default=None, description="Post date")
    engagement: Optional[str] = Field(default=None, description="Likes/comments summary")


class LinkedInCompanyResponse(BaseModel):
    """Response model for POST /scrape/linkedin/company"""
    success: bool = Field(..., description="Whether scrape succeeded")
    name: Optional[str] = Field(default=None, description="Company name")
    tagline: Optional[str] = Field(default=None, description="Company tagline")
    about: Optional[str] = Field(default=None, description="About description")
    industry: Optional[str] = Field(default=None, description="Industry")
    size: Optional[str] = Field(default=None, description="Company size")
    founded: Optional[str] = Field(default=None, description="Founded year")
    headquarters: Optional[str] = Field(default=None, description="Headquarters location")
    website: Optional[str] = Field(default=None, description="Company website")
    followers: Optional[str] = Field(default=None, description="Follower count")
    specialties: List[str] = Field(default_factory=list, description="Company specialties")
    recent_posts: List[LinkedInCompanyPost] = Field(default_factory=list, description="Recent posts")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class LinkedInJobsRequest(BaseModel):
    """Request model for POST /scrape/linkedin/jobs"""
    query: str = Field(..., description="Job search query")
    location: Optional[str] = Field(default=None, description="Job location")
    remote: bool = Field(default=False, description="Remote jobs only")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum results")


class LinkedInJob(BaseModel):
    """LinkedIn job listing"""
    title: str = Field(..., description="Job title")
    company: str = Field(..., description="Company name")
    location: str = Field(..., description="Job location")
    posted_date: Optional[str] = Field(default=None, description="When job was posted")
    job_url: str = Field(..., description="Job posting URL")
    salary_range: Optional[str] = Field(default=None, description="Salary range if shown")
    easy_apply: bool = Field(default=False, description="Easy Apply available")


class LinkedInJobsResponse(BaseModel):
    """Response model for POST /scrape/linkedin/jobs"""
    success: bool = Field(..., description="Whether scrape succeeded")
    jobs: List[LinkedInJob] = Field(default_factory=list, description="Job listings")
    count: int = Field(default=0, description="Number of jobs")
    elapsed_ms: int = Field(default=0, description="Time taken in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
