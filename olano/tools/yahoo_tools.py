"""Yahoo Finance tools (yfinance)."""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def yahoo_get_quote(symbol: str) -> str:
    """Get a real-time stock quote from Yahoo Finance. symbol='AAPL', 'TSLA', etc."""
    yf, err = optional_import("yfinance", purpose="real-time Yahoo Finance quotes")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", "N/A")
        change = getattr(info, "regular_market_change", None)
        pct = getattr(info, "regular_market_change_percent", None)
        return f"{symbol}: ${price}" + (
            f" ({change:+.2f}, {pct:+.2f}%)" if change is not None else ""
        )
    except Exception as exc:
        return f"[tool-error] yahoo_get_quote({symbol}): {exc}"


@tool
def yahoo_get_history(symbol: str, period: str = "1mo", interval: str = "1d") -> str:
    """Get historical price data. period='1d','5d','1mo','3mo','6mo','1y'. interval='1d','1wk'."""
    yf, err = optional_import("yfinance", purpose="historical price data from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty:
            return f"No history found for {symbol}"
        first = hist.iloc[0]
        last = hist.iloc[-1]
        change = ((last["Close"] - first["Close"]) / first["Close"]) * 100
        return (
            f"{symbol} ({period}): "
            f"open=${first['Close']:.2f} → close=${last['Close']:.2f} "
            f"({change:+.2f}%)\n"
            f"{len(hist)} data points"
        )
    except Exception as exc:
        return f"[tool-error] yahoo_get_history({symbol}): {exc}"


@tool
def yahoo_get_financials(symbol: str) -> str:
    """Get key income-statement and balance-sheet metrics for a stock from Yahoo Finance."""
    yf, err = optional_import("yfinance", purpose="financial statements from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        fields = {
            "Market cap": info.get("marketCap"),
            "Revenue (TTM)": info.get("totalRevenue"),
            "Gross profit": info.get("grossProfits"),
            "Net income": info.get("netIncomeToCommon"),
            "EPS (TTM)": info.get("trailingEps"),
            "P/E (TTM)": info.get("trailingPE"),
            "Forward P/E": info.get("forwardPE"),
            "Debt/Equity": info.get("debtToEquity"),
            "ROE": info.get("returnOnEquity"),
            "Free cash flow": info.get("freeCashflow"),
        }
        lines = [f"{symbol} Financials:"]
        for label, val in fields.items():
            if val is None:
                continue
            if isinstance(val, float) and abs(val) > 1_000:
                formatted = f"${val:,.0f}"
            elif isinstance(val, int):
                formatted = f"${val:,}"
            else:
                formatted = str(val)
            lines.append(f"  {label}: {formatted}")
        return "\n".join(lines) if len(lines) > 1 else f"No financial data for {symbol}"
    except Exception as exc:
        return f"[tool-error] yahoo_get_financials({symbol}): {exc}"


@tool
def yahoo_get_dividends(symbol: str) -> str:
    """Get recent dividend history for a stock from Yahoo Finance (last 8 payments)."""
    yf, err = optional_import("yfinance", purpose="dividend history from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        divs = ticker.dividends
        if divs.empty:
            return f"{symbol}: no dividend history found."
        recent = divs.tail(8)
        lines = [f"{symbol} Dividends (recent):"]
        for date, amount in recent.items():
            lines.append(f"  {str(date)[:10]}: ${amount:.4f}")
        info = ticker.info
        yield_val = info.get("dividendYield")
        if yield_val:
            lines.append(f"  Current yield: {yield_val * 100:.2f}%")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] yahoo_get_dividends({symbol}): {exc}"


@tool
def yahoo_get_news(symbol: str) -> str:
    """Get the latest news headlines for a stock or ETF from Yahoo Finance."""
    yf, err = optional_import("yfinance", purpose="news from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        if not news:
            return f"No news found for {symbol}."
        lines = [f"{symbol} News:"]
        for item in news[:8]:
            title = item.get("title", "N/A")
            publisher = item.get("publisher", "")
            link = item.get("link", "")
            lines.append(f"  • {title} [{publisher}]\n    {link}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] yahoo_get_news({symbol}): {exc}"


@tool
def yahoo_compare_symbols(symbols: str) -> str:
    """Compare key stats across multiple symbols (comma-separated). e.g. 'AAPL,MSFT,GOOGL'."""
    yf, err = optional_import("yfinance", purpose="multi-symbol comparison from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not tickers:
            return "No valid symbols provided."
        rows = []
        for sym in tickers:
            info = yf.Ticker(sym).info
            rows.append(
                {
                    "Symbol": sym,
                    "Price": info.get("currentPrice") or info.get("regularMarketPrice", "N/A"),
                    "P/E": info.get("trailingPE", "N/A"),
                    "Mkt Cap ($B)": (
                        f"{info['marketCap'] / 1e9:.1f}" if info.get("marketCap") else "N/A"
                    ),
                    "52w High": info.get("fiftyTwoWeekHigh", "N/A"),
                    "52w Low": info.get("fiftyTwoWeekLow", "N/A"),
                }
            )
        header = (
            f"{'Symbol':<8} {'Price':>8} {'P/E':>7} {'Mkt Cap':>10} {'52w Hi':>9} {'52w Lo':>9}"
        )
        lines = ["Comparison:", header, "-" * len(header)]
        for r in rows:
            lines.append(
                f"{r['Symbol']:<8} {r['Price']!s:>8} {r['P/E']!s:>7} "
                f"{r['Mkt Cap ($B)']:>10} {r['52w High']!s:>9} {r['52w Low']!s:>9}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] yahoo_compare_symbols: {exc}"


@tool
def yahoo_get_options_summary(symbol: str) -> str:
    """Get a summary of available options expiry dates and near-ATM strikes for a symbol."""
    yf, err = optional_import("yfinance", purpose="options data from Yahoo Finance")
    if err:
        return err
    assert yf is not None  # narrowed: err is None ⇒ import succeeded
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return f"{symbol}: no options data available."
        lines = [f"{symbol} Options — expiry dates: {', '.join(expirations[:6])}"]
        chain = ticker.option_chain(expirations[0])
        calls = chain.calls.head(5)
        puts = chain.puts.head(5)
        lines.append(f"\nCalls (nearest expiry {expirations[0]}):")
        for _, row in calls.iterrows():
            lines.append(
                f"  Strike ${row['strike']:.2f}  bid={row['bid']:.2f}  ask={row['ask']:.2f}"
                f"  IV={row.get('impliedVolatility', 0):.1%}"
            )
        lines.append(f"\nPuts (nearest expiry {expirations[0]}):")
        for _, row in puts.iterrows():
            lines.append(
                f"  Strike ${row['strike']:.2f}  bid={row['bid']:.2f}  ask={row['ask']:.2f}"
                f"  IV={row.get('impliedVolatility', 0):.1%}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] yahoo_get_options_summary({symbol}): {exc}"


get_tools, get_tool = make_toolkit_api(globals())
