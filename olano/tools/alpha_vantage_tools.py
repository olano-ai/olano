"""Alpha Vantage toolkit — macro, forex, commodities, economic indicators.

Auth env var: ALPHAVANTAGE_API_KEY (free tier at alphavantage.co).

Free tier: 25 requests/day, 5 req/min. Direct HTTP — no SDK needed.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_AV_BASE = "https://www.alphavantage.co/query"
_AV_CREDS_MSG = "[missing-credentials] ALPHAVANTAGE_API_KEY not set in vault."


def _call(params: dict[str, Any]) -> tuple[dict | None, str | None]:
    key = get_credential("ALPHAVANTAGE_API_KEY") or get_credential("ALPHA_VANTAGE_API_KEY")
    if not key:
        return None, _AV_CREDS_MSG
    try:
        r = httpx.get(_AV_BASE, params={**params, "apikey": key}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            if "Error Message" in data:
                return None, f"[tool-error] Alpha Vantage: {data['Error Message']}"
            if "Note" in data:
                return None, f"[tool-error] Alpha Vantage rate-limited: {data['Note']}"
            if "Information" in data and not any(k for k in data if k not in {"Information"}):
                return None, f"[tool-error] Alpha Vantage: {data['Information']}"
        return data, None
    except Exception as exc:
        return None, f"[tool-error] Alpha Vantage: {exc}"


@tool
def av_get_intraday(
    symbol: Annotated[str, "Ticker symbol"],
    interval: Annotated[str, "1min, 5min, 15min, 30min, 60min"] = "5min",
    output_size: Annotated[str, "'compact' (100 latest) or 'full' (full day)"] = "compact",
) -> str:
    """Get intraday OHLCV bars for a US equity."""
    data, err = _call(
        {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol.upper(),
            "interval": interval,
            "outputsize": output_size,
        }
    )
    if err:
        return err
    series_key = f"Time Series ({interval})"
    series = (data or {}).get(series_key) or {}
    if not series:
        return f"No intraday data for {symbol.upper()} ({interval})."
    items = sorted(series.items(), reverse=True)[:50]
    lines = [f"{symbol.upper()} intraday {interval} ({len(items)} bars, newest first):"]
    lines.append(
        f"  {'Time':<20} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for ts, bar in items:
        lines.append(
            f"  {ts:<20} {float(bar.get('1. open', 0)):>10.4f} "
            f"{float(bar.get('2. high', 0)):>10.4f} "
            f"{float(bar.get('3. low', 0)):>10.4f} "
            f"{float(bar.get('4. close', 0)):>10.4f} "
            f"{float(bar.get('5. volume', 0)):>14,.0f}"
        )
    return "\n".join(lines)


@tool
def av_get_fx_rate(
    from_currency: Annotated[str, "From currency ISO code (e.g. USD)"],
    to_currency: Annotated[str, "To currency ISO code (e.g. EUR)"],
) -> str:
    """Get the current exchange rate for a currency pair (fiat or crypto)."""
    data, err = _call(
        {
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
        }
    )
    if err:
        return err
    r = (data or {}).get("Realtime Currency Exchange Rate") or {}
    if not r:
        return f"No FX rate for {from_currency.upper()}/{to_currency.upper()}."
    return (
        f"{from_currency.upper()}/{to_currency.upper()} exchange rate:\n"
        f"  Rate:     {r.get('5. Exchange Rate', '?')}\n"
        f"  Bid:      {r.get('8. Bid Price', '?')}\n"
        f"  Ask:      {r.get('9. Ask Price', '?')}\n"
        f"  As of:    {r.get('6. Last Refreshed', '?')} {r.get('7. Time Zone', '?')}"
    )


@tool
def av_get_commodity(
    commodity: Annotated[
        str,
        "Commodity: WTI, BRENT, NATURAL_GAS, COPPER, ALUMINUM, WHEAT, CORN, COTTON, "
        "SUGAR, COFFEE, ALL_COMMODITIES",
    ] = "WTI",
    interval: Annotated[str, "daily, weekly, monthly"] = "monthly",
) -> str:
    """Get historical commodity price series."""
    data, err = _call({"function": commodity.upper(), "interval": interval})
    if err:
        return err
    series = (data or {}).get("data") or []
    if not series:
        return f"No data for commodity {commodity}."
    name = (data or {}).get("name", commodity)
    unit = (data or {}).get("unit", "")
    lines = [f"{name} ({unit}) — {interval}, showing {min(30, len(series))} recent:"]
    for row in series[:30]:
        lines.append(f"  {row.get('date', '?')}  {row.get('value', '?')}")
    return "\n".join(lines)


@tool
def av_get_economic_indicator(
    indicator: Annotated[
        str,
        "Indicator: REAL_GDP, REAL_GDP_PER_CAPITA, TREASURY_YIELD, FEDERAL_FUNDS_RATE, "
        "CPI, INFLATION, RETAIL_SALES, DURABLES, UNEMPLOYMENT, NONFARM_PAYROLL",
    ] = "CPI",
    interval: Annotated[str, "monthly, quarterly, annual (depends on indicator)"] = "monthly",
    maturity: Annotated[
        str, "For TREASURY_YIELD: 3month, 2year, 5year, 7year, 10year, 30year"
    ] = "",
) -> str:
    """Get a US economic indicator time series."""
    params: dict[str, Any] = {"function": indicator.upper(), "interval": interval}
    if maturity:
        params["maturity"] = maturity
    data, err = _call(params)
    if err:
        return err
    series = (data or {}).get("data") or []
    if not series:
        return f"No data for indicator {indicator}."
    name = (data or {}).get("name", indicator)
    unit = (data or {}).get("unit", "")
    lines = [f"{name} ({unit}) — {interval}, showing {min(30, len(series))} recent:"]
    for row in series[:30]:
        lines.append(f"  {row.get('date', '?')}  {row.get('value', '?')}")
    return "\n".join(lines)


@tool
def av_get_company_overview(
    symbol: Annotated[str, "Ticker symbol (US-listed equity)"],
) -> str:
    """Get company fundamentals overview (P/E, market cap, dividend yield, sector, etc.)."""
    data, err = _call({"function": "OVERVIEW", "symbol": symbol.upper()})
    if err:
        return err
    if not data or not data.get("Symbol"):
        return f"No company overview for {symbol.upper()}."
    return (
        f"{data.get('Symbol')} — {data.get('Name', '?')}\n"
        f"  Sector: {data.get('Sector', '?')} | Industry: {data.get('Industry', '?')}\n"
        f"  Exchange: {data.get('Exchange', '?')} | Country: {data.get('Country', '?')}\n"
        f"  Market cap: {data.get('MarketCapitalization', '?')}\n"
        f"  P/E: {data.get('PERatio', '?')} | PEG: {data.get('PEGRatio', '?')} | "
        f"P/B: {data.get('PriceToBookRatio', '?')}\n"
        f"  Dividend yield: {data.get('DividendYield', '?')}\n"
        f"  EPS: {data.get('EPS', '?')} | Beta: {data.get('Beta', '?')}\n"
        f"  52w high/low: {data.get('52WeekHigh', '?')} / {data.get('52WeekLow', '?')}\n"
        f"  Analyst target: {data.get('AnalystTargetPrice', '?')}\n"
        f"  Description: {(data.get('Description') or '')[:400]}"
    )


get_tools, get_tool = make_toolkit_api(globals())
