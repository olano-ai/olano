"""OKX V5 API tools — spot trading, market data, account.

Safety
------
**All tools default to ``mode='demo'``** (OKX's paper trading environment).
To trade real funds, the caller must explicitly pass ``mode='live'`` AND have
live credentials configured.

Auth env vars (live):
  OKX_API_KEY        — API key
  OKX_API_SECRET     — API secret
  OKX_API_PASSPHRASE — Passphrase set when creating the API key

Auth env vars (demo):
  OKX_DEMO_API_KEY   — Demo trading API key (created from the "Demo Trading"
                       section of the OKX UI, distinct from live keys)
  OKX_DEMO_API_SECRET / OKX_DEMO_API_PASSPHRASE
Fallback for either: OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE.

Host: https://www.okx.com  (demo mode adds the x-simulated-trading: 1 header)
"""

from __future__ import annotations

import base64
import datetime as _dt
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

_HOST = "https://www.okx.com"


def _okx_creds(mode: str) -> tuple[str, str, str, bool]:
    """Return (api_key, api_secret, passphrase, is_demo)."""
    is_demo = (mode or "live").strip().lower() == "demo"
    if is_demo:
        key = get_credential("OKX_DEMO_API_KEY") or get_credential("OKX_API_KEY", "")
        secret = get_credential("OKX_DEMO_API_SECRET") or get_credential("OKX_API_SECRET", "")
        passphrase = get_credential("OKX_DEMO_API_PASSPHRASE") or get_credential(
            "OKX_API_PASSPHRASE", ""
        )
    else:
        key = get_credential("OKX_API_KEY", "")
        secret = get_credential("OKX_API_SECRET", "")
        passphrase = get_credential("OKX_API_PASSPHRASE", "")
    return key, secret, passphrase, is_demo


def _signed_request(
    method: str, path: str, params: dict | None, body: dict | None, mode: str
) -> dict:
    key, secret, passphrase, is_demo = _okx_creds(mode)
    if not key or not secret or not passphrase:
        raise RuntimeError(
            "[missing-credentials] OKX keys missing. Set OKX_API_KEY, OKX_API_SECRET, "
            "OKX_API_PASSPHRASE (or OKX_DEMO_* for mode='demo')."
        )
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    if method == "GET" and params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        path_with_qs = f"{path}?{qs}"
    else:
        path_with_qs = path
    ts = (
        _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{int(time.time() * 1000) % 1000:03d}Z"
    )
    msg = ts + method.upper() + path_with_qs + body_str
    sig = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    if is_demo:
        headers["x-simulated-trading"] = "1"
    url = f"{_HOST}{path_with_qs}"
    if method == "GET":
        r = httpx.get(url, headers=headers, timeout=15)
    elif method == "POST":
        r = httpx.post(url, headers=headers, content=body_str, timeout=20)
    else:
        raise ValueError(f"Unsupported method: {method}")
    r.raise_for_status()
    out: dict = r.json()
    if str(out.get("code")) not in ("0", ""):
        raise RuntimeError(f"OKX API error {out.get('code')}: {out.get('msg')}")
    return out


def _public_get(path: str, params: dict | None) -> dict:
    r = httpx.get(f"{_HOST}{path}", params=params or {}, timeout=10)
    r.raise_for_status()
    out: dict = r.json()
    if str(out.get("code")) not in ("0", ""):
        raise RuntimeError(f"OKX API error {out.get('code')}: {out.get('msg')}")
    return out


def _label(mode: str) -> str:
    return "DEMO" if (mode or "live").lower() == "demo" else "live"


@tool
def okx_get_balance(
    ccy: Annotated[str, "Filter by currency (e.g. 'USDT'); empty = all"] = "",
    mode: str = "demo",
) -> str:
    """Get OKX trading-account balances. mode='demo' (default, paper) or 'live'."""
    params = {"ccy": ccy.upper()} if ccy else None
    try:
        data = _signed_request("GET", "/api/v5/account/balance", params, None, mode)
    except Exception as e:
        return f"[tool-error] okx_get_balance: {e}"
    rows = data.get("data", [{}])[0].get("details", [])
    rows = [r for r in rows if float(r.get("cashBal") or 0) > 0]
    if not rows:
        return f"No balances [{_label(mode)}]."
    lines = [f"OKX balances [{_label(mode)}]:"]
    for r in rows:
        lines.append(
            f"  {r.get('ccy', '?'):<8} cash={float(r.get('cashBal') or 0):.6f}  "
            f"avail={r.get('availBal', '?')}  eq_usd={r.get('eqUsd', '?')}"
        )
    return "\n".join(lines)


