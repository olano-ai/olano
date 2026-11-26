"""Loose Change, a two-person podcast about money and the news: the team in the kalshi video.

In the take the agent's MCP tab adds Kalshi's official MCP server (``https://api.elections.kalshi.com/mcp``)
as a custom server, with no account. Only its public market tools are called (``search_markets``,
``get_event_odds``, ``get_market_links``); the account tools need a signed-in Kalshi account and are
never called. Each tool step is a real call to that server with the official ``mcp`` Python SDK, and
the reply is built from what came back. The reply reports what the market's prices imply, as
market prices: nothing here suggests placing a trade.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

URL = "https://api.elections.kalshi.com/mcp"

FED_SEARCH = {"query": "Fed decision December"}
SHOW_SEARCH = {"query": "Super Bowl halftime show performers"}


async def _call(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    text = "\n".join(str(getattr(c, "text", "")) for c in res.content)
    return bool(res.isError), text


def _real(tool: str, args: dict[str, Any]) -> str:
    """One real call; a busy upstream (HTTP 429) is asked again after a short wait."""
    text = ""
    for attempt in range(5):
        try:
            err, text = asyncio.run(_call(tool, args))
        except Exception as exc:
            err, text = True, f"{type(exc).__name__}: {exc}"
        if not err:
            return text
        if "429" not in text:
            break
        time.sleep(2.0 + 2.0 * attempt)
    return "Error: " + text


def _json(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pick_event(raw: str, prefix: str) -> dict[str, Any]:
    events = [e for e in _json(raw).get("events") or [] if isinstance(e, dict)]
    for ev in events:
        if str(ev.get("event_ticker", "")).startswith(prefix):
            return ev
    return events[0] if events else {}


def _search(key: str, args: dict[str, Any]):
    def run(ctx: dict[str, Any]) -> str:
        ctx[key] = _real("search_markets", args)
        return ctx[key]

    return run


def _odds_args(key: str, prefix: str, limit: int):
    def args(ctx: dict[str, Any]) -> dict[str, Any]:
        ev = _pick_event(str(ctx.get(key) or ""), prefix)
        ctx[key + "_event"] = ev
        return {"event_ticker": ev.get("event_ticker") or prefix, "limit": limit}

    return args


def _odds(key: str):
    def run(ctx: dict[str, Any]) -> str:
        ctx[key] = _real("get_event_odds", ctx["args"])
        return ctx[key]

    return run


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _chance(m: dict[str, Any]) -> float | None:
    """The market's implied chance of YES, in percent: the bid/ask midpoint, else the last trade."""
    bid, ask = _num(m.get("implied_yes_bid_percent")), _num(m.get("implied_yes_ask_percent"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    last = _num(m.get("last_price_dollars"))
    if last is not None:
        return last * 100
    return ask if ask is not None else bid


def _outcomes(raw: str) -> list[tuple[str, float, str]]:
    rows = []
    for m in _json(raw).get("markets") or []:
        if not isinstance(m, dict) or m.get("status") not in (None, "active", "open"):
            continue
        c = _chance(m)
        name = str(m.get("yes_sub_title") or m.get("title") or m.get("ticker") or "").strip()
        if c is None or not name:
            continue
        rows.append((name, c, str(m.get("ticker") or "")))
    rows.sort(key=lambda r: -r[1])
    return rows


def _pct(c: float) -> str:
    return "under 1%" if c < 1 else f"{c:.0f}%"


def _links_args(ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _outcomes(str(ctx.get("fed_odds") or ""))
    return {"market_ticker": rows[0][2] if rows else "KXFEDDECISION"}


def _links(ctx: dict[str, Any]) -> str:
    ctx["fed_links"] = _real("get_market_links", ctx["args"])
    return ctx["fed_links"]


def _fed_reply(ctx: dict[str, Any]) -> str:
    rows = _outcomes(str(ctx.get("fed_odds") or ""))
    if not rows:
        return (
            "Kalshi's server did not return prices for that market just now, so I have nothing "
            "to report yet. I can ask again in a minute."
        )
    ev = ctx.get("fed_event") or {}
    data = _json(str(ctx.get("fed_odds") or "")).get("event") or {}
    title = str(data.get("title") or ev.get("title") or "the December Fed decision")
    when = str(data.get("sub_title") or ev.get("period") or "").strip()
    cuts = [c for name, c, _ in rows if "cut" in name.lower()]
    lines = []
    if cuts:
        lines.append(
            f"The market puts a December cut, of any size, at about **{_pct(sum(cuts))}**.\n"
        )
    lines.append(f"Kalshi's market **{title}**" + (f" ({when})" if when else "") + ", just now:\n")
    lines += ["| Outcome | The market's chance |", "|---|---|"]
    lines += [f"| {name} | {_pct(c)} |" for name, c, _ in rows[:5]]
    lines.append(f"\nThe most likely outcome by the market's prices: **{rows[0][0]}**.")
    link = _json(str(ctx.get("fed_links") or "")).get("market_url") or ev.get("event_url")
    if link:
        lines.append(f"\n[Open this market on Kalshi]({link})")
    lines.append("\nThese are traders' prices, not a forecast of mine, and they move all day.")
    return "\n".join(lines)


def _show_reply(ctx: dict[str, Any]) -> str:
    rows = _outcomes(str(ctx.get("show_odds") or ""))
    if not rows:
        return (
            "Kalshi's server did not return prices for the halftime market just now. "
            "I can ask again in a minute."
        )
    ev = ctx.get("show_event") or {}
    lines = ["Kalshi's market on who performs at the halftime show, just now:\n"]
    lines += ["| Performer | The market's chance |", "|---|---|"]
    lines += [f"| {name} | {_pct(c)} |" for name, c, _ in rows[:6]]
    lines.append(
        f"\n**{rows[0][0]}** is the market's front-runner. Several people can perform, so "
        "these chances do not add up to 100."
    )
    if ev.get("event_url"):
        lines.append(f"\n[See the market on Kalshi]({ev['event_url']})")
    return "\n".join(lines)


TEAM = {
    "default_agent": "tobi",
    "agents": [
        {
            "name": "tobi",
            "emoji": "🎙️",
            "description": "Research and show notes for the podcast.",
            "title": "Show Research",
            "manager": None,
        },
    ],
    "files": {
        "tobi": {
            "AGENTS.md": "# tobi\n\nResearch for Loose Change, a podcast about money and the news.\n"
        },
    },
    "turns": {
        "tobi": [
            {
                "match": "halftime",
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "search_markets",
                        "args": SHOW_SEARCH,
                        "result": _search("show", SHOW_SEARCH),
                        "took": 0.3,
                    },
                    {
                        "tool": "get_event_odds",
                        "args": _odds_args("show", "KXPERFORMSUPERBOWL", 50),
                        "result": _odds("show_odds"),
                        "took": 0.3,
                    },
                    {"text": _show_reply, "wps": 26},
                ],
            },
            {
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "search_markets",
                        "args": FED_SEARCH,
                        "result": _search("fed", FED_SEARCH),
                        "took": 0.3,
                    },
                    {
                        "tool": "get_event_odds",
                        "args": _odds_args("fed", "KXFEDDECISION", 20),
                        "result": _odds("fed_odds"),
                        "took": 0.3,
                    },
                    {
                        "tool": "get_market_links",
                        "args": _links_args,
                        "result": _links,
                        "took": 0.3,
                    },
                    {"text": _fed_reply, "wps": 26},
                ],
            },
        ],
    },
}
