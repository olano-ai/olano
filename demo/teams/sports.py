"""The Corner Booth, a neighbourhood sports bar: the team in the sports video.

In the take the agent's MCP tab adds Sports Hub (``npx -y mcp-sports-hub@1.4.1``, an open-source,
keyless MCP server on npm) as a custom local (stdio) server, with ``SPORTS_HUB_PROVIDERS=espn`` so it
loads only its ESPN provider (ten tools over ESPN's public data). The dashboard starts it for real
and lists its tools.

In the chat, Dex's tool steps are real calls to that server over stdio with the official ``mcp``
Python SDK: the NFL scoreboard for today, then each game's summary (ESPN's matchup predictor and the
injury report). The server is started from the command, arguments and environment saved in Dex's
``agent.json5`` (what the dashboard wrote), one session for the whole turn. The reply is built from
what came back. "Favoured" means ESPN's matchup predictor; nothing here suggests placing a bet.

What this file adds, and nothing more: outbound HTTPS on this machine goes through a local proxy,
so the server is started with ``NODE_USE_ENV_PROXY=1`` (Node's own switch to honour HTTPS_PROXY)
when a proxy is set. Not shown or needed on a normal machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PACKAGE = "mcp-sports-hub@1.4.1"
DEFAULT = ("npx", ["-y", PACKAGE], {"SPORTS_HUB_PROVIDERS": "espn"})
EASTERN = ZoneInfo("America/New_York")

BOARD_ARGS = {
    "sport": "football",
    "league": "nfl",
    "fields": "uid,shortName,date,broadcast,shortDetail,state",
}


def _saved_server(agent: str) -> tuple[str, list[str], dict[str, str]]:
    """The command, arguments and environment the dashboard saved for the Sports Hub server."""
    home = os.environ.get("LIONCLAW_HOME", "")
    try:
        from olano.config.json5_fast import loads

        cfg = loads((Path(home) / "agents" / agent / "agent.json5").read_text(encoding="utf-8"))
        for srv in cfg.get("mcp_servers") or []:
            if "mcp-sports-hub" in json.dumps(srv):
                env = {str(k): str(v) for k, v in (srv.get("env") or {}).items()}
                return str(srv.get("command")), [str(a) for a in srv.get("args") or []], env
    except Exception:
        pass
    return DEFAULT


class _Session:
    """One stdio session with the server, held open across a turn's steps."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.stack: AsyncExitStack | None = None
        self.session: Any = None

    async def _open(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command, args, saved_env = _saved_server(self.agent)
        env = {**os.environ, **saved_env}
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            env["NODE_USE_ENV_PROXY"] = "1"
        params = StdioServerParameters(
            command=command, args=args, env=env, cwd=tempfile.mkdtemp(prefix="demo-sports-")
        )
        self.stack = AsyncExitStack()
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        if self.session is None:
            await self._open()
        res = await self.session.call_tool(tool, args)
        text = "\n".join(str(getattr(c, "text", "")) for c in res.content).strip()
        return ("Error: " + text) if res.isError else text

    def call(self, tool: str, args: dict[str, Any]) -> str:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._call(tool, args), self.loop)
            return fut.result(timeout=120)
        except Exception as exc:  # shown as the tool's result, as a real failed call would be
            return f"Error: {type(exc).__name__}: {exc}"


def _session(ctx: dict[str, Any]) -> _Session:
    if "hub" not in ctx:
        ctx["hub"] = _Session(ctx["agent"])
    return ctx["hub"]


def _json(text: str) -> Any:
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return {}
    return data


