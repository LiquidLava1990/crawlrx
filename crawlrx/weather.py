"""Weather scraper -- wttr.in JSON API (no auth, no Playwright)."""
# Browser: optional CDP port 9223 (see CRAWLRX_CDP_PORTS)
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ._scraper_helpers import network_exception_types

logger = logging.getLogger(__name__)
router = APIRouter(tags=["weather"])

_FETCH_ERRORS: tuple[type[BaseException], ...] = (
    *network_exception_types(),
    requests.RequestException,
    ValueError,
    KeyError,
    TypeError,
)


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────


class WeatherCurrentRequest(BaseModel):
    """Request model for POST /scrape/weather/current."""

    location: str = Field(..., description="City name, zip code, or 'lat,lon'")


class WeatherForecastRequest(BaseModel):
    """Request model for POST /scrape/weather/forecast."""

    location: str = Field(..., description="City name, zip code, or 'lat,lon'")
    days: int = Field(default=7, ge=1, le=10, description="Number of days (1-10)")


class WeatherHistoricalRequest(BaseModel):
    """Request model for POST /scrape/weather/historical."""

    location: str = Field(..., description="City name, zip code, or 'lat,lon'")
    date: str = Field(..., description="Date in YYYY-MM-DD format")


class WeatherCurrentResponse(BaseModel):
    """Response model for current weather."""

    success: bool
    weather: Optional[Dict[str, Any]] = None
    elapsed_ms: int
    error: Optional[str] = None


class WeatherForecastResponse(BaseModel):
    """Response model for weather forecast."""

    success: bool
    forecast: Optional[List[Dict[str, Any]]] = None
    elapsed_ms: int
    error: Optional[str] = None


class WeatherHistoricalResponse(BaseModel):
    """Response model for historical weather."""

    success: bool
    weather: Optional[Dict[str, Any]] = None
    elapsed_ms: int
    error: Optional[str] = None


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


def _sync_fetch(location: str) -> Optional[Dict[str, Any]]:
    """Synchronous wttr.in fetch (runs in a thread)."""
    encoded_location = location.replace(" ", "+")
    url = f"https://wttr.in/{encoded_location}?format=j1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


async def _fetch_weather_data(location: str) -> Optional[Dict[str, Any]]:
    """Fetch wttr.in JSON for ``location`` off the event loop."""
    return await asyncio.to_thread(_sync_fetch, location)


def _first_value(records: List[Dict[str, Any]], key: str, fallback: str = "") -> str:
    """Pull ``records[0][key]`` defensively, returning ``fallback`` on miss."""
    if not records:
        return fallback
    head = records[0] if isinstance(records[0], dict) else {}
    value = head.get(key, fallback)
    return value if isinstance(value, str) else fallback


def _location_string(nearest_area: Dict[str, Any]) -> str:
    """Stitch nearest-area into a ``"City, Region, Country"`` string."""
    area_name = _first_value(nearest_area.get("areaName", []) or [], "value", "Unknown")
    region = _first_value(nearest_area.get("region", []) or [], "value", "")
    country = _first_value(nearest_area.get("country", []) or [], "value", "")
    if region:
        return f"{area_name}, {region}, {country}"
    return f"{area_name}, {country}"


