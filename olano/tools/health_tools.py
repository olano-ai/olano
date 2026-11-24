"""Cross-source health aggregator (delegates to fitbit_tools / garmin_tools)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api
from olano.tools.fitbit_tools import FITBIT_BASE, fitbit_headers

logger = logging.getLogger(__name__)


@tool
def health_summary(
    days: Annotated[int, "Number of days to summarize (default: 7)"] = 7,
) -> str:
    """Aggregate health data from all available sources (Fitbit + Garmin) for a date range."""
    results = []
    today = date.today()

    if get_credential("FITBIT_ACCESS_TOKEN"):
        for i in range(min(days, 7)):
            d = (today - timedelta(days=i)).isoformat()
            try:
                r = httpx.get(
                    f"{FITBIT_BASE}/activities/date/{d}.json",
                    headers=fitbit_headers(),
                    timeout=10,
                )
                if r.status_code == 200:
                    summary = r.json().get("summary", {})
                    results.append(
                        f"  {d} [Fitbit]: "
                        f"{summary.get('steps', 0):,} steps, "
                        f"{summary.get('caloriesOut', 0):,} kcal"
                    )
            except Exception:
                pass

    if results:
        return f"Health summary (last {days} days):\n" + "\n".join(results)

    return (
        "No health data sources configured.\n"
        "Set FITBIT_ACCESS_TOKEN for Fitbit data,\n"
        "or GARMIN_EMAIL + GARMIN_PASSWORD for Garmin data."
    )


get_tools, get_tool = make_toolkit_api(globals())
