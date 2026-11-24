"""dYdX v4 (Cosmos chain) read-only tools — markets, candles, orderbook, positions.

Auth: none. dYdX v4 trading requires signing Cosmos transactions with a wallet
mnemonic, which is too sensitive to wire through a generic agent toolkit.
This module exposes only public *indexer* endpoints, plus address-keyed
account/position/order lookups.

Hosts:
  Mainnet indexer: https://indexer.dydx.trade
  Testnet indexer: https://dydx-testnet.imperator.co (community-run)

Each tool accepts ``mode='mainnet'`` (default) or ``mode='testnet'``.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_MAINNET = "https://indexer.dydx.trade"
_TESTNET = "https://dydx-testnet.imperator.co"


def _host(mode: str) -> str:
    return _TESTNET if (mode or "mainnet").lower() == "testnet" else _MAINNET


def _label(mode: str) -> str:
    return "TESTNET" if (mode or "mainnet").lower() == "testnet" else "mainnet"


def _get(path: str, params: dict | None, mode: str) -> dict:
    r = httpx.get(f"{_host(mode)}{path}", params=params or {}, timeout=15)
    r.raise_for_status()
    return cast(dict, r.json())


@tool
def dydx_list_markets(mode: str = "mainnet") -> str:
    """List all dYdX perpetual markets with current prices and 24h stats."""
    try:
        data = _get("/v4/perpetualMarkets", None, mode)
    except Exception as e:
        return f"[tool-error] dydx_list_markets: {e}"
    markets = data.get("markets", {})
    if not markets:
        return f"No markets found [{_label(mode)}]."
    lines = [f"dYdX perpetual markets [{_label(mode)}] ({len(markets)}):"]
    lines.append(f"  {'Ticker':<16} {'Price':>14} {'24h %':>9} {'Vol 24h (USD)':>18} {'OI':>14}")
    for m in sorted(markets.values(), key=lambda x: float(x.get("volume24H") or 0), reverse=True)[
        :30
    ]:
        try:
            pct = float(m.get("priceChange24H") or 0) / float(m.get("oraclePrice") or 1) * 100
        except (ValueError, ZeroDivisionError):
            pct = 0
        lines.append(
            f"  {m.get('ticker', '?'):<16} {m.get('oraclePrice', '?'):>14} "
            f"{pct:>+8.2f}% {float(m.get('volume24H') or 0):>18,.0f} "
            f"{float(m.get('openInterest') or 0):>14,.2f}"
        )
    return "\n".join(lines)


@tool
def dydx_get_market(
    ticker: Annotated[str, "Market ticker (e.g. BTC-USD, ETH-USD)"],
    mode: str = "mainnet",
) -> str:
    """Get details for a single dYdX perpetual market."""
    try:
        data = _get("/v4/perpetualMarkets", {"ticker": ticker.upper()}, mode)
    except Exception as e:
        return f"[tool-error] dydx_get_market({ticker}): {e}"
    m = data.get("markets", {}).get(ticker.upper())
    if not m:
        return f"No market data for {ticker}."
    return (
        f"{m.get('ticker')} [{_label(mode)}]\n"
        f"  Oracle price: {m.get('oraclePrice')}\n"
        f"  24h change:   {m.get('priceChange24H')}\n"
        f"  24h volume:   ${float(m.get('volume24H') or 0):,.0f}\n"
        f"  Open interest: {m.get('openInterest')}\n"
        f"  Funding rate (1h): {m.get('nextFundingRate')}\n"
        f"  Tick size: {m.get('tickSize')}  Step size: {m.get('stepSize')}\n"
        f"  Status: {m.get('status')}"
    )


@tool
def dydx_get_candles(
    ticker: Annotated[str, "Market ticker (e.g. BTC-USD)"],
    resolution: Annotated[str, "1MIN, 5MINS, 15MINS, 30MINS, 1HOUR, 4HOURS, 1DAY"] = "1HOUR",
    limit: Annotated[int, "Number of candles (max 100)"] = 30,
    mode: str = "mainnet",
) -> str:
    """Get OHLCV candles for a dYdX market."""
    try:
        data = _get(
            f"/v4/candles/perpetualMarkets/{ticker.upper()}",
            {"resolution": resolution, "limit": min(limit, 100)},
            mode,
        )
    except Exception as e:
        return f"[tool-error] dydx_get_candles({ticker}): {e}"
    rows = data.get("candles", [])
    if not rows:
        return f"No candles for {ticker}."
    lines = [f"{ticker.upper()} {resolution} candles ({len(rows)}) [{_label(mode)}]:"]
    lines.append(
        f"  {'Time':<19} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for k in reversed(rows):
        ts = k.get("startedAt", "?")[:19]
        lines.append(
            f"  {ts:<19} {float(k.get('open') or 0):>10.4f} {float(k.get('high') or 0):>10.4f} "
            f"{float(k.get('low') or 0):>10.4f} {float(k.get('close') or 0):>10.4f} "
            f"{float(k.get('baseTokenVolume') or 0):>14.4f}"
        )
    return "\n".join(lines)


@tool
def dydx_get_orderbook(
    ticker: Annotated[str, "Market ticker (e.g. BTC-USD)"],
    depth: Annotated[int, "Number of bid/ask levels to show"] = 10,
    mode: str = "mainnet",
) -> str:
    """Get the L2 order book for a dYdX market."""
    try:
        data = _get(f"/v4/orderbooks/perpetualMarkets/{ticker.upper()}", None, mode)
    except Exception as e:
        return f"[tool-error] dydx_get_orderbook({ticker}): {e}"
    bids = data.get("bids", [])[:depth]
    asks = data.get("asks", [])[:depth]
    lines = [f"{ticker.upper()} order book [{_label(mode)}]:"]
    lines.append(f"  {'Bid Price':>14} {'Bid Size':>14}     {'Ask Size':>14} {'Ask Price':>14}")
    for i in range(max(len(bids), len(asks))):
        b = bids[i] if i < len(bids) else {}
        a = asks[i] if i < len(asks) else {}
        lines.append(
            f"  {b.get('price', ''):>14} {b.get('size', ''):>14}     "
            f"{a.get('size', ''):>14} {a.get('price', ''):>14}"
        )
    return "\n".join(lines)


@tool
def dydx_get_address_subaccounts(
    address: Annotated[str, "dYdX/Cosmos address (dydx1...)"],
    mode: str = "mainnet",
) -> str:
    """Get subaccounts (equity, free collateral, positions) for a dYdX address."""
    try:
        data = _get(f"/v4/addresses/{address}", None, mode)
    except Exception as e:
        return f"[tool-error] dydx_get_address_subaccounts: {e}"
    subs = data.get("subaccounts", [])
    if not subs:
        return f"No subaccounts found for {address} [{_label(mode)}]."
    lines = [f"dYdX subaccounts for {address} [{_label(mode)}]:"]
    for s in subs:
        positions = s.get("openPerpetualPositions", {})
        lines.append(
            f"  Subaccount #{s.get('subaccountNumber')}\n"
            f"    Equity: ${float(s.get('equity') or 0):,.2f}\n"
            f"    Free collateral: ${float(s.get('freeCollateral') or 0):,.2f}\n"
            f"    Open positions: {len(positions)}"
        )
        for ticker, p in positions.items():
            side = p.get("side", "?")
            size = p.get("size", "?")
            entry = p.get("entryPrice", "?")
            unrealized = p.get("unrealizedPnl", "?")
            lines.append(f"      {ticker}: {side} {size} @ {entry}  Unrealized: ${unrealized}")
    return "\n".join(lines)


@tool
def dydx_get_address_orders(
    address: Annotated[str, "dYdX/Cosmos address (dydx1...)"],
    subaccount_number: Annotated[int, "Subaccount number (default 0)"] = 0,
    mode: str = "mainnet",
) -> str:
    """Get recent orders for a dYdX subaccount."""
    try:
        data = _get(
            "/v4/orders",
            {"address": address, "subaccountNumber": subaccount_number, "limit": 30},
            mode,
        )
    except Exception as e:
        return f"[tool-error] dydx_get_address_orders: {e}"
    orders = data if isinstance(data, list) else data.get("orders", [])
    if not orders:
        return f"No orders found for {address} #{subaccount_number} [{_label(mode)}]."
    lines = [f"dYdX orders for {address} #{subaccount_number} ({len(orders)}):"]
    for o in orders[:25]:
        lines.append(
            f"  [{o.get('id', '')[:8]}] {o.get('ticker')} {o.get('side')} {o.get('type')} "
            f"size={o.get('size')}  price={o.get('price')}  status={o.get('status')}"
        )
    return "\n".join(lines)


@tool
def dydx_get_recent_trades(
    ticker: Annotated[str, "Market ticker"],
    limit: Annotated[int, "Number of trades (max 100)"] = 20,
    mode: str = "mainnet",
) -> str:
    """Get recent public trades for a dYdX market."""
    try:
        data = _get(
            f"/v4/trades/perpetualMarket/{ticker.upper()}",
            {"limit": min(limit, 100)},
            mode,
        )
    except Exception as e:
        return f"[tool-error] dydx_get_recent_trades({ticker}): {e}"
    rows = data.get("trades", [])[:limit]
    if not rows:
        return f"No trades for {ticker}."
    lines = [f"{ticker.upper()} recent trades [{_label(mode)}] ({len(rows)}):"]
    for t in rows:
        ts = t.get("createdAt", "?")[:19]
        lines.append(
            f"  {ts}  {t.get('side', '?'):<4} {t.get('size', '?'):>12} @ {t.get('price', '?'):>12}"
        )
    return "\n".join(lines)


# Suppress unused import warning for `time` in case it gets used later
_ = time


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
