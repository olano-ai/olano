"""Slipstream, a sim-racing café that runs race-weekend watch parties: the team in the f1 video.

In the take the agent's MCP tab adds Sports Hub (``npx -y mcp-sports-hub@1.4.1``, an open-source,
keyless MCP server on npm) as a custom local (stdio) server with ``SPORTS_HUB_PROVIDERS=f1``, its
Formula 1 preset: Jolpica (the Ergast replacement: results, standings) and OpenF1 (sessions, laps).
The dashboard starts it for real and lists its tools.

In the chat, Lotte's tool steps are real calls to that server over stdio with the official ``mcp``
Python SDK, started from the command, arguments and environment saved in Lotte's ``agent.json5``.
The chart is made the way an agent makes one: the recipe written from the lap times OpenF1 returned,
the product's own ``charts`` skill script turning it into a page, and the product's own
``render_html_image`` tool turning that into the PNG shown in the reply.

What this file adds, and nothing more: outbound HTTPS on this machine goes through a local proxy,
so the server is started with ``NODE_USE_ENV_PROXY=1`` (Node's own switch to honour HTTPS_PROXY)
when a proxy is set. Not shown or needed on a normal machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHART_PY = ROOT / "olano" / "skills" / "built_in" / "charts" / "scripts" / "chart.py"
PACKAGE = "mcp-sports-hub@1.4.1"
DEFAULT = ("npx", ["-y", PACKAGE], {"SPORTS_HUB_PROVIDERS": "f1"})
SPEC = "charts/race-laps.json"
PAGE = "charts/race-laps.html"
PNG = "charts/race-laps.png"
SLOW_LAP = 110.0  # seconds: laps slower than this (safety car, pit stops) are left off the chart

STANDINGS_ARGS = {"season": "current", "limit": 5}
RESULTS_ARGS = {"season": "current", "round": "last"}

# What the first turn learned (the last race), for the follow-up.
_LAST: dict[str, Any] = {}


def _saved_server(agent: str) -> tuple[str, list[str], dict[str, str]]:
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
            command=command, args=args, env=env, cwd=tempfile.mkdtemp(prefix="demo-f1-")
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


def _hub(tool: str, key: str):
    def run(ctx: dict[str, Any]) -> str:
        if "hub" not in ctx:
            ctx["hub"] = _Session(ctx["agent"])
        ctx[key] = ctx["hub"].call(tool, ctx["args"])
        return ctx[key]

    return run


def _json(text: str) -> Any:
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
    except ValueError:
        return {}
    return data


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- turn 1: standings and the last race ---


def _race(ctx: dict[str, Any]) -> dict[str, Any]:
    data = _json(str(ctx.get("results") or ""))
    races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
    return races[0] if races and isinstance(races[0], dict) else {}


def _summary_reply(ctx: dict[str, Any]) -> str:
    data = _json(str(ctx.get("standings") or ""))
    table = ((data or {}).get("MRData") or {}).get("StandingsTable") or {}
    lists = table.get("StandingsLists") or [{}]
    rows = lists[0].get("DriverStandings") or []
    race = _race(ctx)
    parts = []
    if rows:
        rnd = table.get("round") or lists[0].get("round")
        parts.append(
            f"The drivers' championship after round {rnd}:\n"
            if rnd
            else "The drivers' championship:\n"
        )
        parts += ["| | Driver | Team | Points | Wins |", "|---|---|---|---|---|"]
        for r in rows[:5]:
            d, cons = r.get("Driver") or {}, (r.get("Constructors") or [{}])[0]
            name = f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
            parts.append(
                f"| {r.get('position')} | {name} | {cons.get('name', '')} | "
                f"{r.get('points')} | {r.get('wins')} |"
            )
        if len(rows) > 1:
            gap = (_num(rows[0].get("points")) or 0) - (_num(rows[1].get("points")) or 0)
            lead = (rows[0].get("Driver") or {}).get("familyName", "")
            parts.append(f"\n**{lead}** leads by **{gap:.0f} points**.")
    results = race.get("Results") or []
    if results:
        _LAST.clear()
        _LAST["race"] = race
        podium = []
        for r in results[:3]:
            d = r.get("Driver") or {}
            t = (r.get("Time") or {}).get("time") or r.get("status") or ""
            grid = r.get("grid")
            extra = (
                f", from {grid}th on the grid"
                if grid and str(grid).isdigit() and int(grid) > 9
                else ""
            )
            podium.append(
                f"P{r.get('position')} {d.get('givenName', '')} {d.get('familyName', '')} ({t}{extra})"
            )
        name = race.get("raceName") or "The last race"
        when = _date(race.get("date") or "")
        parts.append(f"\n**{name}**" + (f", {when}" if when else "") + ":\n")
        parts += [f"- {p}" for p in podium]
    if not parts:
        return "Jolpica's data did not come back just now, so I have no standings to show yet."
    parts.append("\nFrom Jolpica's F1 data, through the Sports Hub server.")
    return "\n".join(parts)


def _date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %B")
    except ValueError:
        return iso


def _lap(t: float) -> str:
    m, sec = divmod(t, 60)
    return f"{int(m)}:{sec:06.3f}" if m else f"{sec:.3f} s"


# --- turn 2: two drivers, lap by lap, as a chart ---


def _drivers(ctx: dict[str, Any]) -> list[tuple[str, int]]:
    """The two drivers the question names (by surname), from the last race's results."""
    results = (_LAST.get("race") or {}).get("Results") or []
    low = str(ctx.get("message") or "").lower()
    named = []
    for r in results:
        d = r.get("Driver") or {}
        fam = str(d.get("familyName") or "")
        num = int(_num(r.get("number")) or 0)
        if fam and num and fam.lower() in low:
            named.append((fam, num, low.find(fam.lower())))
    named.sort(key=lambda x: x[2])
    pairs = [(f, n) for f, n, _ in named][:2]
    if len(pairs) < 2:
        top = [
            (str((r.get("Driver") or {}).get("familyName") or ""), int(_num(r.get("number")) or 0))
            for r in results[:2]
        ]
        pairs = top if len(top) == 2 else [("Verstappen", 3), ("Norris", 1)]
    return pairs