def _games(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    data = _json(str(ctx.get("board") or ""))
    out = []
    for ev in (data.get("events") if isinstance(data, dict) else None) or []:
        if not isinstance(ev, dict):
            continue
        m = re.search(r"~e:(\d+)", str(ev.get("uid") or ""))
        if not m:
            continue
        comp = (ev.get("competitions") or [{}])[0] or {}
        status = ev.get("status") or {}
        stype = status.get("type") or {}
        out.append(
            {
                "id": m.group(1),
                "name": str(ev.get("shortName") or ""),
                "date": str(ev.get("date") or ""),
                "tv": str(comp.get("broadcast") or ""),
                "state": str(stype.get("state") or ""),
                "detail": str(stype.get("shortDetail") or ""),
            }
        )
    return out


def _board(ctx: dict[str, Any]) -> str:
    ctx["board"] = _session(ctx).call("espn_get_scoreboard", ctx["args"])
    games = _games(ctx)
    # The scoreboard covers the whole NFL week: keep today's games (US Eastern), else the next day's.
    days = sorted({_day(g["date"]) for g in games if _day(g["date"])})
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    pick = today if today in days else next((d for d in days if d > today), days[0] if days else "")
    ctx["games"] = [g for g in games if _day(g["date"]) == pick]
    ctx["day"] = pick
    return ctx["board"]


def _day(iso: str) -> str:
    try:
        return (
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone(EASTERN)
            .strftime("%Y-%m-%d")
        )
    except ValueError:
        return ""


def _summary_args(i: int):
    def args(ctx: dict[str, Any]) -> dict[str, Any]:
        games = ctx.get("games") or []
        gid = games[i]["id"] if i < len(games) else ""
        return {
            "sport": "football",
            "league": "nfl",
            "event_id": gid,
            "fields": "predictor,injuries",
        }

    return args


def _summary(i: int):
    def run(ctx: dict[str, Any]) -> str:
        if not ctx["args"].get("event_id"):
            text = "Error: no game at this position on today's scoreboard."
        else:
            text = _session(ctx).call("espn_get_event_summary", ctx["args"])
        ctx[f"sum{i}"] = text
        return text

    return run


def _kickoff(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
    except ValueError:
        return ""
    return t.strftime("%-I:%M %p ET").replace(":00 ", " ")


def _game_block(game: dict[str, Any], summary: Any) -> str:
    teams = game["name"].split(" @ ")
    away, home = (teams + ["", ""])[:2] if len(teams) == 2 else ("", "")
    when = _kickoff(game["date"])
    head = f"**{game['name']}**"
    bits = [b for b in (when, game["tv"]) if b]
    if game["state"] in ("in", "post") and game["detail"]:
        bits = [game["detail"]] + ([game["tv"]] if game["tv"] else [])
    if bits:
        head += " · " + ", ".join(bits)
    lines = [head]
    pred = summary.get("predictor") if isinstance(summary, dict) else None
    if isinstance(pred, dict):
        try:
            h = float((pred.get("homeTeam") or {}).get("gameProjection"))
            a = float((pred.get("awayTeam") or {}).get("gameProjection"))
            fav, pct = (home, h) if h >= a else (away, a)
            lines.append(f"- Favoured: **{fav}**, {pct:.0f}% to win (ESPN's matchup predictor)")
        except (TypeError, ValueError):
            pass
    out: list[str] = []
    for team in (summary.get("injuries") if isinstance(summary, dict) else None) or []:
        abbr = str(((team or {}).get("team") or {}).get("abbreviation") or "")
        for inj in (team or {}).get("injuries") or []:
            status = str(inj.get("status") or "")
            if status not in ("Out", "Doubtful"):
                continue
            ath = inj.get("athlete") or {}
            name = str(ath.get("shortName") or ath.get("displayName") or "")
            pos = str((ath.get("position") or {}).get("abbreviation") or "")
            if name:
                out.append(f"{name} ({abbr} {pos})".replace(" )", ")"))
    if out:
        more = f", and {len(out) - 4} more" if len(out) > 4 else ""
        lines.append("- Out: " + ", ".join(out[:4]) + more)
    elif isinstance(summary, dict) and "injuries" in summary:
        lines.append("- Nobody listed as out")
    return "\n".join(lines)


def _reply(ctx: dict[str, Any]) -> str:
    games = ctx.get("games") or []
    if not games:
        return (
            "ESPN's scoreboard came back without games for today, so I have nothing to plan "
            "around yet. I can check again in a little while."
        )
    blocks = []
    for i, game in enumerate(games[:3]):
        blocks.append(_game_block(game, _json(str(ctx.get(f"sum{i}") or ""))))
    n = len(games)
    try:
        day = datetime.strptime(str(ctx.get("day")), "%Y-%m-%d").strftime("%A, %b %-d")
    except ValueError:
        day = "today"
    when = "today" if ctx.get("day") == datetime.now(EASTERN).strftime("%Y-%m-%d") else "next up"
    intro = f"{n} NFL game{'s' if n != 1 else ''} {when} ({day}), from ESPN:"
    return (
        intro
        + "\n\n"
        + "\n\n".join(blocks)
        + "\n\nKickoff times in US Eastern. The injury report can change up to kickoff."
    )


TEAM = {
    "default_agent": "dex",
    "agents": [
        {
            "name": "dex",
            "emoji": "📺",
            "description": "Plans the screens and the game-day specials.",
            "title": "Game Day",
            "manager": None,
        },
    ],
    "files": {
        "dex": {"AGENTS.md": "# dex\n\nGame-day planning at The Corner Booth, a sports bar.\n"},
    },
    "turns": {
        "dex": [
            {
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "espn_get_scoreboard",
                        "args": BOARD_ARGS,
                        "result": _board,
                        "took": 0.3,
                    },
                    *[
                        {
                            "tool": "espn_get_event_summary",
                            "args": _summary_args(i),
                            "result": _summary(i),
                            "took": 0.3,
                        }
                        for i in range(3)
                    ],
                    {"text": _reply, "wps": 26},
                ],
            },
        ],
    },
}
