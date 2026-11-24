"""CoinGecko tools (free public API — no key required)."""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def coingecko_get_price(coin_id: str, vs_currency: str = "usd") -> str:
    """Get cryptocurrency price from CoinGecko. coin_id='bitcoin', 'ethereum', etc."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        resp = requests.get(
            url,
            params={"ids": coin_id, "vs_currencies": vs_currency, "include_24hr_change": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get(coin_id, {})
        price = data.get(vs_currency, "N/A")
        change = data.get(f"{vs_currency}_24h_change")
        return f"{coin_id}: {price} {vs_currency.upper()}" + (
            f" ({change:+.2f}% 24h)" if change is not None else ""
        )
    except Exception as exc:
        return f"[tool-error] coingecko_get_price({coin_id}): {exc}"


@tool
def coingecko_trending() -> str:
    """Get trending cryptocurrencies from CoinGecko."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        resp.raise_for_status()
        coins = resp.json().get("coins", [])[:7]
        lines = [
            f"{c['item']['market_cap_rank'] or '?'}. {c['item']['name']} ({c['item']['symbol']})"
            for c in coins
        ]
        return "Trending:\n" + "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] coingecko_trending: {exc}"


@tool
def coingecko_top_coins(vs_currency: str = "usd", limit: int = 15) -> str:
    """Get top cryptocurrencies by market cap from CoinGecko. limit up to 250."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": vs_currency,
                "order": "market_cap_desc",
                "per_page": min(limit, 250),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
            timeout=15,
        )
        resp.raise_for_status()
        coins = resp.json()
        lines = [f"Top {len(coins)} by market cap ({vs_currency.upper()}):"]
        for i, c in enumerate(coins, 1):
            change = c.get("price_change_percentage_24h") or 0
            lines.append(
                f"  {i:>3}. {c['name']:<20} ${c['current_price']:>12,.4f}  "
                f"Mkt cap: ${c['market_cap']:>15,.0f}  {change:+.2f}% 24h"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] coingecko_top_coins: {exc}"


@tool
def coingecko_get_coin_info(coin_id: str) -> str:
    """Get detailed info for a coin from CoinGecko: description, links, ATH, supply, etc."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "community_data": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        d = resp.json()
        market = d.get("market_data", {})
        desc = d.get("description", {}).get("en", "")
        desc_short = (desc[:300] + "…") if len(desc) > 300 else desc
        lines = [
            f"{d.get('name')} ({d.get('symbol', '').upper()}) — rank #{d.get('market_cap_rank')}",
            f"  Price (USD): ${market.get('current_price', {}).get('usd', 'N/A'):,}",
            f"  ATH: ${market.get('ath', {}).get('usd', 'N/A'):,}"
            f"  (on {market.get('ath_date', {}).get('usd', 'N/A')[:10]})",
            f"  Market cap: ${market.get('market_cap', {}).get('usd', 0):,.0f}",
            f"  Circulating supply: {market.get('circulating_supply', 'N/A'):,}",
            f"  Max supply: {market.get('max_supply', 'N/A')}",
            f"  Homepage: {(d.get('links', {}).get('homepage') or ['N/A'])[0]}",
        ]
        if desc_short:
            lines.append(f"\n  About: {desc_short}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] coingecko_get_coin_info({coin_id}): {exc}"


@tool
def coingecko_get_ohlc(coin_id: str, vs_currency: str = "usd", days: int = 14) -> str:
    """Get OHLC candlestick data for a coin from CoinGecko. days=1,7,14,30,90,180,365."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
            params={"vs_currency": vs_currency, "days": days},
            timeout=15,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return f"No OHLC data for {coin_id}."
        import time as _time

        lines = [f"{coin_id} OHLC ({days}d, {vs_currency.upper()}) — {len(candles)} candles:"]
        lines.append(f"  {'Date':<12} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10}")
        for ts, o, h, l, c in candles[-10:]:
            date = _time.strftime("%Y-%m-%d", _time.gmtime(ts / 1000))
            lines.append(f"  {date:<12} {o:>10.4f} {h:>10.4f} {l:>10.4f} {c:>10.4f}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] coingecko_get_ohlc({coin_id}): {exc}"


@tool
def coingecko_search(query: str) -> str:
    """Search CoinGecko for coins, exchanges, or categories matching a query string."""
    requests, err = optional_import("requests", purpose="HTTP calls to CoinGecko")
    if err:
        return err
    assert requests is not None
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        coins = data.get("coins", [])[:8]
        lines = [f"CoinGecko search results for '{query}':"]
        for c in coins:
            lines.append(
                f"  {c['name']} ({c['symbol']})  id={c['id']}  rank=#{c.get('market_cap_rank', '?')}"
            )
        return "\n".join(lines) if len(lines) > 1 else "No results found."
    except Exception as exc:
        return f"[tool-error] coingecko_search({query}): {exc}"


get_tools, get_tool = make_toolkit_api(globals())
