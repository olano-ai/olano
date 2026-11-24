"""FRED tools — Federal Reserve Economic Data (St. Louis Fed).

No API key required for basic usage.
FRED_API_KEY env var for higher rate limits.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_BASE = "https://api.stlouisfed.org/fred"


def _params(**extra) -> dict:
    return {
        "api_key": get_credential("FRED_API_KEY", "abcdefghijklmnopqrstuvwxyz012345"),
        "file_type": "json",
        **extra,
    }


@tool
def fred_get_series(
    series_id: Annotated[str, "FRED series ID (e.g. 'GDP', 'UNRATE', 'CPIAUCSL', 'FEDFUNDS')"],
    observation_start: Annotated[str, "Start date YYYY-MM-DD (optional)"] = "",
    observation_end: Annotated[str, "End date YYYY-MM-DD (optional)"] = "",
    limit: Annotated[int, "Max number of observations to return"] = 10,
) -> str:
    """Get economic data series from FRED (Federal Reserve Economic Data).

    Common series IDs:
    - GDP: Gross Domestic Product
    - UNRATE: Unemployment Rate
    - CPIAUCSL: Consumer Price Index (inflation)
    - FEDFUNDS: Federal Funds Effective Rate
    - DGS10: 10-Year Treasury Rate
    - SP500: S&P 500 Index
    - DTWEXBGS: US Dollar Index
    """
    params = _params(series_id=series_id, sort_order="desc", limit=limit)
    if observation_start:
        params["observation_start"] = observation_start
    if observation_end:
        params["observation_end"] = observation_end

    r = httpx.get(f"{_BASE}/series/observations", params=params, timeout=15)
    r.raise_for_status()
    observations = r.json().get("observations", [])

    if not observations:
        return f"No data found for series {series_id}."

    # Reverse to show chronological order
    observations = list(reversed(observations))
    lines = [f"FRED: {series_id} (latest {len(observations)} observations)"]
    for obs in observations:
        val = obs["value"]
        display = f"{float(val):,.2f}" if val != "." else "N/A"
        lines.append(f"  {obs['date']}: {display}")
    return "\n".join(lines)


@tool
def fred_search(
    query: Annotated[str, "Search query (e.g. 'unemployment rate', 'inflation', 'interest rates')"],
    limit: Annotated[int, "Max results to return"] = 10,
) -> str:
    """Search FRED for economic data series matching a query."""
    r = httpx.get(
        f"{_BASE}/series/search",
        params=_params(search_text=query, limit=limit, order_by="popularity", sort_order="desc"),
        timeout=15,
    )
    r.raise_for_status()
    series_list = r.json().get("seriess", [])
    if not series_list:
        return f"No FRED series found for '{query}'."

    lines = [f"FRED series matching '{query}':"]
    for s in series_list:
        lines.append(
            f"  [{s['id']}] {s['title']}\n"
            f"    Frequency: {s['frequency']} | Units: {s['units']} | Updated: {s['last_updated'][:10]}"
        )
    return "\n".join(lines)


@tool
def fred_get_series_info(
    series_id: Annotated[str, "FRED series ID"],
) -> str:
    """Get metadata for a FRED series (title, units, frequency, notes)."""
    r = httpx.get(
        f"{_BASE}/series",
        params=_params(series_id=series_id),
        timeout=10,
    )
    r.raise_for_status()
    series = r.json().get("seriess", [{}])[0]
    return (
        f"Series: {series.get('id')} — {series.get('title')}\n"
        f"Units: {series.get('units')}\n"
        f"Frequency: {series.get('frequency')}\n"
        f"Seasonal Adjustment: {series.get('seasonal_adjustment')}\n"
        f"Last Updated: {series.get('last_updated', '')[:10]}\n"
        f"Notes: {series.get('notes', '')[:300]}"
    )


@tool
def fred_get_categories(
    category_id: Annotated[int, "Category ID (0 = root)"] = 0,
) -> str:
    """Browse FRED data categories."""
    r = httpx.get(
        f"{_BASE}/category/children",
        params=_params(category_id=category_id),
        timeout=10,
    )
    r.raise_for_status()
    cats = r.json().get("categories", [])
    if not cats:
        return "No subcategories found."
    lines = [f"FRED categories (parent={category_id}):"]
    for c in cats:
        lines.append(f"  [{c['id']}] {c['name']}")
    return "\n".join(lines)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
