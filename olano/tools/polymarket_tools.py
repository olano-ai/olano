"""Polymarket public API tools — prediction market discovery and price data.

Auth: none. Polymarket trading goes through the CLOB API which requires an
EVM wallet signature flow that's too sensitive for a generic agent toolkit.
This module exposes only the public *Gamma* (events/markets metadata) and
*CLOB* (orderbook / midpoint) read endpoints.

Hosts:
  Gamma: https://gamma-api.polymarket.com
  CLOB:  https://clob.polymarket.com
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_GAMMA = "https://gamma-api.polymarket.com"
_CLOB = "https://clob.polymarket.com"


def _get(host: str, path: str, params: dict | None = None) -> list | dict:
    r = httpx.get(f"{host}{path}", params=params or {}, timeout=15)
    r.raise_for_status()
    return cast("list | dict", r.json())


@tool
def polymarket_search_markets(
    query: Annotated[str, "Free-text search across Polymarket event titles"],
    active_only: Annotated[bool, "Only return active markets"] = True,
    limit: Annotated[int, "Max events to return"] = 10,
) -> str:
    """Search Polymarket events by free-text query."""
    try:
        params: dict = {"limit": limit, "search": query}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        events = _get(_GAMMA, "/events", params)
    except Exception as e:
        return f"[tool-error] polymarket_search_markets: {e}"
    if not isinstance(events, list) or not events:
        return f"No events match '{query}'."
    lines = [f"Polymarket events matching '{query}' ({len(events)}):"]
    for ev in events[:limit]:
        vol = float(ev.get("volume") or 0)
        liq = float(ev.get("liquidity") or 0)
        lines.append(
            f"  • {ev.get('title', '?')}\n"
            f"    Slug: {ev.get('slug', '?')}\n"
            f"    Vol: ${vol:,.0f}  Liquidity: ${liq:,.0f}  "
            f"End: {(ev.get('endDate') or '?')[:10]}"
        )
    return "\n".join(lines)


@tool
def polymarket_trending(
    limit: Annotated[int, "Max events to return"] = 10,
) -> str:
    """Get trending Polymarket events (sorted by 24h volume)."""
    try:
        events = _get(
            _GAMMA,
            "/events",
            {
                "limit": limit,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        return f"[tool-error] polymarket_trending: {e}"
    if not isinstance(events, list) or not events:
        return "No trending events."
    lines = [f"Trending Polymarket events ({len(events)}):"]
    for ev in events[:limit]:
        vol24 = float(ev.get("volume24hr") or 0)
        lines.append(
            f"  • {ev.get('title', '?')[:80]}\n"
            f"    Vol 24h: ${vol24:,.0f}  Slug: {ev.get('slug', '?')}"
        )
    return "\n".join(lines)


@tool
def polymarket_get_event(
    slug: Annotated[str, "Polymarket event slug (from polymarket_search_markets)"],
) -> str:
    """Get full details for a Polymarket event, including each outcome's current price."""
    try:
        events = _get(_GAMMA, "/events", {"slug": slug})
    except Exception as e:
        return f"[tool-error] polymarket_get_event({slug}): {e}"
    if not isinstance(events, list) or not events:
        return f"No event for slug '{slug}'."
    ev = events[0]
    lines = [
        f"{ev.get('title', '?')}",
        f"  Slug: {ev.get('slug')}",
        f"  Description: {(ev.get('description') or '')[:240]}",
        f"  Volume: ${float(ev.get('volume') or 0):,.0f}",
        f"  Liquidity: ${float(ev.get('liquidity') or 0):,.0f}",
        f"  End date: {ev.get('endDate', '?')[:10]}",
        f"  Status: {'closed' if ev.get('closed') else ('active' if ev.get('active') else 'inactive')}",
    ]
    markets = ev.get("markets", [])
    if markets:
        lines.append(f"\n  Markets ({len(markets)}):")
        for m in markets:
            outcome_prices = m.get("outcomePrices", "")
            outcomes = m.get("outcomes", "")
            lines.append(
                f"    • {m.get('question', '?')}\n"
                f"      Outcomes: {outcomes}\n"
                f"      Prices:   {outcome_prices}\n"
                f"      Token IDs: {m.get('clobTokenIds', '')}"
            )
    return "\n".join(lines)


@tool
def polymarket_get_price(
    token_id: Annotated[str, "CLOB token ID (from polymarket_get_event)"],
    side: Annotated[str, "'BUY' or 'SELL' — perspective for the price"] = "BUY",
) -> str:
    """Get the current best price for a Polymarket outcome token."""
    try:
        data = _get(_CLOB, "/price", {"token_id": token_id, "side": side.upper()})
    except Exception as e:
        return f"[tool-error] polymarket_get_price: {e}"
    if not isinstance(data, dict):
        return f"Unexpected response: {data}"
    return f"Token {token_id[:12]}… {side.upper()} price: {data.get('price', '?')}"


@tool
def polymarket_get_midpoint(
    token_id: Annotated[str, "CLOB token ID"],
) -> str:
    """Get the midpoint price for a Polymarket outcome token."""
    try:
        data = _get(_CLOB, "/midpoint", {"token_id": token_id})
    except Exception as e:
        return f"[tool-error] polymarket_get_midpoint: {e}"
    return f"Token {token_id[:12]}… midpoint: {data.get('mid', '?') if isinstance(data, dict) else data}"


@tool
def polymarket_get_orderbook(
    token_id: Annotated[str, "CLOB token ID"],
    depth: Annotated[int, "Number of bid/ask levels to show"] = 10,
) -> str:
    """Get the order book for a Polymarket outcome token."""
    try:
        data = _get(_CLOB, "/book", {"token_id": token_id})
    except Exception as e:
        return f"[tool-error] polymarket_get_orderbook: {e}"
    if not isinstance(data, dict):
        return f"Unexpected response: {data}"
    bids = data.get("bids", [])[:depth]
    asks = data.get("asks", [])[:depth]
    lines = [f"Polymarket order book for token {token_id[:12]}…"]
    lines.append(f"  {'Bid Price':>10} {'Bid Size':>14}     {'Ask Size':>14} {'Ask Price':>10}")
    for i in range(max(len(bids), len(asks))):
        b = bids[i] if i < len(bids) else {}
        a = asks[i] if i < len(asks) else {}
        lines.append(
            f"  {b.get('price', ''):>10} {b.get('size', ''):>14}     "
            f"{a.get('size', ''):>14} {a.get('price', ''):>10}"
        )
    return "\n".join(lines)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
