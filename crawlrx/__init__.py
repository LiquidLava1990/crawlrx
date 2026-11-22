"""CrawlRX -- Unified local web API service (crawl / scrape / search).

The package surface is built declaratively via ``_optional_imports.load_optional``:
each optional scraper submodule is listed in ``_OPTIONAL_SPECS`` with its
export names. Submodules that fail to import (missing optional dependency,
missing attribute) bind their exports to ``None`` so callers can probe
availability with a plain ``is None`` check -- the legacy contract.
"""

from __future__ import annotations

# Every Playwright browser CrawlRX opens must stay out of internal address space.
from . import browser_guard as _browser_guard

_browser_guard.install()

# Core export -- this one MUST be present (no graceful fallback).
from .platform_detect import detect_platform, detect_platform_batch
from ._optional_imports import ImportSpec, load_optional


# fmt: off
_OPTIONAL_SPECS: tuple[ImportSpec, ...] = (
    # Platform detectors (detect + scrape pair)
    ImportSpec("shopify",              ("detect_shopify", "scrape_shopify_products")),
    ImportSpec("woocommerce",          ("detect_woocommerce", "scrape_woocommerce_products")),
    ImportSpec("wordpress",            (("scrape_wordpress_posts", "scrape_wordpress_products"),)),
    ImportSpec("squarespace_scraper",  ("detect_squarespace", "scrape_squarespace_products")),
    ImportSpec("wix_scraper",          ("detect_wix", "scrape_wix_products")),
    ImportSpec("bigcommerce_scraper",  ("detect_bigcommerce", "scrape_bigcommerce_products")),
    ImportSpec("magento_scraper",      ("detect_magento", "scrape_magento_products")),
    ImportSpec("prestashop_scraper",   ("detect_prestashop", "scrape_prestashop_products")),
    ImportSpec("godaddy_scraper",      ("detect_godaddy", "scrape_godaddy_products")),
    ImportSpec("weebly_scraper",       ("detect_weebly", "scrape_weebly_products")),
    ImportSpec("square_scraper",       ("detect_square", "scrape_square_products")),
    ImportSpec("ecwid_scraper",        ("detect_ecwid", "scrape_ecwid_products")),
    ImportSpec("volusion_scraper",     ("detect_volusion", "scrape_volusion_products")),
    ImportSpec("opencart_scraper",     ("detect_opencart", "scrape_opencart_products")),
    ImportSpec("shift4shop_scraper",   ("detect_shift4shop", "scrape_shift4shop_products")),
    ImportSpec("nopcommerce_scraper",  ("detect_nopcommerce", "scrape_nopcommerce_products")),
    ImportSpec("cscart_scraper",       ("detect_cscart", "scrape_cscart_products")),
    ImportSpec("drupal_scraper",       ("detect_drupal", "scrape_drupal_products")),
    ImportSpec("ghost_scraper",        ("detect_ghost", "scrape_ghost_products")),
    ImportSpec("joomla_scraper",       ("detect_joomla", "scrape_joomla_products")),
    ImportSpec("duda_scraper",         ("detect_duda", "scrape_duda_products")),
    ImportSpec("clickfunnels_scraper", ("detect_clickfunnels", "scrape_clickfunnels_content")),
    ImportSpec("kajabi_scraper",       ("detect_kajabi", "scrape_kajabi_products")),
    ImportSpec("jimdo_scraper",        ("detect_jimdo", "scrape_jimdo_products")),
    ImportSpec("tilda_scraper",        ("detect_tilda", "scrape_tilda_content")),

    # E-commerce
    ImportSpec("amazon_scraper",          ("scrape_amazon_products",)),
    ImportSpec("ebay",                    (("search_ebay", "scrape_ebay_products"),)),
    ImportSpec("etsy",                    ("scrape_etsy_products",)),
    ImportSpec("walmart",                 ("scrape_walmart_products",)),
    ImportSpec("target",                  ("scrape_target_products",)),
    ImportSpec("bestbuy",                 ("scrape_bestbuy_products",)),
    ImportSpec("costco",                  ("scrape_costco_products",)),
    ImportSpec("homedepot",               ("scrape_homedepot_products",)),
    ImportSpec("stockx",                  ("scrape_stockx_products",)),
    ImportSpec("generic_product_scraper", ("scrape_generic_products",)),

    # Travel
    ImportSpec("airbnb",         ("scrape_airbnb_listings",)),
    ImportSpec("hotel_scraper",  (("booking_search", "scrape_booking_hotels"),)),
    ImportSpec("expedia",        ("scrape_expedia_listings",)),
    ImportSpec("kayak",          ("scrape_kayak_flights",)),
    ImportSpec("tripadvisor",    ("scrape_tripadvisor_reviews",)),
    ImportSpec("travel_scraper", ("scrape_travel_site",)),

    # Real Estate
    ImportSpec("zillow",     ("scrape_zillow_listings",)),
    ImportSpec("realtor",    ("scrape_realtor_listings",)),
    ImportSpec("redfin",     ("scrape_redfin_listings",)),
    ImportSpec("trulia",     ("scrape_trulia_listings",)),
    ImportSpec("apartments", ("scrape_apartments_listings",)),

    # Jobs
    ImportSpec("indeed",     ("scrape_indeed_jobs",)),
    ImportSpec("linkedin",   ("scrape_linkedin_jobs",)),
    ImportSpec("glassdoor",  ("scrape_glassdoor_jobs",)),
    ImportSpec("angellist",  ("scrape_angellist_jobs",)),

    # Social Media
    ImportSpec("twitter",   ("scrape_twitter_posts",)),
    ImportSpec("instagram", ("scrape_instagram_posts",)),
    ImportSpec("facebook",  ("scrape_facebook_posts",)),
    ImportSpec("tiktok",    ("scrape_tiktok_videos",)),
    ImportSpec("youtube",   ("scrape_youtube_videos",)),
    ImportSpec("reddit",    ("scrape_reddit_posts",)),
    ImportSpec("pinterest", ("scrape_pinterest_pins",)),

    # Search & SEO
    ImportSpec("google_search",    ("google_search",)),
    ImportSpec("google_trends",    ("get_google_trends",)),
    ImportSpec("google_maps",      ("scrape_google_maps",)),
    ImportSpec("gmaps",            ("scrape_gmaps_places",)),
    ImportSpec("local_search",     ("local_search",)),
    ImportSpec("search_deep",      ("deep_search",)),
    ImportSpec("search_fusion",    ("fusion_search",)),
    ImportSpec("search_media",     ("media_search",)),
    ImportSpec("semrush",          ("get_semrush_data",)),
    ImportSpec("ecommerce_search", ("ecommerce_search",)),

    # Reviews & Business
    ImportSpec("yelp",        ("scrape_yelp_reviews",)),
    ImportSpec("trustpilot",  ("scrape_trustpilot_reviews",)),
    ImportSpec("contacts",    ("scrape_contacts",)),
    ImportSpec("metadata",    ("extract_metadata",)),

    # Automotive
    ImportSpec("craigslist", ("scrape_craigslist_listings",)),
    ImportSpec("autotrader", ("scrape_autotrader_listings",)),
    ImportSpec("cars_com",   ("scrape_cars_com_listings",)),

    # Tech & Product
    ImportSpec("github_scraper", ("scrape_github_repos",)),
    ImportSpec("producthunt",    ("scrape_producthunt_products",)),

    # Website Builders
    ImportSpec("webflow", ("detect_webflow", "scrape_webflow_content")),

    # Weather
    ImportSpec("weather", ("get_weather_data",)),
)
# fmt: on


