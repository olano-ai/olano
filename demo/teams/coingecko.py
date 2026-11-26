"""Tidepool Notes, a two-person newsletter about digital payments: the team in the coingecko video.

In the take the agent's MCP tab connects CoinGecko's keyless public MCP server
(``https://mcp.api.coingecko.com/mcp``) as a custom server. The scripted turn's ``search_docs`` and
``execute`` steps are real calls to that server with the official ``mcp`` Python SDK: the first looks
up how to ask for prices, the second runs the TypeScript the server's ``execute`` tool expects (an
``async function run(client)`` against its SDK client). The reply is built from what came back. The
follow-up puts the snapshot on Olano's scheduler with the product's own ``scheduler_create`` tool.
Market facts only: nothing here suggests buying or selling anything.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

URL = "https://mcp.api.coingecko.com/mcp"
COINS = {"bitcoin": "Bitcoin", "ethereum": "Ethereum", "solana": "Solana"}

DOCS_ARGS = {
    "query": "coin prices with 24 hour change, and trending coins",
    "language": "typescript",
}
CODE = """async function run(client) {
  const price = await client.simple.price.get({
    ids: 'bitcoin,ethereum,solana',
    vs_currencies: 'usd',
    include_24hr_change: true,
  });
  const trending = await client.search.trending.get();
  return {
    price,
    trending: trending.coins.slice(0, 5).map((c) => ({ name: c.item.name, symbol: c.item.symbol })),
  };
}"""
EXEC_ARGS = {"code": CODE, "intent": "Market snapshot: three coins and what is trending"}

BRIEF_PROMPT = (
    "Morning market brief for the newsletter: the price in USD and 24-hour change of Bitcoin, "
    "Ethereum and Solana, and the top five trending coins on CoinGecko. Facts only, no advice."
)
SCHEDULE_ARGS = {
    "name": "Morning market brief",
    "prompt": BRIEF_PROMPT,
    "schedule": "0 8 * * 1-5",
    "timezone": "Asia/Singapore",
}

_RESULTS: dict[str, Any] = {}


async def _call(tool: str, args: dict[str, Any]) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    text = "\n".join(str(getattr(c, "text", "")) for c in res.content)
    return ("Error: " + text) if res.isError else text


def _mcp(tool: str, args: dict[str, Any], key: str):
    """A scripted tool step whose result is the server's own answer."""

    def run(ctx: dict[str, Any]) -> str:
        try:
            text = asyncio.run(_call(tool, args))
        except Exception as exc:
            text = f"Error: {type(exc).__name__}: {exc}"
        _RESULTS[key] = text
        return text

    return run


def _schedule(ctx: dict[str, Any]) -> str:
    """The product's own scheduler_create tool, run for the agent."""
    from olano.tools import scheduler_tools
    from olano.tools._credentials import agent_runtime_context

    with agent_runtime_context(ctx["agent"], None):
        text = str(scheduler_tools.scheduler_create.invoke(SCHEDULE_ARGS))
    _RESULTS["schedule"] = text
    return text


def _snapshot() -> dict[str, Any]:
    raw = str(_RESULTS.get("exec") or "")
    try:
        data = json.loads(raw)
    except ValueError:
        start = raw.find("{")
        try:
            data = json.loads(raw[start:]) if start >= 0 else {}
        except ValueError:
            data = {}
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    return data if isinstance(data, dict) else {}


def _money(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"${x:,.0f}" if x >= 1000 else f"${x:,.2f}"


def _change(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    arrow = "▲" if x > 0 else "▼" if x < 0 else ""
    return f"{arrow} {x:+.1f}%".strip()


def _snapshot_reply(ctx: dict[str, Any]) -> str:
    data = _snapshot()
    prices = data.get("price") if isinstance(data.get("price"), dict) else {}
    rows = ["| Coin | Price (USD) | Last 24 h |", "|---|---|---|"]
    for cid, name in COINS.items():
        p = prices.get(cid)
        if isinstance(p, dict) and p.get("usd") is not None:
            rows.append(f"| {name} | {_money(p.get('usd'))} | {_change(p.get('usd_24h_change'))} |")
    trending = [t for t in data.get("trending") or [] if isinstance(t, dict) and t.get("name")]
    if len(rows) == 2 and not trending:
        return (
            "CoinGecko's server did not return prices this time, so I have no snapshot to show. "
            "I can try again in a minute."
        )
    parts = ["Here is the snapshot from CoinGecko, just now:\n"]
    if len(rows) > 2:
        parts.append("\n".join(rows))
    if trending:
        names = ", ".join(
            f"{t['name']} ({str(t.get('symbol') or '').upper()})" if t.get("symbol") else t["name"]
            for t in trending
        )
        parts.append(f"\n**Trending on CoinGecko in the last 24 hours:** {names}.")
    parts.append("\nFigures as CoinGecko reported them; they move by the minute.")
    return "\n".join(parts)


def _schedule_reply(ctx: dict[str, Any]) -> str:
    text = str(_RESULTS.get("schedule") or "")
    if not text.startswith("Task created"):
        return f"The scheduler refused the task: {text.strip() or 'no answer'}."
    days = re.search(r"Runs on: ([^\n]+)", text)
    day_text = days.group(1).strip().rstrip(".") if days else "Monday to Friday"
    return (
        "Done. **Morning market brief** is on the scheduler: the same snapshot at 08:00 Singapore time, "
        f"on {day_text}. Each run starts its own conversation, so you will find the brief "
        "there when you get in."
    )


TEAM = {
    "default_agent": "remy",
    "agents": [
        {
            "name": "remy",
            "emoji": "📰",
            "description": "Research and briefs for the newsletter.",
            "title": "Research Desk",
            "manager": None,
        },
    ],
    "files": {
        "remy": {
            "AGENTS.md": "# remy\n\nResearch desk at Tidepool Notes, a newsletter on digital payments.\n"
        },
    },
    "tasks": [
        {
            "name": "Issue draft",
            "prompt": "Draft Friday's issue from this week's notes.",
            "schedule": "0 14 * * 4",
            "agent_name": "remy",
            "timezone": "Asia/Singapore",
        },
    ],
    "turns": {
        "remy": [
            {
                "match": "schedule",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "scheduler_create",
                        "args": SCHEDULE_ARGS,
                        "result": _schedule,
                        "took": 0.6,
                    },
                    {"text": _schedule_reply, "wps": 30},
                ],
            },
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "search_docs",
                        "args": DOCS_ARGS,
                        "result": _mcp("search_docs", DOCS_ARGS, "docs"),
                        "took": 0.4,
                    },
                    {"wait": 0.3},
                    {
                        "tool": "execute",
                        "args": EXEC_ARGS,
                        "result": _mcp("execute", EXEC_ARGS, "exec"),
                        "took": 0.4,
                    },
                    {"text": _snapshot_reply, "wps": 30},
                ],
            },
        ],
    },
}
