"""External enrichment: OpenWeatherMap + LocationIQ reverse geocode.

Both degrade gracefully to empty dicts when no API key is configured, so the
service runs locally without secrets.
"""
import time

import httpx

from .config import settings

# Simple in-memory TTL cache: avoid hammering the weather API on every ping.
# Key = (lat_bucket, lon_bucket); value = (cached_at_epoch, result_dict).
_weather_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_WEATHER_TTL_S = 600      # 10 minutes
_WEATHER_GRID_DEG = 0.05  # ~5 km bucket size


async def fetch_weather(lat: float, lon: float) -> dict:
    if not settings.openweather_api_key:
        return {}
    key = (round(lat / _WEATHER_GRID_DEG), round(lon / _WEATHER_GRID_DEG))
    cached_at, cached = _weather_cache.get(key, (0.0, {}))
    if cached and time.monotonic() - cached_at < _WEATHER_TTL_S:
        return cached
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "units": "metric",
        "appid": settings.openweather_api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            d = r.json()
        result = {
            "weather_main": d["weather"][0]["main"],
            "weather_desc": d["weather"][0]["description"],
            "temp": d["main"]["temp"],
            "pressure": d["main"]["pressure"],
            "humidity": d["main"]["humidity"],
            "wind_speed": d.get("wind", {}).get("speed"),
        }
        _weather_cache[key] = (time.monotonic(), result)
        return result
    except Exception:
        return {}


async def reverse_geocode(lat: float, lon: float) -> dict:
    if not settings.locationiq_api_key:
        return {}
    url = "https://us1.locationiq.com/v1/reverse"
    params = {
        "key": settings.locationiq_api_key,
        "lat": lat,
        "lon": lon,
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            d = r.json()
        return {"address": d.get("display_name")}
    except Exception:
        return {}
