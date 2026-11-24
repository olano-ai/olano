"""Alpaca brokerage tools (paper + live trading + market data).

Covers Trading API (account, orders, positions, watchlists, calendar, clock,
account activity/configs, asset lookup) and Market Data API (bars, latest
quote/trade/snapshot, news, crypto, option contracts).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import install_hint, optional_import
from olano.tools._toolkit import make_toolkit_api

_ALPACA_DATA_URL = "https://data.alpaca.markets"

logger = logging.getLogger(__name__)


_ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
_ALPACA_LIVE_URL = "https://api.alpaca.markets"

_ALPACA_CREDS_MSG = (
    "[missing-credentials] No Alpaca API key found for the requested mode. "
    "Set ALPACA_PAPER_API_KEY/ALPACA_PAPER_SECRET_KEY (paper) or "
    "ALPACA_LIVE_API_KEY/ALPACA_LIVE_SECRET_KEY (live). "
    "ALPACA_API_KEY/ALPACA_SECRET_KEY are used as a fallback for both."
)


def _resolve_alpaca_mode(mode: str) -> tuple[str, str, str]:
    """
    Resolve (base_url, api_key, api_secret) for a given mode.

    mode:
      "paper" — force https://paper-api.alpaca.markets
      "live"  — force https://api.alpaca.markets
      ""      — follow ALPACA_BASE_URL (defaults to paper)

    Credentials are looked up in this order:
      paper: ALPACA_PAPER_API_KEY → ALPACA_API_KEY
             ALPACA_PAPER_SECRET_KEY → ALPACA_SECRET_KEY
      live:  ALPACA_LIVE_API_KEY  → ALPACA_API_KEY
             ALPACA_LIVE_SECRET_KEY → ALPACA_SECRET_KEY
    """
    m = (mode or "").strip().lower()
    if m == "live":
        base_url = _ALPACA_LIVE_URL
        key = get_credential("ALPACA_LIVE_API_KEY") or get_credential("ALPACA_API_KEY", "")
        secret = get_credential("ALPACA_LIVE_SECRET_KEY") or get_credential("ALPACA_SECRET_KEY", "")
    elif m == "paper":
        base_url = _ALPACA_PAPER_URL
        key = get_credential("ALPACA_PAPER_API_KEY") or get_credential("ALPACA_API_KEY", "")
        secret = get_credential("ALPACA_PAPER_SECRET_KEY") or get_credential(
            "ALPACA_SECRET_KEY", ""
        )
    else:
        base_url = get_credential("ALPACA_BASE_URL", _ALPACA_PAPER_URL)
        if "paper" in base_url:
            key = get_credential("ALPACA_PAPER_API_KEY") or get_credential("ALPACA_API_KEY", "")
            secret = get_credential("ALPACA_PAPER_SECRET_KEY") or get_credential(
                "ALPACA_SECRET_KEY", ""
            )
        else:
            key = get_credential("ALPACA_LIVE_API_KEY") or get_credential("ALPACA_API_KEY", "")
            secret = get_credential("ALPACA_LIVE_SECRET_KEY") or get_credential(
                "ALPACA_SECRET_KEY", ""
            )
    return base_url, key, secret


def _alpaca_api(mode: str = "paper") -> tuple[Any | None, str | None]:
    """Return (api, error_str). error_str is None on success.

    Defaults to paper trading for safety. Pass mode='live' to hit live trading.
    """
    tradeapi, err = optional_import("alpaca_trade_api", purpose="Alpaca brokerage access")
    if err or tradeapi is None:
        return None, install_hint("alpaca-trade-api", purpose="Alpaca brokerage access")
    base_url, key, secret = _resolve_alpaca_mode(mode)
    if not key or not secret:
        return None, _ALPACA_CREDS_MSG
    return tradeapi.REST(key, secret, base_url), None


def _alpaca_mode_label(mode: str) -> str:
    """Human-readable label for the resolved Alpaca mode."""
    m = (mode or "").strip().lower()
    if m == "live":
        return "LIVE"
    if m == "paper":
        return "paper"
    base_url = get_credential("ALPACA_BASE_URL", _ALPACA_PAPER_URL)
    return "paper" if "paper" in base_url else "LIVE"


@tool
def alpaca_get_account(mode: str = "paper") -> str:
    """Get Alpaca brokerage account summary. mode='paper' (default) or 'live'."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        acct = api.get_account()

        def _get(attr: str, default: str = "N/A") -> str:
            v = getattr(acct, attr, None)
            return str(v) if v is not None else default

        equity = _get("equity")
        last_equity = _get("last_equity")
        try:
            pl_today = (
                f"${float(equity) - float(last_equity):+,.2f}"
                if equity != "N/A" and last_equity != "N/A"
                else "N/A"
            )
        except (ValueError, TypeError):
            pl_today = "N/A"

        lines = [f"Alpaca Account [{_alpaca_mode_label(mode)}]:"]
        for label, attr in [
            ("Portfolio value", "portfolio_value"),
            ("Cash", "cash"),
            ("Buying power", "buying_power"),
            ("Equity", "equity"),
        ]:
            raw = _get(attr)
            try:
                lines.append(f"  {label}: ${float(raw):,.2f}")
            except (ValueError, TypeError):
                lines.append(f"  {label}: {raw}")
        lines.append(f"  P/L today: {pl_today}")
        lines.append(f"  Status: {_get('status')}")
        lines.append(f"  Pattern day trader: {_get('pattern_day_trader')}")

        # If attributes look sparse, also dump raw fields for visibility
        raw_dict = getattr(acct, "_raw", None)
        if raw_dict and equity == "N/A":
            lines.append("\n  Raw account fields:")
            for k, v in sorted(raw_dict.items()):
                lines.append(f"    {k}: {v}")

        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_account: {exc}"


