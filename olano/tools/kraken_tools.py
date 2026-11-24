"""Kraken Spot REST API tools — market data + trading.

Safety
------
Kraken Spot has no sandbox / paper environment, so trading tools always hit
the live exchange. As a paper-trading equivalent, ``kraken_place_order``
**defaults to ``dry_run=True``** which uses Kraken's ``validate=true`` flag —
the order is fully validated by the exchange but not submitted. To actually
submit a real order, the caller must explicitly pass ``dry_run=False``.

Auth env vars:
  KRAKEN_API_KEY     — API key
  KRAKEN_API_SECRET  — API private key (base64-encoded)

Host: https://api.kraken.com
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_HOST = "https://api.kraken.com"


def _signed_request(path: str, data: dict | None) -> dict:
    key = get_credential("KRAKEN_API_KEY", "")
    secret = get_credential("KRAKEN_API_SECRET", "")
    if not key or not secret:
        raise RuntimeError("[missing-credentials] Set KRAKEN_API_KEY and KRAKEN_API_SECRET.")
    payload = dict(data or {})
    payload["nonce"] = str(int(time.time() * 1000))
    body = urllib.parse.urlencode(payload)
    sha = hashlib.sha256((payload["nonce"] + body).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), (path + "").encode() + sha, hashlib.sha512)
    sig = base64.b64encode(mac.digest()).decode()
    headers = {
        "API-Key": key,
        "API-Sign": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = httpx.post(f"{_HOST}{path}", headers=headers, content=body, timeout=20)
    r.raise_for_status()
    out = r.json()
    if out.get("error"):
        raise RuntimeError(f"Kraken API error: {out['error']}")
    return cast(dict, out.get("result", {}))


def _public_get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(f"{_HOST}{path}", params=params or {}, timeout=10)
    r.raise_for_status()
    out = r.json()
    if out.get("error"):
        raise RuntimeError(f"Kraken API error: {out['error']}")
    return cast(dict, out.get("result", {}))


@tool
def kraken_get_balance() -> str:
    """Get Kraken account balances across all assets."""
    try:
        bals = _signed_request("/0/private/Balance", None)
    except Exception as e:
        return f"[tool-error] kraken_get_balance: {e}"
    bals = {k: v for k, v in bals.items() if float(v) > 0}
    if not bals:
        return "No non-zero balances."
    lines = ["Kraken balances:"]
    for asset, bal in sorted(bals.items()):
        lines.append(f"  {asset:<10} {float(bal):.8f}")
    return "\n".join(lines)


@tool
def kraken_get_ticker(
    pair: Annotated[str, "Trading pair (e.g. XBTUSD, ETHUSD, SOLUSD)"],
) -> str:
    """Get the current ticker for a Kraken trading pair (public, no auth)."""
    try:
        result = _public_get("/0/public/Ticker", {"pair": pair})
    except Exception as e:
        return f"[tool-error] kraken_get_ticker({pair}): {e}"
    if not result:
        return f"No ticker data for {pair}."
    pair_id, t = next(iter(result.items()))
    return (
        f"{pair_id}\n"
        f"  Last: {t.get('c', ['?'])[0]}\n"
        f"  Bid: {t.get('b', ['?'])[0]}  Ask: {t.get('a', ['?'])[0]}\n"
        f"  24h volume: {t.get('v', ['?', '?'])[1]}\n"
        f"  24h high: {t.get('h', ['?', '?'])[1]}  low: {t.get('l', ['?', '?'])[1]}\n"
        f"  24h VWAP: {t.get('p', ['?', '?'])[1]}"
    )


@tool
def kraken_get_ohlc(
    pair: Annotated[str, "Trading pair (e.g. XBTUSD)"],
    interval: Annotated[int, "Minutes per candle: 1,5,15,30,60,240,1440,10080,21600"] = 1440,
) -> str:
    """Get OHLCV candles for a Kraken pair (public, no auth)."""
    try:
        result = _public_get("/0/public/OHLC", {"pair": pair, "interval": interval})
    except Exception as e:
        return f"[tool-error] kraken_get_ohlc({pair}): {e}"
    pair_id = next((k for k in result if k != "last"), None)
    if not pair_id:
        return f"No OHLC data for {pair}."
    rows = result[pair_id][-30:]
    lines = [f"{pair_id} OHLC ({interval}m, {len(rows)} candles):"]
    lines.append(
        f"  {'Time':<19} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for k in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(k[0])))
        lines.append(
            f"  {ts:<19} {float(k[1]):>10.4f} {float(k[2]):>10.4f} "
            f"{float(k[3]):>10.4f} {float(k[4]):>10.4f} {float(k[6]):>14.4f}"
        )
    return "\n".join(lines)


@tool
def kraken_list_open_orders() -> str:
    """List open Kraken orders."""
    try:
        result = _signed_request("/0/private/OpenOrders", None)
    except Exception as e:
        return f"[tool-error] kraken_list_open_orders: {e}"
    orders = result.get("open", {})
    if not orders:
        return "No open orders."
    lines = [f"Kraken open orders ({len(orders)}):"]
    for txid, o in orders.items():
        d = o.get("descr", {})
        lines.append(
            f"  [{txid[:10]}] {d.get('pair')} {d.get('type')} {d.get('ordertype')} "
            f"qty={o.get('vol')} price={d.get('price')}  status={o.get('status')}"
        )
    return "\n".join(lines)


@tool
def kraken_place_order(
    pair: Annotated[str, "Trading pair (e.g. XBTUSD)"],
    side: Annotated[str, "'buy' or 'sell'"],
    volume: Annotated[str, "Order quantity as string"],
    order_type: Annotated[str, "'market', 'limit', 'stop-loss', 'take-profit'"] = "market",
    price: Annotated[str, "Required for non-market orders"] = "",
    dry_run: Annotated[bool, "If True (default), validate only without submitting"] = True,
) -> str:
    """Place a Kraken order. Defaults to dry_run=True (validate-only, paper-equivalent). Pass dry_run=False to submit a REAL order — Kraken Spot has no sandbox."""
    payload: dict = {
        "pair": pair,
        "type": side.lower(),
        "ordertype": order_type.lower(),
        "volume": volume,
    }
    if order_type.lower() != "market":
        if not price:
            return "[tool-error] kraken_place_order: price required for non-market orders."
        payload["price"] = price
    if dry_run:
        payload["validate"] = True
    try:
        result = _signed_request("/0/private/AddOrder", payload)
    except Exception as e:
        return f"[tool-error] kraken_place_order: {e}"
    txids = result.get("txid", [])
    descr = result.get("descr", {})
    if dry_run:
        return f"Kraken order VALIDATED (not submitted): {descr.get('order', '?')}"
    return (
        f"Kraken order placed:\n"
        f"  ⚠ LIVE TRADE — real funds.\n"
        f"  IDs: {', '.join(txids) or '(none returned)'}\n"
        f"  {descr.get('order', '?')}"
    )


@tool
def kraken_cancel_order(
    txid: Annotated[str, "Kraken transaction ID to cancel"],
) -> str:
    """Cancel a pending Kraken order by transaction ID."""
    try:
        result = _signed_request("/0/private/CancelOrder", {"txid": txid})
    except Exception as e:
        return f"[tool-error] kraken_cancel_order: {e}"
    return f"Kraken cancel: count={result.get('count', 0)} pending={result.get('pending', False)}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
