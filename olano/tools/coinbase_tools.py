"""Coinbase Advanced Trade tools — crypto portfolio, market data, and trading.

Safety
------
**Coinbase Advanced Trade has no public sandbox / paper environment.** All
trading tools execute on production with real funds. Use small notional
sizes for testing (Coinbase enforces minimums per product). For full
paper-trading workflows, prefer Binance / Bybit / OKX / Alpaca which do
offer free testnets.

Auth env vars:
  COINBASE_API_KEY    — API key name (CDP format, e.g. "organizations/.../apiKeys/...")
  COINBASE_API_SECRET — API secret (HMAC secret string)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_HOST = "https://api.coinbase.com"


def _sign_request(method: str, path: str, body: str = "") -> dict:
    api_key = get_credential("COINBASE_API_KEY", "")
    api_secret = get_credential("COINBASE_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("COINBASE_API_KEY and COINBASE_API_SECRET must be set.")
    timestamp = str(int(time.time()))
    message = timestamp + method.upper() + path + body
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {
        "CB-ACCESS-KEY": api_key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
    body_str = json.dumps(body) if body else ""
    headers = _sign_request(method, path, body_str)
    url = f"{_HOST}{path}"
    if method.upper() == "GET":
        r = httpx.get(url, headers=headers, params=params or {}, timeout=15)
    elif method.upper() == "POST":
        r = httpx.post(url, headers=headers, params=params or {}, content=body_str, timeout=20)
    elif method.upper() == "DELETE":
        r = httpx.delete(url, headers=headers, params=params or {}, timeout=15)
    else:
        raise ValueError(f"Unsupported method: {method}")
    r.raise_for_status()
    return r.json()


@tool
def coinbase_list_accounts() -> str:
    """List Coinbase portfolio accounts and balances."""
    try:
        accounts = _request("GET", "/api/v3/brokerage/accounts").get("accounts", [])
    except Exception as e:
        return f"[tool-error] coinbase_list_accounts: {e}"
    if not accounts:
        return "No accounts found."
    lines = ["Coinbase accounts:"]
    for a in accounts:
        bal = a.get("available_balance", {})
        hold = a.get("hold", {})
        lines.append(
            f"  {a.get('name', '?'):<20} avail={bal.get('value', '0')} {bal.get('currency', '')}"
            + (f"  hold={hold.get('value', '0')}" if hold.get("value") not in (None, "0") else "")
        )
    return "\n".join(lines)


@tool
def coinbase_get_price(
    product_id: Annotated[str, "Trading pair (e.g. BTC-USD, ETH-USD, SOL-USDC)"],
) -> str:
    """Get the current best bid/ask price for a Coinbase product."""
    try:
        data = _request("GET", "/api/v3/brokerage/best_bid_ask", params={"product_ids": product_id})
    except Exception as e:
        return f"[tool-error] coinbase_get_price({product_id}): {e}"
    pricelists = data.get("pricebooks", [])
    if not pricelists:
        return f"No price data for {product_id}."
    book = pricelists[0]
    bids = book.get("bids", [{}])[0]
    asks = book.get("asks", [{}])[0]
    return f"{product_id} — Bid: {bids.get('price', 'N/A')} | Ask: {asks.get('price', 'N/A')}"


@tool
def coinbase_list_products(
    product_type: Annotated[str, "Filter: 'SPOT' or 'FUTURE' (optional, default all)"] = "",
    limit: Annotated[int, "Max products to return"] = 25,
) -> str:
    """List tradable Coinbase products with current price and 24h change."""
    try:
        params: dict = {"limit": limit}
        if product_type:
            params["product_type"] = product_type
        products = _request("GET", "/api/v3/brokerage/products", params=params).get("products", [])
    except Exception as e:
        return f"[tool-error] coinbase_list_products: {e}"
    if not products:
        return "No products found."
    lines = [f"Coinbase products ({len(products)}):"]
    lines.append(f"  {'Product':<14} {'Price':>12} {'24h %':>10} {'Volume 24h':>16}")
    for p in products[:limit]:
        try:
            change = float(p.get("price_percentage_change_24h") or 0)
        except (ValueError, TypeError):
            change = 0
        lines.append(
            f"  {p.get('product_id', '?'):<14} {p.get('price', '?'):>12} "
            f"{change:>+9.2f}% {p.get('volume_24h', '?'):>16}"
        )
    return "\n".join(lines)


@tool
def coinbase_get_product(
    product_id: Annotated[str, "Trading pair (e.g. BTC-USD)"],
) -> str:
    """Get detailed metadata for a Coinbase product (limits, increments, status)."""
    try:
        p = _request("GET", f"/api/v3/brokerage/products/{product_id}")
    except Exception as e:
        return f"[tool-error] coinbase_get_product({product_id}): {e}"
    return (
        f"{p.get('product_id')}  ({p.get('status', '?')})\n"
        f"  Price: {p.get('price', '?')}\n"
        f"  24h change: {p.get('price_percentage_change_24h', '?')}%\n"
        f"  Volume 24h: {p.get('volume_24h', '?')}\n"
        f"  Quote increment: {p.get('quote_increment', '?')}  "
        f"Base increment: {p.get('base_increment', '?')}\n"
        f"  Min order size: {p.get('base_min_size', '?')}  "
        f"Max: {p.get('base_max_size', '?')}\n"
        f"  Trading disabled: {p.get('trading_disabled', False)}"
    )


@tool
def coinbase_get_candles(
    product_id: Annotated[str, "Trading pair (e.g. BTC-USD)"],
    granularity: Annotated[
        str,
        "Candle granularity: ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, "
        "TWO_HOUR, SIX_HOUR, ONE_DAY",
    ] = "ONE_DAY",
    limit: Annotated[int, "Number of candles to return (max 350)"] = 30,
) -> str:
    """Get OHLCV candlestick data for a Coinbase product."""
    granularity_seconds = {
        "ONE_MINUTE": 60,
        "FIVE_MINUTE": 300,
        "FIFTEEN_MINUTE": 900,
        "THIRTY_MINUTE": 1800,
        "ONE_HOUR": 3600,
        "TWO_HOUR": 7200,
        "SIX_HOUR": 21600,
        "ONE_DAY": 86400,
    }
    secs = granularity_seconds.get(granularity, 86400)
    end = int(time.time())
    start = end - secs * max(1, min(limit, 350))
    try:
        data = _request(
            "GET",
            f"/api/v3/brokerage/products/{product_id}/candles",
            params={"start": start, "end": end, "granularity": granularity},
        )
    except Exception as e:
        return f"[tool-error] coinbase_get_candles({product_id}): {e}"
    candles = data.get("candles", [])
    if not candles:
        return f"No candle data for {product_id}."
    lines = [f"{product_id} {granularity} candles ({len(candles)} returned):"]
    lines.append("  Date                  Open      High      Low       Close     Volume")
    for c in candles:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(c.get("start", 0))))
        lines.append(
            f"  {ts}  {float(c.get('open', 0)):>9.2f}  {float(c.get('high', 0)):>9.2f}  "
            f"{float(c.get('low', 0)):>9.2f}  {float(c.get('close', 0)):>9.2f}  "
            f"{float(c.get('volume', 0)):>12.4f}"
        )
    return "\n".join(lines)


@tool
def coinbase_list_orders(
    product_id: Annotated[str, "Trading pair filter (optional)"] = "",
    status: Annotated[str, "Order status: OPEN, FILLED, CANCELLED, ALL"] = "OPEN",
    limit: Annotated[int, "Max orders to return"] = 20,
) -> str:
    """List Coinbase orders."""
    params: dict = {"limit": limit}
    if status and status.upper() != "ALL":
        params["order_status"] = status
    if product_id:
        params["product_id"] = product_id
    try:
        orders = _request("GET", "/api/v3/brokerage/orders/historical/batch", params=params).get(
            "orders", []
        )
    except Exception as e:
        return f"[tool-error] coinbase_list_orders: {e}"
    if not orders:
        return f"No {status} orders found."
    lines = [f"Coinbase orders ({status}, {len(orders)}):"]
    for o in orders:
        cfg = o.get("order_configuration", {})
        leg = cfg.get("market_market_ioc") or cfg.get("limit_limit_gtc") or {}
        size = leg.get("base_size", "?")
        limit_price = leg.get("limit_price", "")
        lines.append(
            f"  [{o.get('order_id', '')[:8]}] {o.get('product_id', '?')} "
            f"{o.get('side', '?')} {size}"
            + (f" @ ${limit_price}" if limit_price else "")
            + f" — {o.get('status', '?')}"
        )
    return "\n".join(lines)


@tool
def coinbase_portfolio_summary() -> str:
    """Get a high-level portfolio summary across all Coinbase portfolios."""
    try:
        portfolios = _request("GET", "/api/v3/brokerage/portfolios").get("portfolios", [])
    except Exception as e:
        return f"[tool-error] coinbase_portfolio_summary: {e}"
    if not portfolios:
        return "No portfolios found."
    lines = ["Coinbase portfolios:"]
    for p in portfolios:
        lines.append(
            f"  [{p.get('uuid', '')[:8]}] {p.get('name', '?')} — type: {p.get('type', '?')}"
        )
    return "\n".join(lines)


@tool
def coinbase_place_order(
    product_id: Annotated[str, "Trading pair (e.g. BTC-USD)"],
    side: Annotated[str, "'BUY' or 'SELL'"],
    size: Annotated[str, "Base-asset quantity as a string (e.g. '0.001')"],
    order_type: Annotated[str, "'MARKET' or 'LIMIT'"] = "MARKET",
    limit_price: Annotated[str, "Required for LIMIT orders, quote price as string"] = "",
) -> str:
    """Submit a Coinbase order. ⚠ NO SANDBOX — orders execute on production with REAL funds. Use small sizes. For paper trading, use Binance/Bybit/OKX/Alpaca instead."""
    cfg: dict
    side_u = side.upper()
    typ = order_type.upper()
    if typ == "MARKET":
        cfg = {"market_market_ioc": {"base_size": size}}
    elif typ == "LIMIT":
        if not limit_price:
            return "[tool-error] coinbase_place_order: limit_price required for LIMIT orders."
        cfg = {
            "limit_limit_gtc": {
                "base_size": size,
                "limit_price": limit_price,
                "post_only": False,
            }
        }
    else:
        return f"[tool-error] coinbase_place_order: unsupported order_type '{order_type}'."
    body = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": side_u,
        "order_configuration": cfg,
    }
    try:
        result = _request("POST", "/api/v3/brokerage/orders", body=body)
    except Exception as e:
        return f"[tool-error] coinbase_place_order: {e}"
    if not result.get("success"):
        err = result.get("error_response") or result.get("failure_reason") or result
        return f"Order rejected: {err}"
    sr = result.get("success_response", {})
    return (
        f"Order placed:\n"
        f"  ID: {sr.get('order_id')}\n"
        f"  {side_u} {size} {product_id} ({typ}"
        + (f" @ ${limit_price}" if typ == "LIMIT" else "")
        + ")"
    )


@tool
def coinbase_cancel_order(
    order_id: Annotated[str, "Coinbase order ID to cancel"],
) -> str:
    """Cancel one or more pending Coinbase orders."""
    body = {"order_ids": [order_id]}
    try:
        result = _request("POST", "/api/v3/brokerage/orders/batch_cancel", body=body)
    except Exception as e:
        return f"[tool-error] coinbase_cancel_order: {e}"
    results = result.get("results", [])
    if not results:
        return f"No response for cancel of {order_id}."
    r = results[0]
    if r.get("success"):
        return f"Order {order_id} cancelled."
    return f"Cancel failed for {order_id}: {r.get('failure_reason', 'unknown')}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
