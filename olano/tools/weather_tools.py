"""OpenWeatherMap toolkit.

Auth env var: OPENWEATHER_API_KEY (free tier at openweathermap.org).

Direct HTTP — no SDK dependency.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_OWM_BASE = "https://api.openweathermap.org/data/2.5"
_OWM_GEO = "http://api.openweathermap.org/geo/1.0"
_OWM_CREDS_MSG = "[missing-credentials] OPENWEATHER_API_KEY not set in vault."


def _key() -> tuple[str | None, str | None]:
    k = get_credential("OPENWEATHER_API_KEY") or get_credential("OPENWEATHERMAP_API_KEY")
    if not k:
        return None, _OWM_CREDS_MSG
    return k, None


def _geocode(location: str, key: str) -> tuple[tuple[float, float] | None, str | None]:
    """Resolve a city/region/country string to (lat, lon)."""
    try:
        r = httpx.get(
            f"{_OWM_GEO}/direct",
            params={"q": location, "limit": 1, "appid": key},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json()
        if not results:
            return None, f"[tool-error] weather: location '{location}' not found."
        return (results[0]["lat"], results[0]["lon"]), None
    except Exception as exc:
        return None, f"[tool-error] weather geocode: {exc}"


@tool
def weather_current(
    location: Annotated[str, "City name (e.g. 'San Jose,CR' or 'London,UK')"],
    units: Annotated[str, "'metric' (°C), 'imperial' (°F), 'standard' (K)"] = "metric",
) -> str:
    """Get the current weather conditions for a location."""
    key, err = _key()
    if err:
        return err
    assert key is not None
    coords, err = _geocode(location, key)
    if err:
        return err
    lat, lon = coords  # type: ignore[misc]
    try:
        r = httpx.get(
            f"{_OWM_BASE}/weather",
            params={"lat": lat, "lon": lon, "units": units, "appid": key},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        main = d.get("main", {}) or {}
        weather = (d.get("weather") or [{}])[0]
        wind = d.get("wind", {}) or {}
        sys = d.get("sys", {}) or {}
        unit_temp = {"metric": "°C", "imperial": "°F", "standard": "K"}.get(units, "?")
        return (
            f"Weather — {d.get('name', '?')}, {sys.get('country', '?')}:\n"
            f"  Condition: {weather.get('main', '?')} ({weather.get('description', '?')})\n"
            f"  Temp:      {main.get('temp', '?')}{unit_temp} "
            f"(feels like {main.get('feels_like', '?')}{unit_temp})\n"
            f"  Range:     {main.get('temp_min', '?')}{unit_temp} → "
            f"{main.get('temp_max', '?')}{unit_temp}\n"
            f"  Humidity:  {main.get('humidity', '?')}%\n"
            f"  Pressure:  {main.get('pressure', '?')} hPa\n"
            f"  Wind:      {wind.get('speed', '?')} (dir {wind.get('deg', '?')}°)\n"
            f"  Clouds:    {(d.get('clouds') or {}).get('all', '?')}%\n"
            f"  Visibility: {d.get('visibility', '?')} m"
        )
    except Exception as exc:
        return f"[tool-error] weather_current: {exc}"


@tool
def weather_forecast(
    location: Annotated[str, "City name (e.g. 'San Jose,CR')"],
    days: Annotated[int, "Forecast days (1-5)"] = 3,
    units: Annotated[str, "'metric', 'imperial', 'standard'"] = "metric",
) -> str:
    """Get a 5-day / 3-hour forecast for a location (free-tier endpoint)."""
    key, err = _key()
    if err:
        return err
    assert key is not None
    coords, err = _geocode(location, key)
    if err:
        return err
    lat, lon = coords  # type: ignore[misc]
    try:
        r = httpx.get(
            f"{_OWM_BASE}/forecast",
            params={"lat": lat, "lon": lon, "units": units, "appid": key},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        items = (data or {}).get("list", []) or []
        if not items:
            return f"No forecast available for {location}."
        unit_temp = {"metric": "°C", "imperial": "°F", "standard": "K"}.get(units, "?")
        max_items = min(days * 8, len(items))  # 8 x 3h slots/day
        lines = [
            f"Forecast — {(data.get('city') or {}).get('name', '?')}, "
            f"{(data.get('city') or {}).get('country', '?')} "
            f"({max_items} 3-hour slots):"
        ]
        lines.append(f"  {'Time':<20} {'Temp':>10} {'Conditions':<25} Wind")
        for item in items[:max_items]:
            ts = (item.get("dt_txt") or "")[:16]
            temp = (item.get("main") or {}).get("temp", "?")
            weather = (item.get("weather") or [{}])[0]
            wind = (item.get("wind") or {}).get("speed", "?")
            lines.append(
                f"  {ts:<20} {str(temp) + unit_temp:>10} "
                f"{(weather.get('description') or '?')[:25]:<25} {wind}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] weather_forecast: {exc}"


@tool
def weather_air_quality(
    location: Annotated[str, "City name"],
) -> str:
    """Get current air-quality index (AQI) and pollutant concentrations."""
    key, err = _key()
    if err:
        return err
    assert key is not None
    coords, err = _geocode(location, key)
    if err:
        return err
    lat, lon = coords  # type: ignore[misc]
    try:
        r = httpx.get(
            f"{_OWM_BASE}/air_pollution",
            params={"lat": lat, "lon": lon, "appid": key},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        items = d.get("list") or []
        if not items:
            return f"No air-quality data for {location}."
        x = items[0]
        aqi = (x.get("main") or {}).get("aqi", "?")
        aqi_label = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}.get(aqi, "?")
        comp = x.get("components", {}) or {}
        return (
            f"Air quality — {location}:\n"
            f"  AQI: {aqi} ({aqi_label})\n"
            f"  CO:    {comp.get('co', '?')} μg/m³\n"
            f"  NO₂:   {comp.get('no2', '?')} μg/m³\n"
            f"  O₃:    {comp.get('o3', '?')} μg/m³\n"
            f"  SO₂:   {comp.get('so2', '?')} μg/m³\n"
            f"  PM2.5: {comp.get('pm2_5', '?')} μg/m³\n"
            f"  PM10:  {comp.get('pm10', '?')} μg/m³"
        )
    except Exception as exc:
        return f"[tool-error] weather_air_quality: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