def _sessions_args(ctx: dict[str, Any]) -> dict[str, Any]:
    race = _LAST.get("race") or {}
    year = int(_num(race.get("season")) or datetime.now(timezone.utc).year)
    return {"year": year, "session_name": "Race"}


def _session_key(ctx: dict[str, Any]) -> tuple[int, str]:
    sessions = _json(str(ctx.get("sessions") or ""))
    now = datetime.now(timezone.utc).isoformat()
    done = [
        s
        for s in (sessions if isinstance(sessions, list) else [])
        if isinstance(s, dict) and not s.get("is_cancelled") and str(s.get("date_end") or "") < now
    ]
    done.sort(key=lambda s: str(s.get("date_start") or ""))
    if not done:
        return 0, ""
    last = done[-1]
    return int(last.get("session_key") or 0), str(last.get("location") or "")


def _laps_args(i: int):
    def args(ctx: dict[str, Any]) -> dict[str, Any]:
        key, where = _session_key(ctx)
        ctx["where"] = where
        ctx["pair"] = _drivers(ctx)
        return {
            "session_key": key,
            "driver_number": ctx["pair"][i][1],
            "fields": "lap_number,lap_duration",
        }

    return args


def _laps(ctx: dict[str, Any], i: int) -> dict[int, float]:
    out = {}
    for row in _json(str(ctx.get(f"laps{i}") or "")) or []:
        if isinstance(row, dict):
            n, t = _num(row.get("lap_number")), _num(row.get("lap_duration"))
            if n is not None and t is not None:
                out[int(n)] = t
    return out


def _spec(ctx: dict[str, Any]) -> dict[str, Any]:
    (a_name, _), (b_name, _) = ctx.get("pair") or [("Verstappen", 3), ("Norris", 1)]
    a, b = _laps(ctx, 0), _laps(ctx, 1)
    laps = sorted(set(a) | set(b))
    slow = [n for n in laps if max(a.get(n, 0), b.get(n, 0)) > SLOW_LAP]

    def vals(d: dict[int, float]) -> list[float | None]:
        return [None if (n in slow or n not in d) else round(d[n], 3) for n in laps]

    kept = [d[n] for d in (a, b) for n in laps if n in d and n not in slow]
    lo = float(int(min(kept)) - 1) if kept else None
    hi = float(int(max(kept)) + 1) if kept else None
    both = [n for n in laps if n in a and n in b and n not in slow]
    last10 = both[-10:]
    faster_b = sum(1 for n in last10 if b[n] < a[n])
    race = _LAST.get("race") or {}
    name = race.get("raceName") or (
        f"{ctx.get('where')} race" if ctx.get("where") else "The last race"
    )
    if last10:
        title = f"{b_name} was quicker on {faster_b} of the last {len(last10)} laps"
        if faster_b * 2 < len(last10):
            title = (
                f"{a_name} was quicker on {len(last10) - faster_b} of the last {len(last10)} laps"
            )
    else:
        title = f"{a_name} and {b_name}, lap by lap"
    note = ""
    if slow:
        span = f"laps {slow[0]} to {slow[-1]}" if len(slow) > 1 else f"lap {slow[0]}"
        note = f"Laps over {SLOW_LAP:.0f} s ({span}) left out"
    spec: dict[str, Any] = {
        "type": "line",
        "kicker": f"{name}, lap times",
        "title": title,
        "subtitle": "Lap time in seconds, lower is faster",
        "categories": [str(n) for n in laps],
        "series": [
            {"name": a_name, "values": vals(a)},
            {"name": b_name, "values": vals(b)},
        ],
        "value_axis": {"label": "Seconds", "min": lo, "max": hi},
        "value_format": {"kind": "number", "decimals": 1, "compact": False},
        "category_axis": {"label": "Lap"},
        "labels": "none",
        "notes": {
            "source": "OpenF1, via the Sports Hub MCP server",
            "as_of": race.get("date") or "",
        },
    }
    if note:
        spec["notes"]["note"] = note
    return spec


