"""Garmin Connect tools.

Auth env vars: GARMIN_EMAIL, GARMIN_PASSWORD (read via garminconnect library).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def garmin_get_daily_summary(
    date_str: Annotated[str, "Date YYYY-MM-DD (default: today)"] = "",
) -> str:
    """Get Garmin Connect daily activity summary."""
    from olano.tools._install_hint import optional_import

    garmin_mod, err = optional_import("garminconnect", purpose="Garmin Connect access")
    if err:
        return err
    assert garmin_mod is not None

    email = get_credential("GARMIN_EMAIL", "")
    password = get_credential("GARMIN_PASSWORD", "")
    if not email or not password:
        return "[missing-credentials] GARMIN_EMAIL and GARMIN_PASSWORD must be set in vault."

    try:
        target_date = date_str or date.today().isoformat()
        api = garmin_mod.Garmin(email, password)
        api.login()
        data = api.get_stats(target_date)

        return (
            f"Garmin Daily Summary — {target_date}:\n"
            f"  Steps:       {data.get('totalSteps', 0):,}\n"
            f"  Calories:    {data.get('totalKilocalories', 0):,} kcal\n"
            f"  Active cal:  {data.get('activeKilocalories', 0):,} kcal\n"
            f"  Distance:    {data.get('totalDistanceMeters', 0) / 1000:.2f} km\n"
            f"  Floors:      {data.get('floorsAscended', 0)}\n"
            f"  Resting HR:  {data.get('restingHeartRate', 'N/A')} bpm\n"
            f"  Avg stress:  {data.get('averageStressLevel', 'N/A')}\n"
            f"  Body battery:{data.get('bodyBatteryMostRecentValue', 'N/A')}"
        )
    except Exception as e:
        return f"[tool-error] garmin_get_daily_summary: {e}"


get_tools, get_tool = make_toolkit_api(globals())
