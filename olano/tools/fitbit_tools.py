"""Fitbit health tools.

Auth env var: FITBIT_ACCESS_TOKEN (OAuth 2.0).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

FITBIT_BASE = "https://api.fitbit.com/1/user/-"


def fitbit_headers() -> dict:
    """Build the bearer-auth headers for the Fitbit API."""
    token = get_credential("FITBIT_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("[missing-credentials] FITBIT_ACCESS_TOKEN not set in vault.")
    return {"Authorization": f"Bearer {token}"}


@tool
def fitbit_get_activity(
    date_str: Annotated[str, "Date YYYY-MM-DD (default: today)"] = "",
) -> str:
    """Get daily activity summary from Fitbit (steps, calories, distance, floors)."""
    try:
        target_date = date_str or date.today().isoformat()
        r = httpx.get(
            f"{FITBIT_BASE}/activities/date/{target_date}.json",
            headers=fitbit_headers(),
            timeout=15,
        )
        r.raise_for_status()
        summary = r.json().get("summary", {})
        goals = r.json().get("goals", {})
        return (
            f"Fitbit Activity — {target_date}:\n"
            f"  Steps:    {summary.get('steps', 0):,}  / goal {goals.get('steps', '?'):,}\n"
            f"  Calories: {summary.get('caloriesOut', 0):,} kcal\n"
            f"  Distance: {summary.get('distances', [{}])[0].get('distance', 0):.2f} km\n"
            f"  Floors:   {summary.get('floors', 0)}\n"
            f"  Active min: {summary.get('fairlyActiveMinutes', 0) + summary.get('veryActiveMinutes', 0)} min\n"
            f"  Sedentary: {summary.get('sedentaryMinutes', 0)} min"
        )
    except Exception as e:
        return f"[tool-error] fitbit: {e}"


@tool
def fitbit_get_sleep(
    date_str: Annotated[str, "Date YYYY-MM-DD (default: today)"] = "",
) -> str:
    """Get sleep data from Fitbit for a given date."""
    try:
        target_date = date_str or date.today().isoformat()
        r = httpx.get(
            f"{FITBIT_BASE}/sleep/date/{target_date}.json",
            headers=fitbit_headers(),
            timeout=15,
        )
        r.raise_for_status()
        sleep_data = r.json().get("sleep", [])
        summary = r.json().get("summary", {})
        if not sleep_data:
            return f"No sleep data for {target_date}."

        total_min = summary.get("totalMinutesAsleep", 0)
        total_hours = total_min / 60
        return (
            f"Fitbit Sleep — {target_date}:\n"
            f"  Total sleep: {total_hours:.1f} h ({total_min} min)\n"
            f"  Time in bed: {summary.get('totalTimeInBed', 0)} min\n"
            f"  Deep:  {summary.get('stages', {}).get('deep', 0)} min\n"
            f"  Light: {summary.get('stages', {}).get('light', 0)} min\n"
            f"  REM:   {summary.get('stages', {}).get('rem', 0)} min\n"
            f"  Wake:  {summary.get('stages', {}).get('wake', 0)} min"
        )
    except Exception as e:
        return f"[tool-error] fitbit: {e}"


@tool
def fitbit_get_heart_rate(
    date_str: Annotated[str, "Date YYYY-MM-DD (default: today)"] = "",
) -> str:
    """Get resting heart rate and heart rate zones from Fitbit."""
    try:
        target_date = date_str or date.today().isoformat()
        r = httpx.get(
            f"{FITBIT_BASE}/activities/heart/date/{target_date}/1d.json",
            headers=fitbit_headers(),
            timeout=15,
        )
        r.raise_for_status()
        hr_data = r.json().get("activities-heart", [{}])[0].get("value", {})
        resting = hr_data.get("restingHeartRate", "N/A")
        zones = hr_data.get("heartRateZones", [])
        lines = [f"Fitbit Heart Rate — {target_date}:", f"  Resting HR: {resting} bpm"]
        for zone in zones:
            lines.append(
                f"  {zone['name']:15s}: {zone.get('minutes', 0)} min  "
                f"({zone.get('min', '?')}-{zone.get('max', '?')} bpm)"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] fitbit: {e}"


@tool
def fitbit_get_weight(
    period: Annotated[str, "Time period: 1d, 7d, 30d, 3m"] = "30d",
) -> str:
    """Get weight log from Fitbit."""
    try:
        end = date.today().isoformat()
        r = httpx.get(
            f"{FITBIT_BASE}/body/weight/date/{end}/{period}.json",
            headers=fitbit_headers(),
            timeout=15,
        )
        r.raise_for_status()
        entries = r.json().get("body-weight", [])
        if not entries:
            return "No weight data found."
        lines = [f"Fitbit weight ({period}):"]
        for e in entries[-10:]:
            lines.append(f"  {e['dateTime']}: {e['value']} kg")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] fitbit: {e}"


get_tools, get_tool = make_toolkit_api(globals())