def _write_spec(ctx: dict[str, Any]) -> str:
    ws = Path(_workspace(ctx))
    path = ws / SPEC
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx["spec"] = _spec(ctx)
    path.write_text(json.dumps(ctx["spec"], indent=1), encoding="utf-8")
    return f"Updated file {SPEC}"


def _workspace(ctx: dict[str, Any]) -> str:
    return str(Path(os.environ.get("LIONCLAW_HOME", "")) / "agents" / ctx["agent"] / "workspace")


def _run_chart(ctx: dict[str, Any]) -> str:
    """The charts skill's own script, as the agent's execute step runs it."""
    cmd = [
        sys.executable,
        str(CHART_PY),
        "render",
        SPEC,
        "--theme",
        "midnight",
        "--size",
        "1200x760",
    ]
    cmd += ["--out", PAGE]
    try:
        p = subprocess.run(cmd, cwd=_workspace(ctx), capture_output=True, text=True, timeout=60)
        return (p.stdout + p.stderr).strip() or f"exit {p.returncode}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def _render(ctx: dict[str, Any]) -> str:
    """The product's own render_html_image tool, run for the agent."""
    from olano.tools._credentials import agent_runtime_context
    from olano.tools.render_tools import render_html_image

    with agent_runtime_context(ctx["agent"], None):
        os.environ.setdefault("LIONCLAW_WORKSPACE", _workspace(ctx))
        ctx["rendered"] = str(render_html_image.invoke(ctx["args"]))
    return ctx["rendered"]


RENDER_ARGS = {
    "html": PAGE,
    "width": 1200,
    "height": 760,
    "save_path": PNG,
    "wait_for": ".render-done",
}
CHART_ARGS = {
    "command": f"python3 skills/charts/scripts/chart.py render {SPEC} --theme midnight "
    f"--size 1200x760 --out {PAGE}"
}


def _chart_reply(ctx: dict[str, Any]) -> str:
    spec = ctx.get("spec") or {}
    if "Saved:" not in str(ctx.get("rendered") or "") or not spec.get("categories"):
        return (
            "OpenF1 did not return lap times for both drivers just now, so there is no chart yet. "
            "I can try again in a minute."
        )
    (a_name, _), (b_name, _) = ctx.get("pair")
    a, b = _laps(ctx, 0), _laps(ctx, 1)
    best_a, best_b = min(a.values()), min(b.values())
    margin = ""
    for r in (_LAST.get("race") or {}).get("Results") or []:
        if (r.get("Driver") or {}).get("familyName") in (a_name, b_name) and r.get(
            "position"
        ) != "1":
            t = (r.get("Time") or {}).get("time")
            if t and t.startswith("+"):
                margin = f" The gap at the flag: **{t[1:]} s**."
            break
    text = (
        f"**{spec.get('title')}.** Fastest laps: {a_name} {_lap(best_a)}, {b_name} {_lap(best_b)}."
        f"{margin}\n\n"
    )
    if spec.get("notes", {}).get("note"):
        text += spec["notes"]["note"] + ", so the race pace is readable.\n\n"
    text += f"Chart: {PNG}"
    return text


TEAM = {
    "default_agent": "lotte",
    "agents": [
        {
            "name": "lotte",
            "emoji": "🏁",
            "description": "Race-weekend notes for the watch parties.",
            "title": "Race Desk",
            "manager": None,
        },
    ],
    "files": {
        "lotte": {"AGENTS.md": "# lotte\n\nRace desk at Slipstream, a sim-racing café.\n"},
    },
    "turns": {
        "lotte": [
            {
                "match": "lap",
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "openf1_get_sessions",
                        "args": _sessions_args,
                        "result": _hub("openf1_get_sessions", "sessions"),
                        "took": 0.3,
                    },
                    *[
                        {
                            "tool": "openf1_get_laps",
                            "args": _laps_args(i),
                            "result": _hub("openf1_get_laps", f"laps{i}"),
                            "took": 0.3,
                        }
                        for i in range(2)
                    ],
                    {
                        "tool": "write_file",
                        "args": {"file_path": SPEC},
                        "result": _write_spec,
                        "took": 0.3,
                    },
                    {"tool": "execute", "args": CHART_ARGS, "result": _run_chart, "took": 0.3},
                    {
                        "tool": "render_html_image",
                        "args": RENDER_ARGS,
                        "result": _render,
                        "took": 0.3,
                    },
                    {"text": _chart_reply, "wps": 22},
                ],
            },
            {
                "steps": [
                    {"wait": 0.3},
                    {
                        "tool": "f1_get_driver_standings",
                        "args": STANDINGS_ARGS,
                        "result": _hub("f1_get_driver_standings", "standings"),
                        "took": 0.3,
                    },
                    {
                        "tool": "f1_get_race_results",
                        "args": RESULTS_ARGS,
                        "result": _hub("f1_get_race_results", "results"),
                        "took": 0.3,
                    },
                    {"text": _summary_reply, "wps": 26},
                ],
            },
        ],
    },
}