@tool
def okx_get_ticker(
    inst_id: Annotated[str, "Instrument ID (e.g. BTC-USDT, ETH-USDT-SWAP)"],
) -> str:
    """Get the latest 24h ticker for an OKX instrument (public, no auth)."""
    try:
        data = _public_get("/api/v5/market/ticker", {"instId": inst_id.upper()})
    except Exception as e:
        return f"[tool-error] okx_get_ticker({inst_id}): {e}"
    rows = data.get("data", [])
    if not rows:
        return f"No ticker for {inst_id}."
    t = rows[0]
    open24 = float(t.get("open24h") or 0)
    last = float(t.get("last") or 0)
    pct = ((last - open24) / open24 * 100) if open24 else 0
    return (
        f"{t.get('instId')}\n"
        f"  Last: {t.get('last')}\n"
        f"  24h change: {pct:+.2f}%\n"
        f"  24h high: {t.get('high24h')}  low: {t.get('low24h')}\n"
        f"  Volume (base): {t.get('vol24h')}  (quote): {t.get('volCcy24h')}\n"
        f"  Bid/Ask: {t.get('bidPx')} / {t.get('askPx')}"
    )


@tool
def okx_get_candles(
    inst_id: Annotated[str, "Instrument ID (e.g. BTC-USDT)"],
    bar: Annotated[str, "1m,5m,15m,1H,4H,1D,1W,1M"] = "1D",
    limit: Annotated[int, "Number of candles (max 300)"] = 30,
) -> str:
    """Get OHLCV candles for an OKX instrument (public, no auth)."""
    try:
        data = _public_get(
            "/api/v5/market/candles",
            {"instId": inst_id.upper(), "bar": bar, "limit": min(limit, 300)},
        )
    except Exception as e:
        return f"[tool-error] okx_get_candles({inst_id}): {e}"
    rows = data.get("data", [])
    if not rows:
        return f"No candles for {inst_id}."
    rows = list(reversed(rows))
    lines = [f"{inst_id.upper()} {bar} candles ({len(rows)}):"]
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
def okx_list_open_orders(
    inst_type: Annotated[str, "SPOT, MARGIN, SWAP, FUTURES, OPTION"] = "SPOT",
    inst_id: Annotated[str, "Filter by instrument (optional)"] = "",
    mode: str = "demo",
) -> str:
    """List pending OKX orders."""
    params: dict = {"instType": inst_type}
    if inst_id:
        params["instId"] = inst_id.upper()
    try:
        data = _signed_request("GET", "/api/v5/trade/orders-pending", params, None, mode)
    except Exception as e:
        return f"[tool-error] okx_list_open_orders: {e}"
    orders = data.get("data", [])
    if not orders:
        return f"No pending orders [{_label(mode)}]."
    lines = [f"OKX pending orders [{_label(mode)}] ({len(orders)}):"]
    for o in orders:
        lines.append(
            f"  [{o.get('ordId', '')[:8]}] {o.get('instId')} {o.get('side')} {o.get('ordType')} "
            f"qty={o.get('sz')}  price={o.get('px')}  state={o.get('state')}"
        )
    return "\n".join(lines)


@tool
def okx_place_order(
    inst_id: Annotated[str, "Instrument ID (e.g. BTC-USDT)"],
    side: Annotated[str, "'buy' or 'sell'"],
    sz: Annotated[str, "Quantity as string"],
    ord_type: Annotated[str, "'market', 'limit', 'post_only', 'fok', 'ioc'"] = "market",
    px: Annotated[str, "Required for non-market orders"] = "",
    td_mode: Annotated[str, "'cash' (spot), 'cross', 'isolated'"] = "cash",
    mode: str = "demo",
) -> str:
    """Place an OKX order. mode='demo' (default, paper) or 'live'."""
    body: dict = {
        "instId": inst_id.upper(),
        "tdMode": td_mode,
        "side": side.lower(),
        "ordType": ord_type.lower(),
        "sz": sz,
    }
    if ord_type.lower() != "market":
        if not px:
            return "[tool-error] okx_place_order: px required for non-market orders."
        body["px"] = px
    try:
        data = _signed_request("POST", "/api/v5/trade/order", None, body, mode)
    except Exception as e:
        return f"[tool-error] okx_place_order: {e}"
    rows = data.get("data", [{}])
    res = rows[0]
    if str(res.get("sCode")) not in ("0", ""):
        return f"OKX order rejected: {res.get('sMsg')} (code {res.get('sCode')})"
    label = _label(mode)
    warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "live" else ""
    return (
        f"OKX order placed [{label}]:\n"
        f"{warn}"
        f"  ID: {res.get('ordId')}\n"
        f"  {side.lower()} {sz} {inst_id.upper()} {ord_type}" + (f" @ {px}" if px else "")
    )


@tool
def okx_cancel_order(
    inst_id: Annotated[str, "Instrument ID"],
    ord_id: Annotated[str, "OKX order ID"],
    mode: str = "demo",
) -> str:
    """Cancel a pending OKX order."""
    body = {"instId": inst_id.upper(), "ordId": ord_id}
    try:
        data = _signed_request("POST", "/api/v5/trade/cancel-order", None, body, mode)
    except Exception as e:
        return f"[tool-error] okx_cancel_order: {e}"
    res = data.get("data", [{}])[0]
    if str(res.get("sCode")) not in ("0", ""):
        return f"Cancel rejected: {res.get('sMsg')}"
    return f"OKX order cancelled [{_label(mode)}]: {res.get('ordId', ord_id)}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
