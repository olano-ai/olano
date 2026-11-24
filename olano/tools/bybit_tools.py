"""Bybit V5 unified API tools — spot trading, market data, account.

Safety
------
**All tools default to ``mode='testnet'``** (paper trading, fake balances).
To trade real funds, the caller must explicitly pass ``mode='live'`` AND have
live credentials configured.

Auth env vars (live):
  BYBIT_API_KEY    / BYBIT_API_SECRET
Auth env vars (testnet):
  BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET
Fallback for either: BYBIT_API_KEY / BYBIT_API_SECRET.

Live host:    https://api.bybit.com
Testnet host: https://api-testnet.bybit.com  (sign up at testnet.bybit.com)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_LIVE_HOST = "https://api.bybit.com"
_TEST_HOST = "https://api-testnet.bybit.com"
_RECV_WINDOW = "5000"


def _bybit_creds(mode: str) -> tuple[str, str, str]:
    m = (mode or "live").strip().lower()
    if m == "testnet":
        host = _TEST_HOST
        key = get_credential("BYBIT_TESTNET_API_KEY") or get_credential("BYBIT_API_KEY", "")
        secret = get_credential("BYBIT_TESTNET_API_SECRET") or get_credential(
            "BYBIT_API_SECRET", ""
        )
    else:
        host = _LIVE_HOST
        key = get_credential("BYBIT_API_KEY", "")
        secret = get_credential("BYBIT_API_SECRET", "")
    return host, key, secret


def _signed_request(method: str, path: str, payload: dict, mode: str) -> dict:
    host, key, secret = _bybit_creds(mode)
    if not key or not secret:
        raise RuntimeError(
            "[missing-credentials] Bybit keys missing. Set BYBIT_API_KEY / BYBIT_API_SECRET "
            "(or BYBIT_TESTNET_* for mode='testnet')."
        )
    ts = str(int(time.time() * 1000))
    if method == "GET":
        query = "&".join(f"{k}={v}" for k, v in sorted(payload.items()))
        sign_payload = ts + key + _RECV_WINDOW + query
        body_str = ""
    else:
        body_str = json.dumps(payload, separators=(",", ":"))
        sign_payload = ts + key + _RECV_WINDOW + body_str
        query = ""
    sig = hmac.new(secret.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
        "Content-Type": "application/json",
    }
    url = f"{host}{path}" + (f"?{query}" if query else "")
    if method == "GET":
        r = httpx.get(url, headers=headers, timeout=15)
    else:
        r = httpx.post(url, headers=headers, content=body_str, timeout=20)
    r.raise_for_status()
    out: dict = r.json()
    if out.get("retCode") not in (0, "0"):
        raise RuntimeError(f"Bybit API error {out.get('retCode')}: {out.get('retMsg')}")
    return out


def _public_get(path: str, params: dict | None, mode: str) -> dict:
    host, _, _ = _bybit_creds(mode)
    r = httpx.get(f"{host}{path}", params=params or {}, timeout=10)
    r.raise_for_status()
    out: dict = r.json()
    if out.get("retCode") not in (0, "0"):
        raise RuntimeError(f"Bybit API error {out.get('retCode')}: {out.get('retMsg')}")
    return out


def _label(mode: str) -> str:
    return "TESTNET" if (mode or "live").lower() == "testnet" else "live"


@tool
def bybit_get_balance(
    account_type: Annotated[str, "UNIFIED, CONTRACT, SPOT, FUND"] = "UNIFIED",
    mode: str = "testnet",
) -> str:
    """Get Bybit wallet balance. account_type='UNIFIED' (default) or CONTRACT/SPOT/FUND."""
    try:
        data = _signed_request(
            "GET", "/v5/account/wallet-balance", {"accountType": account_type}, mode
        )
    except Exception as e:
        return f"[tool-error] bybit_get_balance: {e}"
    items = data.get("result", {}).get("list", [])
    if not items:
        return f"No {account_type} wallet data [{_label(mode)}]."
    lines = [f"Bybit {account_type} wallet [{_label(mode)}]:"]
    for w in items:
        total = w.get("totalEquity", "?")
        lines.append(f"  Total equity: {total}")
        for c in w.get("coin", []):
            bal = float(c.get("walletBalance") or 0)
            if bal > 0:
                lines.append(f"    {c.get('coin'):<6} bal={bal:.8f}  usd={c.get('usdValue', '?')}")
    return "\n".join(lines)


@tool
def bybit_get_ticker(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT)"],
    category: Annotated[str, "spot, linear, inverse, option"] = "spot",
    mode: str = "testnet",
) -> str:
    """Get latest 24h ticker for a Bybit symbol."""
    try:
        data = _public_get(
            "/v5/market/tickers", {"category": category, "symbol": symbol.upper()}, mode
        )
    except Exception as e:
        return f"[tool-error] bybit_get_ticker({symbol}): {e}"
    items = data.get("result", {}).get("list", [])
    if not items:
        return f"No ticker for {symbol}."
    t = items[0]
    pct = float(t.get("price24hPcnt") or 0) * 100
    return (
        f"{t.get('symbol')} ({category}) [{_label(mode)}]\n"
        f"  Last: {t.get('lastPrice')}\n"
        f"  24h change: {pct:+.2f}%\n"
        f"  24h high: {t.get('highPrice24h')}  low: {t.get('lowPrice24h')}\n"
        f"  Volume: {t.get('volume24h')}\n"
        f"  Bid/Ask: {t.get('bid1Price')} / {t.get('ask1Price')}"
    )


@tool
def bybit_get_kline(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT)"],
    interval: Annotated[str, "1, 5, 15, 60, 240, D, W"] = "D",
    category: Annotated[str, "spot, linear, inverse"] = "spot",
    limit: Annotated[int, "Number of candles (max 1000)"] = 30,
    mode: str = "testnet",
) -> str:
    """Get OHLCV klines for a Bybit symbol."""
    try:
        data = _public_get(
            "/v5/market/kline",
            {
                "category": category,
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": min(limit, 1000),
            },
            mode,
        )
    except Exception as e:
        return f"[tool-error] bybit_get_kline({symbol}): {e}"
    rows = data.get("result", {}).get("list", [])
    if not rows:
        return f"No kline data for {symbol}."
    rows = list(reversed(rows))  # API returns newest first
    lines = [f"{symbol.upper()} {interval} klines [{_label(mode)}] ({len(rows)}):"]
    lines.append(
        f"  {'Time':<19} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for k in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(k[0]) / 1000))
        lines.append(
            f"  {ts:<19} {float(k[1]):>10.4f} {float(k[2]):>10.4f} "
            f"{float(k[3]):>10.4f} {float(k[4]):>10.4f} {float(k[5]):>14.4f}"
        )
    return "\n".join(lines)


@tool
def bybit_list_open_orders(
    category: Annotated[str, "spot, linear, inverse"] = "spot",
    symbol: Annotated[str, "Filter by symbol (optional)"] = "",
    mode: str = "testnet",
) -> str:
    """List open Bybit orders."""
    params: dict = {"category": category, "limit": 50}
    if symbol:
        params["symbol"] = symbol.upper()
    try:
        data = _signed_request("GET", "/v5/order/realtime", params, mode)
    except Exception as e:
        return f"[tool-error] bybit_list_open_orders: {e}"
    orders = data.get("result", {}).get("list", [])
    if not orders:
        return f"No open orders [{_label(mode)}]."
    lines = [f"Bybit open orders [{_label(mode)}] ({len(orders)}):"]
    for o in orders:
        lines.append(
            f"  [{o.get('orderId', '')[:8]}] {o.get('symbol')} {o.get('side')} {o.get('orderType')} "
            f"qty={o.get('qty')}  price={o.get('price')}  status={o.get('orderStatus')}"
        )
    return "\n".join(lines)


@tool
def bybit_place_order(
    symbol: Annotated[str, "Trading pair (e.g. BTCUSDT)"],
    side: Annotated[str, "'Buy' or 'Sell'"],
    qty: Annotated[str, "Quantity as string (e.g. '0.001')"],
    order_type: Annotated[str, "'Market' or 'Limit'"] = "Market",
    price: Annotated[str, "Required for Limit orders"] = "",
    category: Annotated[str, "spot, linear, inverse"] = "spot",
    time_in_force: Annotated[str, "GTC, IOC, FOK"] = "GTC",
    mode: str = "testnet",
) -> str:
    """Place a Bybit order. mode='testnet' (default, paper) or 'live'."""
    body: dict = {
        "category": category,
        "symbol": symbol.upper(),
        "side": side.capitalize(),
        "orderType": order_type.capitalize(),
        "qty": qty,
        "timeInForce": time_in_force,
    }
    if order_type.lower() == "limit":
        if not price:
            return "[tool-error] bybit_place_order: price required for Limit orders."
        body["price"] = price
    try:
        data = _signed_request("POST", "/v5/order/create", body, mode)
    except Exception as e:
        return f"[tool-error] bybit_place_order: {e}"
    res = data.get("result", {})
    label = _label(mode)
    warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "live" else ""
    return (
        f"Bybit order placed [{label}]:\n"
        f"{warn}"
        f"  ID: {res.get('orderId')}\n"
        f"  {side} {qty} {symbol.upper()} {order_type}" + (f" @ {price}" if price else "")
    )


@tool
def bybit_cancel_order(
    symbol: Annotated[str, "Trading pair"],
    order_id: Annotated[str, "Bybit orderId"],
    category: Annotated[str, "spot, linear, inverse"] = "spot",
    mode: str = "testnet",
) -> str:
    """Cancel a pending Bybit order."""
    body = {"category": category, "symbol": symbol.upper(), "orderId": order_id}
    try:
        data = _signed_request("POST", "/v5/order/cancel", body, mode)
    except Exception as e:
        return f"[tool-error] bybit_cancel_order: {e}"
    return (
        f"Bybit order cancelled [{_label(mode)}]: {data.get('result', {}).get('orderId', order_id)}"
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