# ``detect_wordpress`` is referenced by callers but not implemented in
# ``wordpress.py``; preserve the historical ``None`` binding.
detect_wordpress = None

globals().update(load_optional(__package__, _OPTIONAL_SPECS))


__version__ = "0.1.0"
__author__ = "CrawlRX contributors"


__all__ = [
    "detect_platform",
    "detect_platform_batch",
    "detect_shopify",
    "detect_woocommerce",
    "detect_wordpress",
    "detect_squarespace",
    "detect_wix",
    "detect_bigcommerce",
    "detect_magento",
    "detect_prestashop",
    "detect_godaddy",
    "detect_weebly",
    "detect_square",
    "detect_ecwid",
    "detect_volusion",
    "detect_opencart",
    "detect_shift4shop",
    "detect_nopcommerce",
    "detect_cscart",
    "detect_drupal",
    "detect_ghost",
    "detect_joomla",
    "detect_duda",
    "detect_clickfunnels",
    "detect_kajabi",
    "detect_jimdo",
    "detect_tilda",
    "scrape_shopify_products",
    "scrape_woocommerce_products",
    "scrape_wordpress_products",
    "scrape_squarespace_products",
    "scrape_wix_products",
    "scrape_bigcommerce_products",
    "scrape_magento_products",
    "scrape_prestashop_products",
    "scrape_godaddy_products",
    "scrape_weebly_products",
    "scrape_square_products",
    "scrape_ecwid_products",
    "scrape_volusion_products",
    "scrape_opencart_products",
    "scrape_shift4shop_products",
    "scrape_nopcommerce_products",
    "scrape_cscart_products",
    "scrape_drupal_products",
    "scrape_ghost_products",
    "scrape_joomla_products",
    "scrape_duda_products",
    "scrape_clickfunnels_content",
    "scrape_kajabi_products",
    "scrape_jimdo_products",
    "scrape_tilda_content",
    # E-commerce
    "scrape_amazon_products",
    "scrape_ebay_products",
    "scrape_etsy_products",
    "scrape_walmart_products",
    "scrape_target_products",
    "scrape_bestbuy_products",
    "scrape_costco_products",
    "scrape_homedepot_products",
    "scrape_stockx_products",
    "scrape_generic_products",
    # Travel
    "scrape_airbnb_listings",
    "scrape_booking_hotels",
    "scrape_expedia_listings",
    "scrape_kayak_flights",
    "scrape_tripadvisor_reviews",
    "scrape_travel_site",
    # Real Estate
    "scrape_zillow_listings",
    "scrape_realtor_listings",
    "scrape_redfin_listings",
    "scrape_trulia_listings",
    "scrape_apartments_listings",
    # Jobs
    "scrape_indeed_jobs",
    "scrape_linkedin_jobs",
    "scrape_glassdoor_jobs",
    "scrape_angellist_jobs",
    # Social Media
    "scrape_twitter_posts",
    "scrape_instagram_posts",
    "scrape_facebook_posts",
    "scrape_tiktok_videos",
    "scrape_youtube_videos",
    "scrape_reddit_posts",
    "scrape_pinterest_pins",
    # Search & SEO
    "google_search",
    "get_google_trends",
    "scrape_google_maps",
    "scrape_gmaps_places",
    "local_search",
    "deep_search",
    "fusion_search",
    "media_search",
    "get_semrush_data",
    "ecommerce_search",
    # Reviews & Business
    "scrape_yelp_reviews",
    "scrape_trustpilot_reviews",
    "scrape_contacts",
    "extract_metadata",
    # Automotive
    "scrape_craigslist_listings",
    "scrape_autotrader_listings",
    "scrape_cars_com_listings",
    # Tech & Product
    "scrape_github_repos",
    "scrape_producthunt_products",
    # Website Builders
    "detect_webflow",
    "scrape_webflow_content",
    # Weather
    "get_weather_data",
]