def _parse_current_weather(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse current-conditions block from a wttr.in response."""
    current = (data.get("current_condition") or [{}])[0]
    nearest_area = (data.get("nearest_area") or [{}])[0]
    temp_c = float(current.get("temp_C", 0) or 0)
    temp_f = float(current.get("temp_F", 0) or 0)
    return {
        "location": _location_string(nearest_area),
        "temperature_f": round(temp_f, 1),
        "temperature_c": round(temp_c, 1),
        "feels_like_f": round(float(current.get("FeelsLikeF", temp_f) or temp_f), 1),
        "feels_like_c": round(float(current.get("FeelsLikeC", temp_c) or temp_c), 1),
        "condition": _first_value(current.get("weatherDesc") or [], "value", "Unknown"),
        "humidity": int(current.get("humidity", 0) or 0),
        "wind_mph": float(current.get("windspeedMiles", 0) or 0),
        "wind_direction": current.get("winddir16Point", "N"),
        "visibility_miles": float(current.get("visibilityMiles", 0) or 0),
        "uv_index": int(current.get("uvIndex", 0) or 0),
        "air_quality_index": _extract_aqi(current),
        "last_updated": current.get(
            "observation_time", datetime.now().strftime("%H:%M")
        ),
    }


def _extract_aqi(current: Dict[str, Any]) -> Optional[int]:
    """Approximate US AQI from PM2.5 in the wttr.in air_quality block."""
    pm25 = (current.get("air_quality") or {}).get("pm2.5")
    if not pm25:
        return None
    try:
        return int(float(pm25) * 4)
    except (ValueError, TypeError):
        return None


def _astronomy(day: Dict[str, Any], key: str) -> str:
    """Pull a sunrise/sunset value out of a day's astronomy block."""
    return _first_value(day.get("astronomy") or [], key, "")


def _midday_hourly(hourly: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the midday hourly record (or empty dict if none)."""
    return hourly[len(hourly) // 2] if hourly else {}


def _day_high_low(day: Dict[str, Any]) -> Dict[str, int]:
    """Return ``{high_f, high_c, low_f, low_c}`` for a wttr.in day record."""
    return {
        "high_f": int(day.get("maxtempF", 0) or 0),
        "high_c": int(day.get("maxtempC", 0) or 0),
        "low_f": int(day.get("mintempF", 0) or 0),
        "low_c": int(day.get("mintempC", 0) or 0),
    }


def _day_astronomy(day: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{sunrise, sunset}`` for a wttr.in day record."""
    return {"sunrise": _astronomy(day, "sunrise"), "sunset": _astronomy(day, "sunset")}


def _midday_condition(midday: Dict[str, Any]) -> str:
    """Pull the midday weather description, defaulting to ``"Unknown"``."""
    return _first_value(midday.get("weatherDesc") or [], "value", "Unknown")


def _format_forecast_day(day: Dict[str, Any]) -> Dict[str, Any]:
    """Format a single forecast day into the response shape."""
    midday = _midday_hourly(day.get("hourly") or [])
    record: Dict[str, Any] = {"date": day.get("date", "")}
    record.update(_day_high_low(day))
    record["condition"] = _midday_condition(midday)
    record["precipitation_chance"] = int(midday.get("chanceofrain", 0) or 0)
    record["humidity"] = int(midday.get("humidity", 0) or 0)
    record["wind_mph"] = float(midday.get("windspeedMiles", 0) or 0)
    record.update(_day_astronomy(day))
    return record


def _parse_forecast(data: Dict[str, Any], days: int) -> List[Dict[str, Any]]:
    """Parse forecast records from a wttr.in response."""
    return [_format_forecast_day(day) for day in (data.get("weather") or [])[:days]]


def _format_historical_day(day: Dict[str, Any], date: str) -> Dict[str, Any]:
    """Format a single historical day into the response shape."""
    hourly = day.get("hourly") or []
    midday = _midday_hourly(hourly)
    record: Dict[str, Any] = {"date": date}
    record.update(_day_high_low(day))
    record["condition"] = _midday_condition(midday)
    record["humidity"] = (
        sum(int(h.get("humidity", 0) or 0) for h in hourly) // len(hourly)
        if hourly else 0
    )
    record["wind_mph"] = round(
        sum(float(h.get("windspeedMiles", 0) or 0) for h in hourly) / len(hourly)
        if hourly else 0,
        1,
    )
    record.update(_day_astronomy(day))
    return record


def _parse_historical(data: Dict[str, Any], date: str) -> Dict[str, Any]:
    """Parse historical weather for a specific date from wttr.in payload."""
    for day in data.get("weather") or []:
        if day.get("date") == date:
            return _format_historical_day(day, date)
    return {"error": f"No data available for {date}"}


def _ms_since(start: float) -> int:
    """Milliseconds elapsed since ``start`` (a ``time.time()`` value)."""
    return int((time.time() - start) * 1000)


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────


async def _load_payload(
    location: str, label: str, start: float
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch wttr.in payload for ``location``; returns (data, error_message)."""
    try:
        data = await _fetch_weather_data(location)
    except _FETCH_ERRORS as exc:
        logger.error("Error fetching %s: %s: %s", label, type(exc).__name__, exc)
        return None, str(exc)
    if not data:
        return None, "Failed to fetch weather data"
    return data, None


@router.post("/scrape/weather/current", response_model=WeatherCurrentResponse)
async def get_current_weather(
    request: WeatherCurrentRequest,
) -> WeatherCurrentResponse:
    """Get current weather for a location."""
    start = time.time()
    data, error = await _load_payload(request.location, "current weather", start)
    if data is None:
        return WeatherCurrentResponse(
            success=False, error=error, elapsed_ms=_ms_since(start)
        )
    return WeatherCurrentResponse(
        success=True,
        weather=_parse_current_weather(data),
        elapsed_ms=_ms_since(start),
    )


@router.post("/scrape/weather/forecast", response_model=WeatherForecastResponse)
async def get_weather_forecast(
    request: WeatherForecastRequest,
) -> WeatherForecastResponse:
    """Get weather forecast for a location."""
    start = time.time()
    data, error = await _load_payload(request.location, "weather forecast", start)
    if data is None:
        return WeatherForecastResponse(
            success=False, error=error, elapsed_ms=_ms_since(start)
        )
    return WeatherForecastResponse(
        success=True,
        forecast=_parse_forecast(data, request.days),
        elapsed_ms=_ms_since(start),
    )


@router.post("/scrape/weather/historical", response_model=WeatherHistoricalResponse)
async def get_historical_weather(
    request: WeatherHistoricalRequest,
) -> WeatherHistoricalResponse:
    """Get historical weather for a date (wttr.in keeps ~7 days)."""
    start = time.time()
    data, error = await _load_payload(request.location, "historical weather", start)
    if data is None:
        return WeatherHistoricalResponse(
            success=False, error=error, elapsed_ms=_ms_since(start)
        )
    weather = _parse_historical(data, request.date)
    if "error" in weather:
        return WeatherHistoricalResponse(
            success=False, error=weather["error"], elapsed_ms=_ms_since(start)
        )
    return WeatherHistoricalResponse(
        success=True, weather=weather, elapsed_ms=_ms_since(start)
    )
