"""Lamplight Runners, a small beginners' running club in Bangkok: the team in the runweather video.

The keyless weather MCP server ``@dangahagan/weather-mcp`` (Open-Meteo data, run locally with
``npx``, no account) is added to the agent as a custom stdio server during the take and connects for
real: the dashboard starts it and lists its tools. In the chat, Kip's two tool steps are real calls
to that server over stdio, made with the official ``mcp`` Python SDK from the command and arguments
the dashboard saved in Kip's ``agent.json5``: ``get_weather_summary`` for the current conditions and
air quality, then ``get_forecast`` hour by hour. The reply (a go or no-go and the best hour) is built
from what came back; nothing in it is written ahead of the take.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PACKAGE = "@dangahagan/weather-mcp@1.35.30"
CITY = "Bangkok, Thailand"
DEFAULT_COMMAND = ("npx", ["-y", PACKAGE])

SUMMARY_ARGS = {"city_name": CITY, "include": ["current", "air_quality"], "units": "metric"}
FORECAST_ARGS = {"city_name": CITY, "days": 2, "granularity": "hourly", "units": "metric"}


def _saved_command(agent: str) -> tuple[str, list[str]]:
    """The command and arguments the dashboard saved for the weather server."""
    home = os.environ.get("LIONCLAW_HOME", "")
    try:
        from olano.config.json5_fast import loads

        cfg = loads((Path(home) / "agents" / agent / "agent.json5").read_text(encoding="utf-8"))
        for srv in cfg.get("mcp_servers") or []:
            if "weather-mcp" in json.dumps(srv):
                cmd = srv.get("command")
                args = [str(a) for a in srv.get("args") or []]
                if cmd:
                    return str(cmd), args
    except Exception:
        pass
    return DEFAULT_COMMAND


class _Session:
    """One stdio session with the weather server, held open across a turn's steps."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.stack: AsyncExitStack | None = None
        self.session: Any = None

    async def _open(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command, args = _saved_command(self.agent)
        if command == "npx" and "-y" not in args:
            args = ["-y", *args]  # never stop to ask whether to install
        params = StdioServerParameters(
            command=command,
            args=args,
            env=dict(os.environ),
            cwd=tempfile.mkdtemp(prefix="demo-wx-"),
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
            return fut.result(timeout=150)
        except Exception as exc:  # shown as the tool's result, as a real failed call would be
            return f"Error: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        if self.stack is not None:
            fut = asyncio.run_coroutine_threadsafe(self.stack.aclose(), self.loop)
            try:
                fut.result(timeout=30)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


def _step(tool: str, key: str):
    def run(ctx: dict[str, Any]) -> str:
        if "wx" not in ctx:
            ctx["wx"] = _Session(ctx["agent"])
        ctx[key] = ctx["wx"].call(tool, ctx["args"])
        return ctx[key]

    return run


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _air(summary: str) -> dict[str, Any]:
    us = re.search(r"US AQI:\s*(\d+)\s*\(([^)]+)\)", summary)
    return {
        "us_aqi": int(us.group(1)) if us else None,
        "us_cat": us.group(2).strip() if us else "",
        "pm25": _num(r"PM2\.5[^:]*:\*\*\s*([\d.]+)", summary),
        "temp": _num(r"\*\*Temperature:\*\*\s*(-?[\d.]+)", summary),
        "feels": _num(r"\*\*Feels Like:\*\*\s*(-?[\d.]+)", summary),
    }


def _hours(forecast: str) -> tuple[list[dict[str, Any]], str]:
    tz = re.search(r"\*\*Timezone:\*\*\s*(\S+)", forecast)
    tzname = tz.group(1) if tz else "Asia/Bangkok"
    out = []
    for block in re.split(r"\n## ", "\n" + forecast)[1:]:
        head = block.split("\n", 1)[0].strip()
        try:
            when = datetime.strptime(head, "%m/%d/%Y, %H:%M")
        except ValueError:
            continue
        out.append(
            {
                "when": when,
                "temp": _num(r"\*\*Temperature:\*\*\s*(-?[\d.]+)", block),
                "feels": _num(r"feels like\s*(-?[\d.]+)", block),
                "rain": _num(r"\*\*Precipitation Chance:\*\*\s*([\d.]+)", block),
                "cond": (re.search(r"\*\*Conditions:\*\*\s*(.+)", block) or [None, ""])[1].strip(),
            }
        )
    return out, tzname


def _tonight(hours: list[dict[str, Any]], tzname: str) -> list[dict[str, Any]]:
    try:
        now = datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
    except Exception:
        now = datetime.utcnow() + timedelta(hours=7)
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if (end - start).total_seconds() < 2 * 3600:  # late already: the next few hours instead
        end = start + timedelta(hours=4)
    if start.hour < 17 and start.date() == now.date():
        start = start.replace(hour=18)
    return [h for h in hours if start <= h["when"] <= end and h["temp"] is not None]


def _verdict(aqi: int | None, cat: str) -> tuple[str, str]:
    if aqi is None:
        return (
            "No air reading",
            "the server did not return an air quality reading, so I cannot say.",
        )
    label = f"US AQI {aqi}, {cat.lower()}" if cat else f"US AQI {aqi}"
    if aqi <= 50:
        return "Go", f"the air is clean right now ({label})."
    if aqi <= 100:
        return "Go", f"the air is acceptable right now ({label})."
    if aqi <= 150:
        return (
            "Go easy",
            f"the air is not great right now ({label}): keep it short and easy, "
            "and skip it if you have asthma.",
        )
    return "Not tonight", f"the air is poor right now ({label}): an indoor run is the better call."


def _fmt(n: float | None) -> str:
    return "?" if n is None else f"{n:.0f}"


def _reply(ctx: dict[str, Any]) -> str:
    summary = str(ctx.get("summary") or "")
    forecast = str(ctx.get("forecast") or "")
    if "wx" in ctx:
        ctx["wx"].close()
    air = _air(summary) if not summary.startswith("Error") else {}
    hours, tzname = _hours(forecast) if not forecast.startswith("Error") else ([], "")
    tonight = _tonight(hours, tzname) if hours else []
    if not air and not tonight:
        return (
            "The weather server did not answer this time, so I have nothing to go on. "
            "I will not guess: ask me again in a minute."
        )
    word, why = _verdict(air.get("us_aqi"), air.get("us_cat", ""))
    lines = [f"**{word}:** {why}"]
    if air.get("pm25") is not None:
        lines[-1] += f" PM2.5 is {_fmt(air['pm25'])} µg/m³."
    dry = [h for h in tonight if (h["rain"] or 0) <= 30] or tonight
    if dry:
        best = min(dry, key=lambda h: (h["feels"] if h["feels"] is not None else h["temp"]))
        lead = (
            f"**If you run anyway: {best['when']:%H:%M}.** It is"
            if word == "Not tonight"
            else f"**Best hour: {best['when']:%H:%M}.** It is"
        )
        lines.append(
            f"\n{lead} the coolest dry hour tonight: "
            f"{_fmt(best['temp'])}°C, feels like {_fmt(best['feels'])}°C, "
            f"{_fmt(best['rain'])}% chance of rain."
        )
        lines.append("\n| Hour | Temp | Feels like | Rain |\n|---|---|---|---|")
        for h in tonight[:6]:
            mark = " ✅" if h is best else ""
            lines.append(
                f"| {h['when']:%H:%M}{mark} | {_fmt(h['temp'])}°C | {_fmt(h['feels'])}°C "
                f"| {_fmt(h['rain'])}% |"
            )
    lines.append(
        "\nAir quality is the reading now; the hours are Open-Meteo's forecast, "
        "through Weather MCP."
    )
    return "\n".join(lines)


TEAM = {
    "default_agent": "kip",
    "agents": [
        {
            "name": "kip",
            "emoji": "🏃",
            "description": "Plans the club's runs and checks the weather before them.",
            "title": "Run Planner",
            "manager": None,
        },
    ],
    "files": {
        "kip": {
            "AGENTS.md": (
                "# kip\n\nRun planner for Lamplight Runners, a beginners' running club in Bangkok.\n"
            ),
        },
    },
    "turns": {
        "kip": [
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "get_weather_summary",
                        "args": SUMMARY_ARGS,
                        "result": _step("get_weather_summary", "summary"),
                        "took": 0.4,
                    },
                    {"wait": 0.3},
                    {
                        "tool": "get_forecast",
                        "args": FORECAST_ARGS,
                        "result": _step("get_forecast", "forecast"),
                        "took": 0.4,
                    },
                    {"text": _reply, "wps": 30},
                ],
            },
        ],
    },
}
