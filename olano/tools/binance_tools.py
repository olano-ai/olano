"""Binance Spot tools — market data + trading via the public REST API.

Safety
------
**All tools default to ``mode='testnet'``** (paper trading, fake balances).
To trade real funds, the caller must explicitly pass ``mode='live'`` AND have
live credentials configured.

Auth env vars (live):
  BINANCE_API_KEY    / BINANCE_API_SECRET
Auth env vars (testnet):
  BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
Fallback for either:
  BINANCE_API_KEY / BINANCE_API_SECRET (used when the mode-specific pair is unset)

Testnet base: https://testnet.binance.vision  (sign up free with GitHub)
Live base:    https://api.binance.com

Note: Public market-data tools (``binance_get_ticker``, ``binance_get_klines``)
also default to testnet for consistency. Pass ``mode='live'`` to read real
production prices.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Annotated, Any, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_LIVE_HOST = "https://api.binance.com"
_TEST_HOST = "https://testnet.binance.vision"


def _binance_creds(mode: str) -> tuple[str, str, str]:
    """Return (host, api_key, api_secret) for a given mode."""
    m = (mode or "live").strip().lower()
    if m == "testnet":
        host = _TEST_HOST
        key = get_credential("BINANCE_TESTNET_API_KEY") or get_credential("BINANCE_API_KEY", "")
        secret = get_credential("BINANCE_TESTNET_API_SECRET") or get_credential(
            "BINANCE_API_SECRET", ""
        )
    else:
        host = _LIVE_HOST
        key = get_credential("BINANCE_API_KEY", "")
        secret = get_credential("BINANCE_API_SECRET", "")
    return host, key, secret


def _signed_request(method: str, path: str, params: dict | None, mode: str) -> dict:
    host, key, secret = _binance_creds(mode)
    if not key or not secret:
        raise RuntimeError(
            "[missing-credentials] Binance keys missing. Set BINANCE_API_KEY / "
            "BINANCE_API_SECRET (or BINANCE_TESTNET_API_KEY / _SECRET for mode='testnet')."
        )
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    p["signature"] = sig
    url = f"{host}{path}"
    headers = {"X-MBX-APIKEY": key}
    if method == "GET":
        r = httpx.get(url, headers=headers, params=p, timeout=15)
    elif method == "POST":
        r = httpx.post(url, headers=headers, params=p, timeout=20)
    elif method == "DELETE":
        r = httpx.delete(url, headers=headers, params=p, timeout=15)
    else:
        raise ValueError(f"Unsupported method: {method}")
    r.raise_for_status()
    return cast(dict, r.json())


def _public_get(path: str, params: dict | None, mode: str) -> Any:
    host, _, _ = _binance_creds(mode)
    r = httpx.get(f"{host}{path}", params=params or {}, timeout=10)
    r.raise_for_status()
    return r.json()


def _label(mode: str) -> str:
    return "TESTNET" if (mode or "live").lower() == "testnet" else "live"


@tool
def binance_get_account(mode: str = "testnet") -> str:
    """Get Binance spot account balances. mode='testnet' (default, paper) or 'live'."""
    try:
        data = _signed_request("GET", "/api/v3/account", None, mode)
    except Exception as e:
        return f"[tool-error] binance_get_account: {e}"
    balances = [b for b in data.get("balances", []) if float(b["free"]) + float(b["locked"]) > 0]
    if not balances:
        return f"No non-zero balances [{_label(mode)}]."
    lines = [f"Binance balances [{_label(mode)}]:"]
    lines.append(f"  {'Asset':<8} {'Free':>16} {'Locked':>16}")
    for b in balances:
        lines.append(f"  {b['asset']:<8} {float(b['free']):>16.8f} {float(b['locked']):>16.8f}")
    return "\n".join(lines)


@tool
def binance_get_ticker(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT, ETHUSDT)"],
    mode: str = "testnet",
) -> str:
    """Get the latest 24h ticker for a Binance symbol."""
    try:
        data = _public_get("/api/v3/ticker/24hr", {"symbol": symbol.upper()}, mode)
    except Exception as e:
        return f"[tool-error] binance_get_ticker({symbol}): {e}"
    return (
        f"{symbol.upper()} [{_label(mode)}]\n"
        f"  Last: {data.get('lastPrice')}\n"
        f"  24h change: {data.get('priceChangePercent')}%\n"
        f"  24h high: {data.get('highPrice')}  low: {data.get('lowPrice')}\n"
        f"  Volume: {data.get('volume')} {symbol[:-4] if symbol.upper().endswith('USDT') else ''}\n"
        f"  Bid/Ask: {data.get('bidPrice')} / {data.get('askPrice')}"
    )


@tool
def binance_get_klines(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT)"],
    interval: Annotated[str, "Candle interval: 1m,5m,15m,1h,4h,1d,1w"] = "1d",
    limit: Annotated[int, "Number of candles (max 1000)"] = 30,
    mode: str = "testnet",
) -> str:
    """Get OHLCV klines for a Binance symbol."""
    try:
        data = _public_get(
            "/api/v3/klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)},
            mode,
        )
    except Exception as e:
        return f"[tool-error] binance_get_klines({symbol}): {e}"
    if not data:
        return f"No kline data for {symbol}."
    lines = [f"{symbol.upper()} klines ({interval}, {len(data)}) [{_label(mode)}]:"]
    lines.append(
        f"  {'Time':<19} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for k in data:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(k[0] / 1000))
        lines.append(
            f"  {ts:<19} {float(k[1]):>10.4f} {float(k[2]):>10.4f} "
            f"{float(k[3]):>10.4f} {float(k[4]):>10.4f} {float(k[5]):>14.4f}"
        )
    return "\n".join(lines)


@tool
def binance_list_open_orders(
    symbol: Annotated[str, "Trading pair filter (optional, returns all if empty)"] = "",
    mode: str = "testnet",
) -> str:
    """List open Binance orders. mode='testnet' (default, paper) or 'live'."""
    params = {"symbol": symbol.upper()} if symbol else {}
    try:
        orders = _signed_request("GET", "/api/v3/openOrders", params, mode)
    except Exception as e:
        return f"[tool-error] binance_list_open_orders: {e}"
    if not orders:
        return f"No open orders [{_label(mode)}]."
    lines = [f"Binance open orders [{_label(mode)}] ({len(orders)}):"]
    for o in orders:
        lines.append(
            f"  [{o.get('orderId')}] {o.get('symbol')} {o.get('side')} {o.get('type')}  "
            f"qty={o.get('origQty')}  price={o.get('price')}  status={o.get('status')}"
        )
    return "\n".join(lines)


@tool
def binance_place_order(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT)"],
    side: Annotated[str, "'BUY' or 'SELL'"],
    quantity: Annotated[float, "Base-asset quantity"],
    order_type: Annotated[str, "'MARKET' or 'LIMIT'"] = "MARKET",
    price: Annotated[float, "Required for LIMIT orders"] = 0.0,
    time_in_force: Annotated[str, "GTC, IOC, FOK (LIMIT only)"] = "GTC",
    mode: str = "testnet",
) -> str:
    """Place a Binance spot order. mode='testnet' (default, paper trading) or 'live' (real funds)."""
    typ = order_type.upper()
    params: dict = {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": typ,
        "quantity": quantity,
    }
    if typ == "LIMIT":
        if price <= 0:
            return "[tool-error] binance_place_order: price required for LIMIT orders."
        params["price"] = price
        params["timeInForce"] = time_in_force
    try:
        result = _signed_request("POST", "/api/v3/order", params, mode)
    except Exception as e:
        return f"[tool-error] binance_place_order: {e}"
    label = _label(mode)
    warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "live" else ""
    return (
        f"Binance order placed [{label}]:\n"
        f"{warn}"
        f"  ID: {result.get('orderId')}\n"
        f"  {side.upper()} {quantity} {symbol.upper()} {typ}"
        + (f" @ {price}" if typ == "LIMIT" else "")
        + f"\n  Status: {result.get('status')}"
    )


@tool
def binance_cancel_order(
    symbol: Annotated[str, "Trading pair"],
    order_id: Annotated[int, "Binance orderId to cancel"],
    mode: str = "testnet",
) -> str:
    """Cancel a pending Binance order. mode='testnet' (default, paper) or 'live'."""
    try:
        result = _signed_request(
            "DELETE", "/api/v3/order", {"symbol": symbol.upper(), "orderId": order_id}, mode
        )
    except Exception as e:
        return f"[tool-error] binance_cancel_order: {e}"
    return (
        f"Binance order cancelled [{_label(mode)}]: {result.get('symbol')} "
        f"#{result.get('orderId')} status={result.get('status')}"
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
