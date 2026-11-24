"""Interactive Brokers tools — account, positions, market data, trading.

Uses ``ib_insync`` to connect to a running TWS or IB Gateway instance.

Connection
----------
TWS / IB Gateway must be running locally with the API enabled. Default ports:
  Paper trading: 7497 (TWS) or 4002 (Gateway)
  Live trading:  7496 (TWS) or 4001 (Gateway)

Each tool accepts ``mode='paper'`` (default, port 7497) or ``mode='live'``
(port 7496). Override the host/port with ``IBKR_HOST``, ``IBKR_PAPER_PORT``,
``IBKR_LIVE_PORT``, and the client ID with ``IBKR_CLIENT_ID``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import install_hint, optional_import

logger = logging.getLogger(__name__)


def _ibkr_connect(mode: str) -> tuple[Any | None, str | None]:
    """Return (IB instance or None, error_str)."""
    ib_insync, err = optional_import("ib_insync", purpose="Interactive Brokers TWS/Gateway access")
    if err or ib_insync is None:
        return None, install_hint("ib_insync", purpose="Interactive Brokers TWS/Gateway access")
    host = get_credential("IBKR_HOST", "127.0.0.1")
    is_live = (mode or "paper").strip().lower() == "live"
    if is_live:
        port = int(get_credential("IBKR_LIVE_PORT", "7496"))
    else:
        port = int(get_credential("IBKR_PAPER_PORT", "7497"))
    client_id = int(get_credential("IBKR_CLIENT_ID", "42"))
    ib = ib_insync.IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=8)
    except Exception as e:
        return None, (
            f"[connection-error] Could not connect to IBKR at {host}:{port}. "
            f"Is TWS/Gateway running with API enabled? ({e})"
        )
    return ib, None


def _label(mode: str) -> str:
    return "LIVE" if (mode or "paper").lower() == "live" else "paper"


@tool
def ibkr_get_account_summary(mode: str = "paper") -> str:
    """Get IBKR account summary (NetLiq, cash, buying power). mode='paper' (default) or 'live'."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        rows = ib.accountSummary()
        keep = {
            "NetLiquidation",
            "TotalCashValue",
            "BuyingPower",
            "GrossPositionValue",
            "UnrealizedPnL",
            "RealizedPnL",
            "AvailableFunds",
            "ExcessLiquidity",
        }
        items = [r for r in rows if getattr(r, "tag", None) in keep]
        if not items:
            return f"No account data [{_label(mode)}]."
        lines = [f"IBKR account summary [{_label(mode)}]:"]
        for r in items:
            tag = getattr(r, "tag", "?")
            value = getattr(r, "value", "?")
            currency = getattr(r, "currency", "")
            lines.append(f"  {tag:<22} {value} {currency}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] ibkr_get_account_summary: {e}"
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


@tool
def ibkr_list_positions(mode: str = "paper") -> str:
    """List all open IBKR positions."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        positions = ib.positions()
        if not positions:
            return f"No open positions [{_label(mode)}]."
        lines = [f"IBKR positions [{_label(mode)}] ({len(positions)}):"]
        lines.append(f"  {'Symbol':<10} {'SecType':<8} {'Qty':>10} {'Avg Cost':>12}")
        for p in positions:
            contract = getattr(p, "contract", None)
            symbol = getattr(contract, "symbol", "?") if contract else "?"
            sec_type = getattr(contract, "secType", "?") if contract else "?"
            qty = float(getattr(p, "position", 0) or 0)
            avg = float(getattr(p, "avgCost", 0) or 0)
            lines.append(f"  {symbol:<10} {sec_type:<8} {qty:>10.4f} {avg:>12.4f}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] ibkr_list_positions: {e}"
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


@tool
def ibkr_get_quote(
    symbol: Annotated[str, "Stock symbol (e.g. 'AAPL')"],
    sec_type: Annotated[str, "Security type: STK, FUT, OPT, CASH"] = "STK",
    exchange: Annotated[str, "Exchange (default 'SMART')"] = "SMART",
    currency: Annotated[str, "Currency (default 'USD')"] = "USD",
    mode: str = "paper",
) -> str:
    """Get a real-time IBKR quote for a contract. Requires market data subscription."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        ib_insync, _ = optional_import("ib_insync", purpose="ibkr_get_quote")
        assert ib_insync is not None
        contract = ib_insync.Contract(
            symbol=symbol.upper(), secType=sec_type, exchange=exchange, currency=currency
        )
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
        ib.sleep(2.0)  # wait for data
        return (
            f"{symbol.upper()} ({sec_type}/{exchange}/{currency}) [{_label(mode)}]\n"
            f"  Last: {ticker.last}\n"
            f"  Bid:  {ticker.bid}  ({ticker.bidSize})\n"
            f"  Ask:  {ticker.ask}  ({ticker.askSize})\n"
            f"  High: {ticker.high}  Low: {ticker.low}\n"
            f"  Volume: {ticker.volume}"
        )
    except Exception as e:
        return f"[tool-error] ibkr_get_quote({symbol}): {e}"
    finally:
        ib.disconnect()


@tool
def ibkr_list_open_orders(mode: str = "paper") -> str:
    """List open IBKR orders."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        trades = ib.openTrades()
        if not trades:
            return f"No open orders [{_label(mode)}]."
        lines = [f"IBKR open orders [{_label(mode)}] ({len(trades)}):"]
        for t in trades:
            o = getattr(t, "order", None)
            c = getattr(t, "contract", None)
            status = getattr(getattr(t, "orderStatus", None), "status", "?")
            lines.append(
                f"  [{getattr(o, 'orderId', '?')}] {getattr(c, 'symbol', '?')} "
                f"{getattr(o, 'action', '?')} {getattr(o, 'totalQuantity', '?')} "
                f"{getattr(o, 'orderType', '?')}  lmt={getattr(o, 'lmtPrice', '?')}  "
                f"status={status}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] ibkr_list_open_orders: {e}"
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


@tool
def ibkr_place_order(
    symbol: Annotated[str, "Stock symbol (e.g. 'AAPL')"],
    action: Annotated[str, "'BUY' or 'SELL'"],
    quantity: Annotated[float, "Share quantity"],
    order_type: Annotated[str, "'MKT' or 'LMT'"] = "MKT",
    limit_price: Annotated[float, "Required for LMT"] = 0.0,
    sec_type: Annotated[str, "STK, FUT, OPT, CASH"] = "STK",
    exchange: Annotated[str, "Exchange"] = "SMART",
    currency: Annotated[str, "Currency"] = "USD",
    mode: str = "paper",
) -> str:
    """Place an IBKR order. mode='paper' (default, port 7497) or 'live' (port 7496)."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        ib_insync, _ = optional_import("ib_insync", purpose="ibkr_place_order")
        assert ib_insync is not None
        contract = ib_insync.Contract(
            symbol=symbol.upper(), secType=sec_type, exchange=exchange, currency=currency
        )
        ib.qualifyContracts(contract)
        if order_type.upper() == "MKT":
            order = ib_insync.MarketOrder(action.upper(), quantity)
        elif order_type.upper() == "LMT":
            if limit_price <= 0:
                return "[tool-error] ibkr_place_order: limit_price required for LMT orders."
            order = ib_insync.LimitOrder(action.upper(), quantity, limit_price)
        else:
            return f"[tool-error] ibkr_place_order: unsupported order_type '{order_type}'."
        trade = ib.placeOrder(contract, order)
        ib.sleep(1.0)
        label = _label(mode)
        warn = "  ⚠ LIVE TRADE — real funds.\n" if label == "LIVE" else ""
        return (
            f"IBKR order submitted [{label}]:\n"
            f"{warn}"
            f"  ID: {trade.order.orderId}\n"
            f"  {action.upper()} {quantity} {symbol.upper()} {order_type}"
            + (f" @ {limit_price}" if order_type.upper() == "LMT" else "")
            + f"\n  Status: {trade.orderStatus.status}"
        )
    except Exception as e:
        return f"[tool-error] ibkr_place_order: {e}"
    finally:
        ib.disconnect()


@tool
def ibkr_cancel_order(
    order_id: Annotated[int, "IBKR order ID to cancel"],
    mode: str = "paper",
) -> str:
    """Cancel a pending IBKR order by order ID."""
    ib, err = _ibkr_connect(mode)
    if err:
        return err
    assert ib is not None
    try:
        for trade in ib.openTrades():
            if trade.order.orderId == order_id:
                ib.cancelOrder(trade.order)
                ib.sleep(0.5)
                return f"IBKR order #{order_id} cancellation requested [{_label(mode)}]."
        return f"No open order with ID {order_id} found [{_label(mode)}]."
    except Exception as e:
        return f"[tool-error] ibkr_cancel_order: {e}"
    finally:
        ib.disconnect()


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
