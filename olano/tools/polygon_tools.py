"""Polygon.io market data toolkit.

Auth env var: POLYGON_API_KEY (free tier at polygon.io).

Uses direct HTTP rather than polygon-api-client so we don't add another optional
dep. All endpoints are read-only.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_POLYGON_CREDS_MSG = "[missing-credentials] POLYGON_API_KEY not set in vault."


def _params(**extra: Any) -> tuple[dict | None, str | None]:
    key = get_credential("POLYGON_API_KEY")
    if not key:
        return None, _POLYGON_CREDS_MSG
    return {"apiKey": key, **{k: v for k, v in extra.items() if v not in (None, "", 0)}}, None


def _get(path: str, **extra: Any) -> tuple[Any, str | None]:
    params, err = _params(**extra)
    if err:
        return None, err
    try:
        r = httpx.get(f"{_POLYGON_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return None, f"[tool-error] Polygon {path}: {exc}"


@tool
def polygon_get_aggregates(
    symbol: Annotated[str, "Ticker symbol (e.g. AAPL)"],
    multiplier: Annotated[int, "Size of timespan multiplier (e.g. 1 for 1-day bars)"] = 1,
    timespan: Annotated[str, "minute, hour, day, week, month, quarter, year"] = "day",
    from_date: Annotated[str, "Start YYYY-MM-DD"] = "",
    to_date: Annotated[str, "End YYYY-MM-DD"] = "",
    limit: Annotated[int, "Max bars (max 50000)"] = 120,
    adjusted: Annotated[bool, "Apply split/dividend adjustments"] = True,
) -> str:
    """Get OHLCV aggregate bars for a symbol."""
    if not from_date or not to_date:
        from datetime import date, timedelta

        today = date.today()
        from_date = from_date or (today - timedelta(days=365)).isoformat()
        to_date = to_date or today.isoformat()
    sym = symbol.upper()
    data, err = _get(
        f"/v2/aggs/ticker/{sym}/range/{multiplier}/{timespan}/{from_date}/{to_date}",
        adjusted=str(adjusted).lower(),
        sort="asc",
        limit=min(limit, 50000),
    )
    if err:
        return err
    results = (data or {}).get("results", []) or []
    if not results:
        return f"No bars for {sym} in range {from_date} → {to_date}."
    import datetime as _dt

    lines = [f"{sym} {multiplier}{timespan} bars ({from_date} → {to_date}, {len(results)}):"]
    lines.append(
        f"  {'Time':<20} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for b in results[-limit:]:
        t_ms = b.get("t", 0)
        ts = _dt.datetime.utcfromtimestamp(t_ms / 1000).isoformat(timespec="seconds")
        lines.append(
            f"  {ts:<20} {b.get('o', 0):>10.4f} {b.get('h', 0):>10.4f} "
            f"{b.get('l', 0):>10.4f} {b.get('c', 0):>10.4f} {b.get('v', 0):>14,.0f}"
        )
    return "\n".join(lines)


@tool
def polygon_get_last_trade(
    symbol: Annotated[str, "Ticker symbol"],
) -> str:
    """Get the most recent trade for a US-listed equity."""
    sym = symbol.upper()
    data, err = _get(f"/v2/last/trade/{sym}")
    if err:
        return err
    r = (data or {}).get("results", {}) or {}
    price = r.get("p", "?")
    size = r.get("s", "?")
    ts = r.get("t", "?")
    exchange = r.get("x", "?")
    return f"{sym} last trade: ${price} x {size} (exchange={exchange}) @ {ts}"


@tool
def polygon_get_last_quote(
    symbol: Annotated[str, "Ticker symbol"],
) -> str:
    """Get the latest NBBO bid/ask quote for a US-listed equity."""
    sym = symbol.upper()
    data, err = _get(f"/v2/last/nbbo/{sym}")
    if err:
        return err
    r = (data or {}).get("results", {}) or {}
    bid = r.get("p", "?")
    bid_size = r.get("s", "?")
    ask = r.get("P", "?")
    ask_size = r.get("S", "?")
    ts = r.get("t", "?")
    return (
        f"{sym} NBBO quote:\n"
        f"  Bid: ${bid} x {bid_size}\n"
        f"  Ask: ${ask} x {ask_size}\n"
        f"  Timestamp: {ts}"
    )


@tool
def polygon_get_ticker_details(
    symbol: Annotated[str, "Ticker symbol"],
) -> str:
    """Get reference data for a ticker (company name, market cap, sector, exchange)."""
    sym = symbol.upper()
    data, err = _get(f"/v3/reference/tickers/{sym}")
    if err:
        return err
    r = (data or {}).get("results", {}) or {}
    if not r:
        return f"No details for {sym}."
    return (
        f"{sym}: {r.get('name', '?')}\n"
        f"  Market: {r.get('market', '?')} | Type: {r.get('type', '?')}\n"
        f"  Exchange: {r.get('primary_exchange', '?')}\n"
        f"  Currency: {r.get('currency_name', '?')}\n"
        f"  Locale: {r.get('locale', '?')}\n"
        f"  CIK: {r.get('cik', '?')} | Composite FIGI: {r.get('composite_figi', '?')}\n"
        f"  Market cap: {r.get('market_cap', '?')}\n"
        f"  Shares outstanding: {r.get('share_class_shares_outstanding', '?')}\n"
        f"  Description: {(r.get('description') or '')[:400]}\n"
        f"  Website: {r.get('homepage_url', '?')}"
    )


@tool
def polygon_list_news(
    symbol: Annotated[str, "Ticker symbol (empty = market-wide)"] = "",
    limit: Annotated[int, "Max articles (max 1000)"] = 20,
    published_utc_gte: Annotated[str, "Min published date YYYY-MM-DD (optional)"] = "",
) -> str:
    """Get recent financial news, optionally filtered by ticker."""
    kwargs: dict[str, Any] = {"limit": min(limit, 1000), "order": "desc"}
    if symbol:
        kwargs["ticker"] = symbol.upper()
    if published_utc_gte:
        kwargs["published_utc.gte"] = published_utc_gte
    data, err = _get("/v2/reference/news", **kwargs)
    if err:
        return err
    articles = (data or {}).get("results", []) or []
    if not articles:
        return f"No news for filter symbol='{symbol}'."
    lines = [f"Polygon news ({len(articles)}):"]
    for a in articles:
        published = (a.get("published_utc") or "")[:19]
        publisher = (a.get("publisher") or {}).get("name", "?")
        tickers = ",".join(a.get("tickers", []) or [])
        lines.append(
            f"  [{published}] {publisher} | {tickers} | {a.get('title', '?')}\n"
            f"    {a.get('article_url', '')}"
        )
    return "\n".join(lines)


@tool
def polygon_get_snapshot(
    symbol: Annotated[str, "Ticker symbol"],
) -> str:
    """Get a market snapshot for a single ticker (day, prev day, min bar, last quote/trade)."""
    sym = symbol.upper()
    data, err = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
    if err:
        return err
    t = (data or {}).get("ticker", {}) or {}
    if not t:
        return f"No snapshot for {sym}."
    day = t.get("day", {}) or {}
    prev = t.get("prevDay", {}) or {}
    last_trade = t.get("lastTrade", {}) or {}
    last_quote = t.get("lastQuote", {}) or {}
    change = t.get("todaysChange", "?")
    change_pct = t.get("todaysChangePerc", "?")
    return (
        f"{sym} snapshot:\n"
        f"  Last trade: ${last_trade.get('p', '?')} @ {last_trade.get('t', '?')}\n"
        f"  Bid/Ask: ${last_quote.get('p', '?')} / ${last_quote.get('P', '?')}\n"
        f"  Day:  O={day.get('o', '?')} H={day.get('h', '?')} L={day.get('l', '?')} "
        f"C={day.get('c', '?')} V={day.get('v', '?')}\n"
        f"  Prev: O={prev.get('o', '?')} H={prev.get('h', '?')} L={prev.get('l', '?')} "
        f"C={prev.get('c', '?')} V={prev.get('v', '?')}\n"
        f"  Today's change: {change} ({change_pct}%)"
    )


get_tools, get_tool = make_toolkit_api(globals())