@tool
def alpaca_list_positions(mode: str = "paper") -> str:
    """List all open positions in the Alpaca account. mode='paper' (default) or 'live'."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        positions = api.list_positions()
        if not positions:
            return f"No open positions [{_alpaca_mode_label(mode)}]."
        lines = [f"Open Alpaca positions [{_alpaca_mode_label(mode)}]:"]
        lines.append(
            f"  {'Symbol':<8} {'Qty':>8} {'Side':<6} {'Avg Cost':>10} "
            f"{'Mkt Value':>12} {'Unrealized P/L':>15} {'P/L %':>8}"
        )
        total_pl = 0.0
        for p in positions:
            pl = float(p.unrealized_pl)
            total_pl += pl
            lines.append(
                f"  {p.symbol:<8} {float(p.qty):>8.4f} {p.side:<6} "
                f"${float(p.avg_entry_price):>9.2f} ${float(p.market_value):>11.2f} "
                f"${pl:>+14.2f} {float(p.unrealized_plpc) * 100:>+7.2f}%"
            )
        lines.append(f"\n  Total unrealized P/L: ${total_pl:+,.2f}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_list_positions: {exc}"


@tool
def alpaca_list_orders(
    status: str = "open",
    limit: int = 20,
    symbol: str = "",
    mode: str = "paper",
) -> str:
    """List Alpaca orders. status='open'|'closed'|'all'. Filter by symbol. mode='paper'|'live'."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {"status": status, "limit": limit}
        if symbol:
            kwargs["symbols"] = [symbol.upper()]
        orders = api.list_orders(**kwargs)
        if not orders:
            return f"No {status} orders found [{_alpaca_mode_label(mode)}]."
        lines = [f"Alpaca orders [{_alpaca_mode_label(mode)}] ({status}, {len(orders)}):"]
        for o in orders:
            filled = f"{float(o.filled_qty):.4f}/{float(o.qty):.4f}" if o.qty else "?"
            lines.append(
                f"  [{o.id[:8]}] {o.symbol} {o.side.upper()} {o.type} "
                f"qty={filled}  status={o.status}  "
                f"limit=${o.limit_price or 'mkt'}  submitted={str(o.submitted_at)[:16]}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_list_orders: {exc}"


@tool
def alpaca_get_bars(
    symbol: str,
    timeframe: str = "1Day",
    limit: int = 10,
    mode: str = "paper",
) -> str:
    """Get OHLCV bars for a symbol from Alpaca. timeframe='1Min','5Min','15Min','1Hour','1Day'.

    Bar data is identical for paper and live, but the request goes through whichever
    account credentials match the chosen mode. mode='paper' (default) or 'live'.
    """
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        bars = api.get_bars(symbol.upper(), timeframe, limit=limit).df
        if bars.empty:
            return f"No bar data for {symbol}."
        lines = [f"{symbol} bars [{_alpaca_mode_label(mode)}] ({timeframe}, last {len(bars)}):"]
        lines.append(
            f"  {'Time':<20} {'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} {'Volume':>12}"
        )
        for ts, row in bars.iterrows():
            lines.append(
                f"  {str(ts)[:19]:<20} {row['open']:>9.2f} {row['high']:>9.2f} "
                f"{row['low']:>9.2f} {row['close']:>9.2f} {row['volume']:>12,.0f}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_bars({symbol}): {exc}"


@tool
def alpaca_get_portfolio_history(
    period: str = "1W",
    timeframe: str = "1D",
    mode: str = "paper",
) -> str:
    """Get Alpaca portfolio value history. period='1D','1W','1M','3M','6M','1A'. mode='paper'|'live'."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        hist = api.get_portfolio_history(period=period, timeframe=timeframe)
        timestamps = hist.timestamp or []
        equity = hist.equity or []
        profit_loss = hist.profit_loss or []
        if not timestamps:
            return "No portfolio history data."
        import time as _time

        lines = [f"Portfolio history [{_alpaca_mode_label(mode)}] ({period}, {timeframe}):"]
        lines.append(f"  {'Date':<12} {'Equity':>14} {'P/L':>12}")
        paired = list(zip(timestamps[-10:], equity[-10:], profit_loss[-10:], strict=False))
        for ts, eq, pl in paired:
            date = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
            lines.append(f"  {date:<12} ${float(eq):>13,.2f} ${float(pl):>+11.2f}")
        baseline = float(equity[0]) if equity[0] else 0
        latest = float(equity[-1]) if equity[-1] else 0
        total_pct = ((latest - baseline) / baseline * 100) if baseline else 0
        lines.append(f"\n  Period return: ${latest - baseline:+,.2f} ({total_pct:+.2f}%)")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_portfolio_history: {exc}"


@tool
def alpaca_place_order(
    symbol: str,
    qty: float,
    side: str,
    order_type: str = "market",
    limit_price: float = 0.0,
    time_in_force: str = "day",
    mode: str = "paper",
) -> str:
    """Place an order on Alpaca. side='buy'|'sell'. order_type='market'|'limit'.

    mode='paper' (default, safe) submits against paper-api.alpaca.markets.
    mode='live' submits a REAL money trade against api.alpaca.markets — only set
    this when the user has explicitly confirmed they want to trade live capital.
    """
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {
            "symbol": symbol.upper(),
            "qty": qty,
            "side": side.lower(),
            "type": order_type.lower(),
            "time_in_force": time_in_force,
        }
        if order_type.lower() == "limit" and limit_price > 0:
            kwargs["limit_price"] = limit_price
        order = api.submit_order(**kwargs)
        label = _alpaca_mode_label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        return (
            f"Order submitted [{label}]:\n"
            f"{warn}"
            f"  ID: {order.id}\n"
            f"  {order.side.upper()} {order.qty} {order.symbol} @ {order.type}"
            + (f" ${order.limit_price}" if order.limit_price else "")
            + f"\n  Status: {order.status}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_place_order: {exc}"


@tool
def alpaca_cancel_order(order_id: str, mode: str = "paper") -> str:
    """Cancel a pending Alpaca order by its order ID. mode='paper' (default) or 'live'."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        api.cancel_order(order_id)
        return f"Order {order_id} cancellation requested [{_alpaca_mode_label(mode)}]."
    except Exception as exc:
        return f"[tool-error] alpaca_cancel_order({order_id}): {exc}"


# ─────────────────────────────────── helpers ─────────────────────────────────


def _alpaca_http(
    method: str,
    url: str,
    mode: str = "paper",
    *,
    use_data_api: bool = False,
    **kwargs: Any,
) -> tuple[Any, str | None]:
    """Make a direct REST call to Alpaca for endpoints the SDK doesn't expose well.

    Returns (json_or_None, error_string). One of them is always None.
    """
    _, key, secret = _resolve_alpaca_mode(mode)
    if not key or not secret:
        return None, _ALPACA_CREDS_MSG
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        r = httpx.request(method, url, headers=headers, timeout=20, **kwargs)
        r.raise_for_status()
        return (r.json() if r.content else {}), None
    except Exception as exc:
        return None, f"[tool-error] Alpaca {method} {url}: {exc}"


def _trading_base(mode: str) -> str:
    base_url, _, _ = _resolve_alpaca_mode(mode)
    return base_url


# ─────────────────────────────────── market clock & calendar ─────────────────


@tool
def alpaca_get_clock(mode: str = "paper") -> str:
    """Get the current market clock — is the market open, when does it next open/close."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        clock = api.get_clock()
        return (
            f"Alpaca clock [{_alpaca_mode_label(mode)}]:\n"
            f"  Now:        {getattr(clock, 'timestamp', '?')}\n"
            f"  Market:     {'OPEN' if getattr(clock, 'is_open', False) else 'CLOSED'}\n"
            f"  Next open:  {getattr(clock, 'next_open', '?')}\n"
            f"  Next close: {getattr(clock, 'next_close', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_clock: {exc}"


@tool
def alpaca_get_calendar(
    start: Annotated[str, "Start date YYYY-MM-DD (optional)"] = "",
    end: Annotated[str, "End date YYYY-MM-DD (optional)"] = "",
    mode: str = "paper",
) -> str:
    """Get the Alpaca market calendar (trading days + open/close times)."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {}
        if start:
            kwargs["start"] = start
        if end:
            kwargs["end"] = end
        days = api.get_calendar(**kwargs)
        if not days:
            return "No calendar entries in range."
        lines = [f"Alpaca calendar [{_alpaca_mode_label(mode)}] ({len(days)} sessions):"]
        for d in days[:60]:
            lines.append(
                f"  {getattr(d, 'date', '?')}  open {getattr(d, 'open', '?')}  "
                f"close {getattr(d, 'close', '?')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_calendar: {exc}"


# ─────────────────────────────────── positions: per-symbol + bulk close ──────


@tool
def alpaca_get_position(
    symbol: Annotated[str, "Ticker symbol (e.g. AAPL)"],
    mode: str = "paper",
) -> str:
    """Get a single open Alpaca position by symbol."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        p = api.get_position(symbol.upper())
        qty = float(getattr(p, "qty", 0) or 0)
        avg = float(getattr(p, "avg_entry_price", 0) or 0)
        mv = float(getattr(p, "market_value", 0) or 0)
        pl = float(getattr(p, "unrealized_pl", 0) or 0)
        plpc = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
        return (
            f"Position {symbol.upper()} [{_alpaca_mode_label(mode)}]:\n"
            f"  Qty: {qty:.4f}  side={getattr(p, 'side', '?')}\n"
            f"  Avg cost: ${avg:.4f}  Market value: ${mv:,.2f}\n"
            f"  Unrealized P/L: ${pl:+,.2f} ({plpc:+.2f}%)"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_position({symbol}): {exc}"


@tool
def alpaca_close_position(
    symbol: Annotated[str, "Ticker symbol of position to close"],
    qty: Annotated[float, "Optional qty to liquidate (0 = full)"] = 0.0,
    percentage: Annotated[float, "Optional percent to liquidate (0-100, 0 = ignore)"] = 0.0,
    mode: str = "paper",
) -> str:
    """Liquidate a single Alpaca position. Provide qty OR percentage (not both), or neither for full close."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {}
        if qty > 0:
            kwargs["qty"] = qty
        elif percentage > 0:
            kwargs["percentage"] = percentage
        order = api.close_position(symbol.upper(), **kwargs)
        label = _alpaca_mode_label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        return (
            f"Close-position order submitted [{label}]:\n"
            f"{warn}"
            f"  Symbol: {symbol.upper()}\n"
            f"  Order ID: {getattr(order, 'id', '?')}\n"
            f"  Status: {getattr(order, 'status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_close_position({symbol}): {exc}"


@tool
def alpaca_close_all_positions(
    cancel_orders: Annotated[bool, "Cancel all open orders too"] = False,
    mode: str = "paper",
) -> str:
    """Liquidate ALL open Alpaca positions. ⚠ Use carefully — this is a panic-close."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        results = api.close_all_positions(cancel_orders=cancel_orders)
        label = _alpaca_mode_label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        if not results:
            return f"Close-all-positions: no open positions [{label}]."
        lines = [f"Close-all-positions submitted [{label}]:", warn.rstrip()]
        for r in results:
            sym = getattr(getattr(r, "body", None), "symbol", None) or getattr(r, "symbol", "?")
            status = getattr(r, "status", "?")
            lines.append(f"  {sym}: {status}")
        return "\n".join(line for line in lines if line)
    except Exception as exc:
        return f"[tool-error] alpaca_close_all_positions: {exc}"


@tool
def alpaca_cancel_all_orders(mode: str = "paper") -> str:
    """Cancel ALL open Alpaca orders."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        results = api.cancel_all_orders()
        if not results:
            return f"No open orders to cancel [{_alpaca_mode_label(mode)}]."
        ids = [getattr(r, "id", "?") for r in results]
        return f"Cancelled {len(ids)} order(s) [{_alpaca_mode_label(mode)}]: {', '.join(ids[:20])}"
    except Exception as exc:
        return f"[tool-error] alpaca_cancel_all_orders: {exc}"


# ─────────────────────────────────── orders: get/replace + advanced types ────


@tool
def alpaca_get_order(order_id: str, mode: str = "paper") -> str:
    """Get a single Alpaca order by its ID."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        o = api.get_order(order_id)
        filled = (
            f"{float(getattr(o, 'filled_qty', 0) or 0):.4f}/{float(getattr(o, 'qty', 0) or 0):.4f}"
        )
        return (
            f"Order {order_id} [{_alpaca_mode_label(mode)}]:\n"
            f"  {getattr(o, 'symbol', '?')} {str(getattr(o, 'side', '?')).upper()} "
            f"{getattr(o, 'type', '?')} qty={filled}\n"
            f"  Status: {getattr(o, 'status', '?')} | "
            f"limit={getattr(o, 'limit_price', None) or 'mkt'} | "
            f"stop={getattr(o, 'stop_price', None) or '—'}\n"
            f"  Submitted: {getattr(o, 'submitted_at', '?')}\n"
            f"  Filled avg price: {getattr(o, 'filled_avg_price', '—')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_order({order_id}): {exc}"


@tool
def alpaca_replace_order(
    order_id: Annotated[str, "Existing order ID to replace"],
    qty: Annotated[float, "New quantity (0 = leave unchanged)"] = 0.0,
    limit_price: Annotated[float, "New limit price (0 = leave unchanged)"] = 0.0,
    stop_price: Annotated[float, "New stop price (0 = leave unchanged)"] = 0.0,
    trail: Annotated[float, "New trail amount/percent (0 = leave unchanged)"] = 0.0,
    time_in_force: Annotated[str, "New TIF (empty = leave unchanged)"] = "",
    mode: str = "paper",
) -> str:
    """Replace fields on an open Alpaca order without cancelling it."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {}
        if qty > 0:
            kwargs["qty"] = qty
        if limit_price > 0:
            kwargs["limit_price"] = limit_price
        if stop_price > 0:
            kwargs["stop_price"] = stop_price
        if trail > 0:
            kwargs["trail"] = trail
        if time_in_force:
            kwargs["time_in_force"] = time_in_force
        if not kwargs:
            return "[tool-error] alpaca_replace_order: at least one replacement field is required."
        new_order = api.replace_order(order_id, **kwargs)
        return (
            f"Order replaced [{_alpaca_mode_label(mode)}]:\n"
            f"  Old ID: {order_id} → New ID: {getattr(new_order, 'id', '?')}\n"
            f"  Status: {getattr(new_order, 'status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_replace_order({order_id}): {exc}"


@tool
def alpaca_place_bracket_order(
    symbol: Annotated[str, "Ticker symbol"],
    qty: Annotated[float, "Share quantity"],
    side: Annotated[str, "buy or sell"],
    entry_limit_price: Annotated[float, "Entry limit price (0 = market entry)"] = 0.0,
    take_profit: Annotated[float, "Take-profit limit price"] = 0.0,
    stop_loss: Annotated[float, "Stop-loss trigger price"] = 0.0,
    stop_loss_limit: Annotated[float, "Stop-loss limit price (0 = stop-market)"] = 0.0,
    time_in_force: Annotated[str, "day, gtc, opg, cls, ioc, fok"] = "day",
    mode: str = "paper",
) -> str:
    """Submit a bracket order (entry + take-profit + stop-loss in one atomic submission).

    Entry is market if entry_limit_price=0, otherwise limit. Both take_profit and
    stop_loss are required.
    """
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    if take_profit <= 0 or stop_loss <= 0:
        return "[tool-error] alpaca_place_bracket_order: take_profit and stop_loss are required."
    try:
        kwargs: dict = {
            "symbol": symbol.upper(),
            "qty": qty,
            "side": side.lower(),
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "take_profit": {"limit_price": take_profit},
            "stop_loss": (
                {"stop_price": stop_loss, "limit_price": stop_loss_limit}
                if stop_loss_limit > 0
                else {"stop_price": stop_loss}
            ),
        }
        if entry_limit_price > 0:
            kwargs["type"] = "limit"
            kwargs["limit_price"] = entry_limit_price
        else:
            kwargs["type"] = "market"
        order = api.submit_order(**kwargs)
        label = _alpaca_mode_label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        return (
            f"Bracket order submitted [{label}]:\n"
            f"{warn}"
            f"  ID: {getattr(order, 'id', '?')}\n"
            f"  {side.upper()} {qty} {symbol.upper()} | TP=${take_profit} | SL=${stop_loss}\n"
            f"  Status: {getattr(order, 'status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_place_bracket_order: {exc}"


@tool
def alpaca_place_trailing_stop_order(
    symbol: Annotated[str, "Ticker symbol"],
    qty: Annotated[float, "Share quantity"],
    side: Annotated[str, "buy or sell"],
    trail_percent: Annotated[
        float, "Trailing offset as a percent (e.g. 1.5 = 1.5%). 0 = use trail_price"
    ] = 0.0,
    trail_price: Annotated[
        float, "Trailing offset as a dollar amount. 0 = use trail_percent"
    ] = 0.0,
    time_in_force: Annotated[str, "day, gtc"] = "day",
    mode: str = "paper",
) -> str:
    """Submit a trailing-stop order. Provide either trail_percent OR trail_price."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    if trail_percent <= 0 and trail_price <= 0:
        return (
            "[tool-error] alpaca_place_trailing_stop_order: trail_percent or trail_price required."
        )
    try:
        kwargs: dict = {
            "symbol": symbol.upper(),
            "qty": qty,
            "side": side.lower(),
            "type": "trailing_stop",
            "time_in_force": time_in_force,
        }
        if trail_percent > 0:
            kwargs["trail_percent"] = trail_percent
        else:
            kwargs["trail_price"] = trail_price
        order = api.submit_order(**kwargs)
        label = _alpaca_mode_label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        trail_str = f"{trail_percent}%" if trail_percent > 0 else f"${trail_price}"
        return (
            f"Trailing-stop order submitted [{label}]:\n"
            f"{warn}"
            f"  ID: {getattr(order, 'id', '?')}\n"
            f"  {side.upper()} {qty} {symbol.upper()} trail={trail_str}\n"
            f"  Status: {getattr(order, 'status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_place_trailing_stop_order: {exc}"


# ─────────────────────────────────── assets ──────────────────────────────────


@tool
def alpaca_list_assets(
    status: Annotated[str, "active or inactive"] = "active",
    asset_class: Annotated[str, "us_equity, crypto (empty = us_equity)"] = "",
    tradable_only: Annotated[bool, "Only return tradable assets"] = True,
    limit: Annotated[int, "Max assets to return (helps when full list is huge)"] = 50,
    mode: str = "paper",
) -> str:
    """List Alpaca tradable assets, filtered by status/class."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {"status": status}
        if asset_class:
            kwargs["asset_class"] = asset_class
        assets = api.list_assets(**kwargs)
        if tradable_only:
            assets = [a for a in assets if getattr(a, "tradable", False)]
        if not assets:
            return "No assets matched filter."
        lines = [f"Alpaca assets ({len(assets)} matched, showing {min(limit, len(assets))}):"]
        lines.append(
            f"  {'Symbol':<10} {'Class':<10} {'Exchange':<10} {'Tradable':<10} {'Shortable':<10} {'Fractionable'}"
        )
        for a in assets[:limit]:
            lines.append(
                f"  {getattr(a, 'symbol', '?'):<10} {getattr(a, 'class', '?'):<10} "
                f"{getattr(a, 'exchange', '?'):<10} "
                f"{getattr(a, 'tradable', '?')!s:<10} "
                f"{getattr(a, 'shortable', '?')!s:<10} "
                f"{getattr(a, 'fractionable', '?')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_list_assets: {exc}"


@tool
def alpaca_get_asset(
    symbol_or_id: Annotated[str, "Ticker symbol or asset UUID"],
    mode: str = "paper",
) -> str:
    """Get details for a single Alpaca asset (tradable, shortable, fractionable, etc.)."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        a = api.get_asset(symbol_or_id)
        return (
            f"Asset {getattr(a, 'symbol', symbol_or_id)}:\n"
            f"  Name: {getattr(a, 'name', '?')}\n"
            f"  Class: {getattr(a, 'class', '?')} | Exchange: {getattr(a, 'exchange', '?')}\n"
            f"  Status: {getattr(a, 'status', '?')}\n"
            f"  Tradable: {getattr(a, 'tradable', '?')} | "
            f"Marginable: {getattr(a, 'marginable', '?')}\n"
            f"  Shortable: {getattr(a, 'shortable', '?')} | "
            f"Easy-to-borrow: {getattr(a, 'easy_to_borrow', '?')}\n"
            f"  Fractionable: {getattr(a, 'fractionable', '?')}\n"
            f"  Maintenance margin: {getattr(a, 'maintenance_margin_requirement', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_asset({symbol_or_id}): {exc}"


# ─────────────────────────────────── account config & activity ───────────────


@tool
def alpaca_get_account_configurations(mode: str = "paper") -> str:
    """Get Alpaca account configuration flags (DTBP check, trade confirmations, suspend trade)."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        cfg = api.get_account_configurations()
        return (
            f"Alpaca configurations [{_alpaca_mode_label(mode)}]:\n"
            f"  DTBP check:         {getattr(cfg, 'dtbp_check', '?')}\n"
            f"  Trade confirm email: {getattr(cfg, 'trade_confirm_email', '?')}\n"
            f"  Suspend trade:      {getattr(cfg, 'suspend_trade', '?')}\n"
            f"  No-shorting:        {getattr(cfg, 'no_shorting', '?')}\n"
            f"  Fractional trading: {getattr(cfg, 'fractional_trading', '?')}\n"
            f"  Max margin multiplier: {getattr(cfg, 'max_margin_multiplier', '?')}\n"
            f"  PDT check:          {getattr(cfg, 'pdt_check', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_account_configurations: {exc}"


@tool
def alpaca_update_account_configurations(
    no_shorting: Annotated[str, "true/false/empty"] = "",
    suspend_trade: Annotated[str, "true/false/empty"] = "",
    dtbp_check: Annotated[str, "both/entry/exit/empty"] = "",
    trade_confirm_email: Annotated[str, "all/none/empty"] = "",
    mode: str = "paper",
) -> str:
    """Update Alpaca account configuration flags. Empty values are left unchanged."""
    if not any([no_shorting, suspend_trade, dtbp_check, trade_confirm_email]):
        return "[tool-error] alpaca_update_account_configurations: at least one field required."
    base = _trading_base(mode)
    payload: dict = {}
    if no_shorting:
        payload["no_shorting"] = no_shorting.lower() == "true"
    if suspend_trade:
        payload["suspend_trade"] = suspend_trade.lower() == "true"
    if dtbp_check:
        payload["dtbp_check"] = dtbp_check
    if trade_confirm_email:
        payload["trade_confirm_email"] = trade_confirm_email
    data, err = _alpaca_http("PATCH", f"{base}/v2/account/configurations", mode=mode, json=payload)
    if err:
        return err
    return f"Account configurations updated [{_alpaca_mode_label(mode)}]: {data}"


@tool
def alpaca_get_activities(
    activity_type: Annotated[
        str,
        "Activity type filter: FILL, DIV, TRANS, CSR, etc. Empty = all types.",
    ] = "",
    date: Annotated[str, "Single date filter YYYY-MM-DD (optional)"] = "",
    until: Annotated[str, "Return activities before this date YYYY-MM-DD (optional)"] = "",
    after: Annotated[str, "Return activities after this date YYYY-MM-DD (optional)"] = "",
    page_size: Annotated[int, "Max activities to return"] = 50,
    mode: str = "paper",
) -> str:
    """Get Alpaca account activities (fills, dividends, transfers, journal entries, etc.)."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        kwargs: dict = {"page_size": page_size}
        if activity_type:
            kwargs["activity_types"] = activity_type.upper()
        if date:
            kwargs["date"] = date
        if until:
            kwargs["until"] = until
        if after:
            kwargs["after"] = after
        activities = api.get_activities(**kwargs)
        if not activities:
            return "No activities found for filter."
        lines = [f"Alpaca activities [{_alpaca_mode_label(mode)}] ({len(activities)}):"]
        for a in activities[:page_size]:
            atype = getattr(a, "activity_type", "?")
            ts = getattr(a, "transaction_time", None) or getattr(a, "date", "?")
            sym = getattr(a, "symbol", "")
            qty = getattr(a, "qty", "")
            price = getattr(a, "price", "")
            side = getattr(a, "side", "")
            net = getattr(a, "net_amount", "")
            lines.append(f"  [{atype}] {ts} {sym} {side} qty={qty} price={price} net={net}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_activities: {exc}"


# ─────────────────────────────────── watchlists ──────────────────────────────


@tool
def alpaca_list_watchlists(mode: str = "paper") -> str:
    """List all Alpaca watchlists for the account."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        wls = api.get_watchlists()
        if not wls:
            return "No watchlists configured."
        lines = [f"Alpaca watchlists [{_alpaca_mode_label(mode)}] ({len(wls)}):"]
        for w in wls:
            lines.append(f"  [{getattr(w, 'id', '?')}] {getattr(w, 'name', '?')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_list_watchlists: {exc}"


@tool
def alpaca_get_watchlist(
    watchlist_id: Annotated[str, "Watchlist ID"],
    mode: str = "paper",
) -> str:
    """Get the contents of an Alpaca watchlist."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        w = api.get_watchlist(watchlist_id)
        assets = getattr(w, "assets", []) or []
        lines = [f"Watchlist '{getattr(w, 'name', '?')}' ({len(assets)} symbols):"]
        for a in assets:
            sym = getattr(a, "symbol", None) or (a.get("symbol") if isinstance(a, dict) else "?")
            lines.append(f"  - {sym}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_watchlist({watchlist_id}): {exc}"


@tool
def alpaca_create_watchlist(
    name: Annotated[str, "Watchlist name"],
    symbols: Annotated[str, "Comma-separated tickers (e.g. 'AAPL,MSFT,GOOG')"] = "",
    mode: str = "paper",
) -> str:
    """Create a new Alpaca watchlist."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        w = api.add_watchlist(name=name, symbols=sym_list)
        return (
            f"Watchlist created [{_alpaca_mode_label(mode)}]: "
            f"[{getattr(w, 'id', '?')}] {getattr(w, 'name', name)}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_create_watchlist: {exc}"


@tool
def alpaca_add_to_watchlist(
    watchlist_id: Annotated[str, "Watchlist ID"],
    symbol: Annotated[str, "Ticker to add"],
    mode: str = "paper",
) -> str:
    """Add a symbol to an existing Alpaca watchlist."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        api.add_to_watchlist(watchlist_id, symbol.upper())
        return f"Added {symbol.upper()} to watchlist {watchlist_id}."
    except Exception as exc:
        return f"[tool-error] alpaca_add_to_watchlist: {exc}"


@tool
def alpaca_remove_from_watchlist(
    watchlist_id: Annotated[str, "Watchlist ID"],
    symbol: Annotated[str, "Ticker to remove"],
    mode: str = "paper",
) -> str:
    """Remove a symbol from an Alpaca watchlist."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        api.delete_from_watchlist(watchlist_id, symbol.upper())
        return f"Removed {symbol.upper()} from watchlist {watchlist_id}."
    except Exception as exc:
        return f"[tool-error] alpaca_remove_from_watchlist: {exc}"


@tool
def alpaca_delete_watchlist(
    watchlist_id: Annotated[str, "Watchlist ID to delete"],
    mode: str = "paper",
) -> str:
    """Delete an Alpaca watchlist entirely."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        api.delete_watchlist(watchlist_id)
        return f"Deleted watchlist {watchlist_id}."
    except Exception as exc:
        return f"[tool-error] alpaca_delete_watchlist: {exc}"


# ─────────────────────────────────── market data (latest quote/trade/snapshot)


@tool
def alpaca_get_latest_quote(
    symbol: Annotated[str, "Ticker symbol"],
    mode: str = "paper",
) -> str:
    """Get the latest NBBO quote (bid/ask) for a symbol."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        q = api.get_latest_quote(symbol.upper())
        bp = getattr(q, "bp", None) or getattr(q, "bid_price", "?")
        ap = getattr(q, "ap", None) or getattr(q, "ask_price", "?")
        bs = getattr(q, "bs", None) or getattr(q, "bid_size", "?")
        as_ = getattr(q, "as", None) or getattr(q, "ask_size", "?")
        ts = getattr(q, "t", None) or getattr(q, "timestamp", "?")
        return (
            f"{symbol.upper()} quote:\n  Bid: ${bp} x {bs}\n  Ask: ${ap} x {as_}\n  Timestamp: {ts}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_latest_quote({symbol}): {exc}"


@tool
def alpaca_get_latest_trade(
    symbol: Annotated[str, "Ticker symbol"],
    mode: str = "paper",
) -> str:
    """Get the most recent trade print for a symbol."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        t = api.get_latest_trade(symbol.upper())
        price = getattr(t, "price", None) or getattr(t, "p", "?")
        size = getattr(t, "size", None) or getattr(t, "s", "?")
        ts = getattr(t, "timestamp", None) or getattr(t, "t", "?")
        return f"{symbol.upper()} last trade: ${price} x {size} @ {ts}"
    except Exception as exc:
        return f"[tool-error] alpaca_get_latest_trade({symbol}): {exc}"


@tool
def alpaca_get_snapshot(
    symbol: Annotated[str, "Ticker symbol"],
    mode: str = "paper",
) -> str:
    """Get a market snapshot for a symbol (latest trade + quote + minute/daily bar)."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    try:
        s = api.get_snapshot(symbol.upper())
        latest_trade = getattr(s, "latest_trade", None)
        latest_quote = getattr(s, "latest_quote", None)
        minute_bar = getattr(s, "minute_bar", None)
        daily_bar = getattr(s, "daily_bar", None)
        prev_daily = getattr(s, "prev_daily_bar", None)

        def _safe(obj, attr, default="?"):
            return getattr(obj, attr, default) if obj else default

        return (
            f"{symbol.upper()} snapshot:\n"
            f"  Last trade: ${_safe(latest_trade, 'price')} @ {_safe(latest_trade, 'timestamp')}\n"
            f"  Bid/Ask:    ${_safe(latest_quote, 'bp', _safe(latest_quote, 'bid_price'))} / "
            f"${_safe(latest_quote, 'ap', _safe(latest_quote, 'ask_price'))}\n"
            f"  Minute bar: O={_safe(minute_bar, 'o')} H={_safe(minute_bar, 'h')} "
            f"L={_safe(minute_bar, 'l')} C={_safe(minute_bar, 'c')} V={_safe(minute_bar, 'v')}\n"
            f"  Daily bar:  O={_safe(daily_bar, 'o')} H={_safe(daily_bar, 'h')} "
            f"L={_safe(daily_bar, 'l')} C={_safe(daily_bar, 'c')} V={_safe(daily_bar, 'v')}\n"
            f"  Prev close: {_safe(prev_daily, 'c')}"
        )
    except Exception as exc:
        return f"[tool-error] alpaca_get_snapshot({symbol}): {exc}"


@tool
def alpaca_get_multi_snapshot(
    symbols: Annotated[str, "Comma-separated tickers (e.g. 'AAPL,MSFT,GOOG')"],
    mode: str = "paper",
) -> str:
    """Get latest snapshots for multiple symbols at once."""
    api, err = _alpaca_api(mode)
    if err:
        return err
    assert api is not None
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return "[tool-error] alpaca_get_multi_snapshot: no symbols provided."
    try:
        snaps = api.get_snapshots(sym_list)
        if not snaps:
            return "No snapshots returned."
        lines = [f"Snapshots ({len(snaps)}):"]
        lines.append(f"  {'Symbol':<8} {'Last':>10} {'Bid':>10} {'Ask':>10} {'PrevClose':>11}")
        for sym, snap in snaps.items() if isinstance(snaps, dict) else []:
            lt = getattr(snap, "latest_trade", None)
            lq = getattr(snap, "latest_quote", None)
            pd = getattr(snap, "prev_daily_bar", None)
            last = getattr(lt, "price", "?") if lt else "?"
            bid = (getattr(lq, "bp", None) or getattr(lq, "bid_price", "?")) if lq else "?"
            ask = (getattr(lq, "ap", None) or getattr(lq, "ask_price", "?")) if lq else "?"
            prev_close = getattr(pd, "c", "?") if pd else "?"
            lines.append(f"  {sym:<8} {last!s:>10} {bid!s:>10} {ask!s:>10} {prev_close!s:>11}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] alpaca_get_multi_snapshot: {exc}"


# ─────────────────────────────────── news ────────────────────────────────────


@tool
def alpaca_get_news(
    symbols: Annotated[str, "Comma-separated tickers to filter (empty = market-wide)"] = "",
    limit: Annotated[int, "Max articles (1-50)"] = 10,
    start: Annotated[str, "ISO-8601 start time (optional)"] = "",
    end: Annotated[str, "ISO-8601 end time (optional)"] = "",
    mode: str = "paper",
) -> str:
    """Get financial news from Alpaca's news API."""
    params: dict[str, Any] = {"limit": max(1, min(limit, 50))}
    if symbols:
        params["symbols"] = symbols.upper().replace(" ", "")
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    data, err = _alpaca_http("GET", f"{_ALPACA_DATA_URL}/v1beta1/news", mode=mode, params=params)
    if err:
        return err
    articles = (data or {}).get("news", [])
    if not articles:
        return "No news found."
    lines = [f"Alpaca news ({len(articles)}):"]
    for a in articles:
        ts = (a.get("created_at") or "")[:19]
        headline = a.get("headline", "?")
        source = a.get("source", "?")
        syms = ",".join(a.get("symbols", []) or [])
        url = a.get("url", "")
        lines.append(f"  [{ts}] {source} | {syms} | {headline}")
        if url:
            lines.append(f"    {url}")
    return "\n".join(lines)


# ─────────────────────────────────── crypto ──────────────────────────────────


@tool
def alpaca_get_crypto_bars(
    symbol: Annotated[str, "Crypto pair (e.g. 'BTC/USD' or 'ETH/USD')"],
    timeframe: Annotated[str, "1Min, 5Min, 15Min, 1Hour, 1Day"] = "1Hour",
    limit: Annotated[int, "Max bars"] = 24,
    mode: str = "paper",
) -> str:
    """Get OHLCV bars for a crypto pair."""
    sym = symbol.upper().replace(" ", "")
    params = {"symbols": sym, "timeframe": timeframe, "limit": limit}
    data, err = _alpaca_http(
        "GET", f"{_ALPACA_DATA_URL}/v1beta3/crypto/us/bars", mode=mode, params=params
    )
    if err:
        return err
    bars_map = (data or {}).get("bars", {})
    bars = bars_map.get(sym, [])
    if not bars:
        return f"No bar data for {sym}."
    lines = [f"{sym} crypto bars ({timeframe}, last {len(bars)}):"]
    lines.append(
        f"  {'Time':<20} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>14}"
    )
    for b in bars[-limit:]:
        ts = (b.get("t") or "")[:19]
        lines.append(
            f"  {ts:<20} {b.get('o', 0):>10.4f} {b.get('h', 0):>10.4f} "
            f"{b.get('l', 0):>10.4f} {b.get('c', 0):>10.4f} {b.get('v', 0):>14.4f}"
        )
    return "\n".join(lines)


@tool
def alpaca_get_crypto_quote(
    symbol: Annotated[str, "Crypto pair (e.g. 'BTC/USD')"],
    mode: str = "paper",
) -> str:
    """Get the latest crypto bid/ask quote."""
    sym = symbol.upper().replace(" ", "")
    data, err = _alpaca_http(
        "GET",
        f"{_ALPACA_DATA_URL}/v1beta3/crypto/us/latest/quotes",
        mode=mode,
        params={"symbols": sym},
    )
    if err:
        return err
    quotes = (data or {}).get("quotes", {})
    q = quotes.get(sym)
    if not q:
        return f"No quote for {sym}."
    return (
        f"{sym} crypto quote:\n"
        f"  Bid: ${q.get('bp', '?')} x {q.get('bs', '?')}\n"
        f"  Ask: ${q.get('ap', '?')} x {q.get('as', '?')}\n"
        f"  Timestamp: {q.get('t', '?')}"
    )


# ─────────────────────────────────── options ─────────────────────────────────


@tool
def alpaca_list_option_contracts(
    underlying_symbol: Annotated[str, "Underlying ticker (e.g. AAPL)"],
    expiration_date: Annotated[str, "YYYY-MM-DD (optional)"] = "",
    contract_type: Annotated[str, "'call' or 'put' (empty = both)"] = "",
    strike_price_gte: Annotated[float, "Min strike (0 = no min)"] = 0.0,
    strike_price_lte: Annotated[float, "Max strike (0 = no max)"] = 0.0,
    limit: Annotated[int, "Max contracts to return"] = 50,
    mode: str = "paper",
) -> str:
    """List Alpaca option contracts for an underlying symbol."""
    params: dict[str, Any] = {
        "underlying_symbols": underlying_symbol.upper(),
        "limit": limit,
        "status": "active",
    }
    if expiration_date:
        params["expiration_date"] = expiration_date
    if contract_type:
        params["type"] = contract_type.lower()
    if strike_price_gte > 0:
        params["strike_price_gte"] = strike_price_gte
    if strike_price_lte > 0:
        params["strike_price_lte"] = strike_price_lte
    base = _trading_base(mode)
    data, err = _alpaca_http("GET", f"{base}/v2/options/contracts", mode=mode, params=params)
    if err:
        return err
    contracts = (data or {}).get("option_contracts", [])
    if not contracts:
        return f"No option contracts found for {underlying_symbol.upper()}."
    lines = [
        f"Option contracts for {underlying_symbol.upper()} ({len(contracts)} shown):",
        f"  {'Symbol':<22} {'Type':<5} {'Strike':>9} {'Expires':<12} {'Open Int':>10}",
    ]
    for c in contracts[:limit]:
        sym = c.get("symbol", "?")
        ctype = c.get("type", "?")
        strike = c.get("strike_price", "?")
        exp = c.get("expiration_date", "?")
        oi = c.get("open_interest", "?")
        lines.append(f"  {sym:<22} {ctype:<5} {strike!s:>9} {exp:<12} {oi!s:>10}")
    return "\n".join(lines)


@tool
def alpaca_get_option_snapshot(
    symbol: Annotated[str, "Full OCC option symbol (e.g. AAPL250117C00150000)"],
    mode: str = "paper",
) -> str:
    """Get a snapshot (greeks + latest quote/trade) for a single option contract."""
    params = {"symbols": symbol.upper()}
    data, err = _alpaca_http(
        "GET",
        f"{_ALPACA_DATA_URL}/v1beta1/options/snapshots",
        mode=mode,
        params=params,
    )
    if err:
        return err
    snaps = (data or {}).get("snapshots", {})
    snap = snaps.get(symbol.upper())
    if not snap:
        return f"No snapshot for {symbol.upper()}."
    lt = snap.get("latestTrade", {}) or {}
    lq = snap.get("latestQuote", {}) or {}
    greeks = snap.get("greeks", {}) or {}
    return (
        f"{symbol.upper()} option snapshot:\n"
        f"  Last trade: ${lt.get('p', '?')} @ {lt.get('t', '?')}\n"
        f"  Bid/Ask:    ${lq.get('bp', '?')} / ${lq.get('ap', '?')}\n"
        f"  IV: {snap.get('impliedVolatility', '?')}\n"
        f"  Δ delta={greeks.get('delta', '?')} Γ gamma={greeks.get('gamma', '?')} "
        f"Θ theta={greeks.get('theta', '?')} ν vega={greeks.get('vega', '?')} "
        f"ρ rho={greeks.get('rho', '?')}"
    )


@tool
def alpaca_get_option_chain(
    underlying_symbol: Annotated[str, "Underlying ticker (e.g. AAPL)"],
    expiration_date: Annotated[str, "YYYY-MM-DD (optional, narrows the chain)"] = "",
    mode: str = "paper",
) -> str:
    """Get the option chain snapshot for an underlying symbol."""
    params: dict[str, Any] = {}
    if expiration_date:
        params["expiration_date"] = expiration_date
    data, err = _alpaca_http(
        "GET",
        f"{_ALPACA_DATA_URL}/v1beta1/options/snapshots/{underlying_symbol.upper()}",
        mode=mode,
        params=params,
    )
    if err:
        return err
    snaps = (data or {}).get("snapshots", {})
    if not snaps:
        return f"No option chain found for {underlying_symbol.upper()}."
    lines = [
        f"Option chain for {underlying_symbol.upper()} ({len(snaps)} contracts):",
        f"  {'OCC Symbol':<22} {'Bid':>8} {'Ask':>8} {'IV':>7} {'Δ':>7}",
    ]
    for occ, snap in list(snaps.items())[:80]:
        lq = snap.get("latestQuote", {}) or {}
        greeks = snap.get("greeks", {}) or {}
        lines.append(
            f"  {occ:<22} {lq.get('bp', '?')!s:>8} {lq.get('ap', '?')!s:>8} "
            f"{str(snap.get('impliedVolatility', '?'))[:6]:>7} "
            f"{str(greeks.get('delta', '?'))[:6]:>7}"
        )
    return "\n".join(lines)


# ─────────────────────────────────── screener / movers ───────────────────────


@tool
def alpaca_get_top_movers(
    market_type: Annotated[str, "'stocks' or 'crypto'"] = "stocks",
    top: Annotated[int, "Number of gainers + losers to show"] = 10,
    mode: str = "paper",
) -> str:
    """Get the top market movers (gainers and losers) by % change."""
    params = {"top": top}
    data, err = _alpaca_http(
        "GET",
        f"{_ALPACA_DATA_URL}/v1beta1/screener/{market_type.lower()}/movers",
        mode=mode,
        params=params,
    )
    if err:
        return err
    gainers = (data or {}).get("gainers", [])
    losers = (data or {}).get("losers", [])
    if not gainers and not losers:
        return f"No movers data for {market_type}."
    lines = [f"Top {market_type} movers (last update: {(data or {}).get('last_updated', '?')})"]
    lines.append("\n  ▲ Gainers:")
    for g in gainers[:top]:
        lines.append(
            f"    {g.get('symbol', '?'):<8} ${g.get('price', '?')} "
            f"({g.get('percent_change', '?')}%) chg=${g.get('change', '?')}"
        )
    lines.append("\n  ▼ Losers:")
    for losseritem in losers[:top]:
        lines.append(
            f"    {losseritem.get('symbol', '?'):<8} ${losseritem.get('price', '?')} "
            f"({losseritem.get('percent_change', '?')}%) chg=${losseritem.get('change', '?')}"
        )
    return "\n".join(lines)


get_tools, get_tool = make_toolkit_api(globals())
