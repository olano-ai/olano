"""Second Look, a two-person newsletter about what the crowd expects: the team in the markets video.

In the take the agent's MCP tab adds pdata's public MCP server (``https://api.pdata.world/mcp``),
which aggregates prediction markets (Polymarket, Kalshi and others), as a custom server with no
account. The chat's tool steps are real calls to that server with the official ``mcp`` Python SDK:
``compare_pairs`` (the same question priced on Polymarket and on Kalshi, largest gap first) and
``top_movers`` (the biggest moves of the last 24 hours). The reply is built from what came back.
Market prices only: nothing here suggests placing a trade.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

URL = "https://api.pdata.world/mcp"

PAIRS_ARGS = {"left": "polymarket", "right": "kalshi", "limit": 5}
MOVERS_ARGS = {"limit": 5}
VENUES = {
    "polymarket": "Polymarket",
    "kalshi": "Kalshi",
    "manifold": "Manifold",
    "predict": "Predict",
    "limitless": "Limitless",
    "myriad": "Myriad",
    "opinion": "Opinion",
    "gemini": "Gemini",
}


async def _call(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    text = "\n".join(str(getattr(c, "text", "")) for c in res.content)
    return bool(res.isError), text


def _real(tool: str, key: str):
    def run(ctx: dict[str, Any]) -> str:
        text = ""
        for attempt in range(3):
            try:
                err, text = asyncio.run(_call(tool, ctx["args"]))
            except Exception as exc:
                err, text = True, f"{type(exc).__name__}: {exc}"
            if not err:
                break
            time.sleep(2.0 + 2.0 * attempt)
        else:
            text = "Error: " + text
        ctx[key] = text
        return text

    return run


def _json(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(p: float | None) -> str:
    if p is None:
        return "?"
    x = p * 100
    return "under 1%" if x < 1 else f"{x:.0f}%"


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _cell(text: str) -> str:
    return text.replace("|", "/")


def _pairs_reply(ctx: dict[str, Any]) -> str:
    pairs = [
        p for p in _json(str(ctx.get("pairs") or "")).get("pairs") or [] if isinstance(p, dict)
    ]
    rows = []
    for p in pairs:
        left, right = p.get("left") or {}, p.get("right") or {}
        a, b = _num(left.get("probability")), _num(right.get("probability"))
        if a is None or b is None:
            continue
        q = _short(p.get("question") or left.get("title") or "", 60)
        if p.get("outcome"):
            q += f" ({p['outcome']})"
        rows.append((q, a, b, abs(round(a * 100) - round(b * 100))))
    if not rows:
        return (
            "pdata's server returned no matched pairs with prices on both sides just now, so "
            "there is nothing to compare yet. I can ask again shortly."
        )
    lines = ["The same questions, priced on both markets, biggest gap first:\n"]
    lines += ["| Question | Polymarket | Kalshi | Gap |", "|---|---|---|---|"]
    for q, a, b, gap in rows:
        lines.append(f"| {_cell(q)} | {_pct(a)} | {_pct(b)} | {gap:.0f} pts |")
    q, a, b, gap = rows[0]
    hi, lo = ("Polymarket", "Kalshi") if a > b else ("Kalshi", "Polymarket")
    lines.append(
        f"\nThe widest gap right now: **{q}**. {hi} has it at {_pct(max(a, b))}, "
        f"{lo} at {_pct(min(a, b))}."
    )
    lines.append(
        "\nPairs are matched automatically by pdata.world; prices from Polymarket and Kalshi, "
        "and they move all day."
    )
    return "\n".join(lines)


def _movers_reply(ctx: dict[str, Any]) -> str:
    items = [
        i for i in _json(str(ctx.get("movers") or "")).get("items") or [] if isinstance(i, dict)
    ]
    rows = []
    for it in items:
        p, d = _num(it.get("probability")), _num(it.get("prob_24h_change"))
        if p is None or d is None:
            continue
        venue = VENUES.get(str(it.get("source")), str(it.get("source") or "").title())
        rows.append((_short(it.get("question") or "", 60), venue, p, d))
    if not rows:
        return "pdata's server returned no movers just now. I can ask again shortly."
    lines = ["The biggest moves of the last 24 hours:\n"]
    lines += ["| Question | Market | Now | 24 h change |", "|---|---|---|---|"]
    for q, venue, p, d in rows:
        arrow = "▲" if d > 0 else "▼" if d < 0 else ""
        lines.append(f"| {_cell(q)} | {venue} | {_pct(p)} | {arrow} {d * 100:+.0f} pts |")
    q, venue, p, d = rows[0]
    way = "up" if d > 0 else "down"
    lines.append(f"\nThe biggest swing: **{q}**, {way} {abs(d) * 100:.0f} points on {venue}.")
    return "\n".join(lines)


TEAM = {
    "default_agent": "ozzie",
    "agents": [
        {
            "name": "ozzie",
            "emoji": "🔭",
            "description": "Finds the week's stories for the newsletter.",
            "title": "Story Desk",
            "manager": None,
        },
    ],
    "files": {
        "ozzie": {
            "AGENTS.md": "# ozzie\n\nStory desk at Second Look, a newsletter about forecasts.\n"
        },
    },
    "turns": {
        "ozzie": [
            {
                "match": "moved",
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "top_movers",
                        "args": MOVERS_ARGS,
                        "result": _real("top_movers", "movers"),
                        "took": 0.3,
                    },
                    {"text": _movers_reply, "wps": 26},
                ],
            },
            {
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "compare_pairs",
                        "args": PAIRS_ARGS,
                        "result": _real("compare_pairs", "pairs"),
                        "took": 0.4,
                    },
                    {"text": _pairs_reply, "wps": 26},
                ],
            },
        ],
    },
}
