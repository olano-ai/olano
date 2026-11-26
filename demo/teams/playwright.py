"""Halyard Software, a small team that maintains Python web services: the team in the Playwright video.

Microsoft's Playwright MCP server (``npx @playwright/mcp@latest``, a local stdio server, no account)
is added from the MCP directory during the take, with ``--headless`` added to its arguments, and
connects for real: the dashboard starts it with ``npx`` and lists its tools. This is the Playwright
MCP server driving its own browser, not Olano's built-in browser tools.

In the chat, Cass's tool steps are real calls to that server over stdio, made with the official
``mcp`` Python SDK. The server is started from the command and arguments saved in Cass's
``agent.json5`` (what the dashboard wrote), and one session is kept for the whole turn, so the
click lands on the page the navigation opened. The reply is built from the page snapshot the
server returned.

What this file adds, and nothing more:

- **This machine's network.** Outbound HTTPS here goes through a local proxy, so the server is
  given Playwright MCP's own proxy settings as environment variables
  (``PLAYWRIGHT_MCP_PROXY_SERVER``, ``PLAYWRIGHT_MCP_IGNORE_HTTPS_ERRORS`` for the proxy's own
  certificate), plus ``PLAYWRIGHT_MCP_ISOLATED`` so every take starts from a clean profile and
  ``PLAYWRIGHT_MCP_NO_SANDBOX`` because the take runs as root. None of it is shown or needed on a
  normal machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

PACKAGE = "httpx"
PAGE = f"https://pypi.org/project/{PACKAGE}/"
DEFAULT_COMMAND = ("npx", ["@playwright/mcp@latest", "--headless"])


def _saved_command(agent: str) -> tuple[str, list[str]]:
    """The command and arguments the dashboard saved for the Playwright server."""
    home = os.environ.get("LIONCLAW_HOME", "")
    try:
        from olano.config.json5_fast import loads

        cfg = loads((Path(home) / "agents" / agent / "agent.json5").read_text(encoding="utf-8"))
        for srv in cfg.get("mcp_servers") or []:
            if "playwright" in json.dumps(srv):
                cmd = srv.get("command") or srv.get("url")
                args = [str(a) for a in srv.get("args") or []]
                if cmd:
                    return str(cmd), args
    except Exception:
        pass
    return DEFAULT_COMMAND


class _Session:
    """One stdio session with the Playwright MCP server, held open across a turn's steps."""

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
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        env = {
            **os.environ,
            "PLAYWRIGHT_MCP_ISOLATED": "true",
            "PLAYWRIGHT_MCP_NO_SANDBOX": "true",
        }
        if proxy:
            env["PLAYWRIGHT_MCP_PROXY_SERVER"] = proxy
            env["PLAYWRIGHT_MCP_IGNORE_HTTPS_ERRORS"] = "true"
        params = StdioServerParameters(
            command=command, args=args, env=env, cwd=tempfile.mkdtemp(prefix="demo-pw-")
        )
        self.stack = AsyncExitStack()
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        if self.session is None:
            await self._open()
        res = await self.session.call_tool(tool, args)
        return "\n".join(str(getattr(c, "text", "")) for c in res.content).strip()

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


def _session(ctx: dict[str, Any]) -> _Session:
    if "pw" not in ctx:
        ctx["pw"] = _Session(ctx["agent"])
    return ctx["pw"]


def _step(tool: str, key: str):
    def run(ctx: dict[str, Any]) -> str:
        ctx[key] = _session(ctx).call(tool, ctx["args"])
        return ctx[key]

    return run


def _ref(snapshot: str, pattern: str) -> str:
    for line in snapshot.splitlines():
        if re.search(pattern, line, flags=re.I):
            m = re.search(r"\[ref=(e\d+)\]", line)
            if m:
                return m.group(1)
    return ""


def _click_args(ctx: dict[str, Any]) -> dict[str, Any]:
    ref = _ref(ctx.get("snapshot", ""), r'tab "Release history')
    return {"element": "Release history tab", "target": ref or "e54"}


def _releases(snapshot: str) -> list[dict[str, Any]]:
    """The release history panel of a PyPI page snapshot: version, date, pre-release or not."""
    panel = snapshot.split('tabpanel "Release history', 1)
    if len(panel) < 2:
        return []
    out: list[dict[str, Any]] = []
    pre, version = False, ""
    for line in panel[1].splitlines():
        if 'link "Pre-release"' in line:
            pre = True
        elif m := re.search(r'link "(\d[^"]*)"', line):
            version = m.group(1)
        elif (m := re.search(r"- time \[ref=e\d+\]: (.+)$", line)) and version:
            out.append({"version": version, "date": m.group(1).strip(), "pre": pre})
            pre, version = False, ""
        elif "tabpanel " in line:
            break
    return out


def _reply(ctx: dict[str, Any]) -> str:
    rel = _releases(ctx.get("history", "") or "")
    if "pw" in ctx:
        ctx["pw"].close()
    if not rel:
        return (
            "I opened the PyPI page in the Playwright browser, but I could not read the release "
            "history from it this time, so I will not guess a version."
        )
    stable = next((r for r in rel if not r["pre"]), None)
    newer = [r for r in rel[: rel.index(stable)]] if stable else rel
    lines = []
    if stable:
        lines.append(
            f"The latest stable release of **{PACKAGE}** on PyPI is **{stable['version']}**, "
            f"published **{stable['date']}**."
        )
    if newer:
        lines.append(
            f"\nThere {'is a newer pre-release' if len(newer) == 1 else 'are newer pre-releases'}"
            ", from the Release history tab:\n"
        )
        lines += [f"- {r['version']}: {r['date']}" for r in newer[:3]]
        if len(newer) > 3:
            lines.append(f"- and {len(newer) - 3} more")
    lines.append(f"\n{PAGE}#history")
    return "\n".join(lines)


TEAM = {
    "default_agent": "cass",
    "agents": [
        {
            "name": "cass",
            "emoji": "🧪",
            "description": "Keeps dependencies current and checks releases before upgrades.",
            "title": "Release Engineer",
            "manager": None,
            "reports": ["teo"],
        },
        {
            "name": "teo",
            "emoji": "🖥️",
            "description": "Builds and runs the team's API services.",
            "title": "Backend Developer",
            "manager": "cass",
        },
    ],
    "files": {
        "cass": {"AGENTS.md": "# cass\n\nRelease engineer at Halyard Software.\n"},
        "teo": {"AGENTS.md": "# teo\n\nBackend developer at Halyard Software.\n"},
    },
    "turns": {
        "cass": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "browser_navigate",
                        "args": {"url": PAGE},
                        "result": _step("browser_navigate", "opened"),
                        "took": 0.3,
                    },
                    {
                        "tool": "browser_snapshot",
                        "args": {},
                        "result": _step("browser_snapshot", "snapshot"),
                        "took": 0.3,
                    },
                    {
                        "tool": "browser_click",
                        "args": _click_args,
                        "result": _step("browser_click", "clicked"),
                        "took": 0.3,
                    },
                    {
                        "tool": "browser_snapshot",
                        "args": {},
                        "result": _step("browser_snapshot", "history"),
                        "took": 0.3,
                    },
                    {"text": _reply, "wps": 34},
                ]
            },
        ],
    },
}
