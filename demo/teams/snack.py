"""The Okafor household: a family's home agent, the team in the snack video.

In the take the agent's MCP tab adds ``@cyanheads/openfoodfacts-mcp-server`` (a published npm
package, run locally with ``npx``; Open Food Facts needs no account or key) as a custom stdio
server, and it connects for real: the dashboard starts it and lists its tools. Basil then compares
two breakfast cereals by the barcodes on their boxes: ``off_compare_products`` and two
``off_get_product`` calls are real calls to that server, made with the official ``mcp`` Python SDK,
and the reply is built from what came back (Nutri-Score, NOVA group, sugar, additives). Facts from
the labels only; no dietary or medical advice.

What this file adds for this machine only (``after_seed``):

- The server needs Node.js 24 and this machine's default ``node`` is 22, so Node 24's folder goes
  first on the runtime's ``PATH`` once the dashboard is up (``/opt/node24/bin`` when it exists).
- Node's ``fetch`` reaches the web here only through an HTTPS proxy, so ``NODE_USE_ENV_PROXY=1``
  (Node's own switch to honour ``HTTPS_PROXY``) is set for the server too.

Neither is shown or needed on a normal machine with a current Node.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

DEFAULT_COMMAND = ("npx", ["@cyanheads/openfoodfacts-mcp-server"])
NODE24 = "/opt/node24/bin"

# Two boxes as their barcodes read: Kellogg's Froot Loops (345 g, sold in Canada) and General Mills
# Cheerios (8.9 oz, sold in the United States), both well filled in on Open Food Facts.
FROOT_LOOPS = "0064100595729"
CHEERIOS = "0016000275263"

ADDITIVE_NAMES = {
    "e100": "curcumin (colour)", "e101": "riboflavin", "e102": "tartrazine (colour)",
    "e110": "sunset yellow (colour)", "e129": "allura red (colour)",
    "e133": "brilliant blue (colour)", "e150a": "caramel (colour)",
    "e150c": "ammonia caramel (colour)", "e160a": "carotenes (colour)",
    "e160b": "annatto (colour)", "e300": "vitamin C", "e301": "sodium ascorbate",
    "e306": "vitamin E", "e307": "alpha-tocopherol", "e320": "BHA (preservative)",
    "e321": "BHT (preservative)", "e322": "lecithins", "e330": "citric acid",
    "e339": "sodium phosphates", "e340": "potassium phosphates", "e341": "calcium phosphates", "e375": "niacin",
    "e422": "glycerol", "e471": "mono- and diglycerides", "e500": "sodium carbonates",
    "e503": "ammonium carbonates", "e1400": "dextrins", "e1422": "modified starch",
}  # fmt: skip
NOVA = {
    "1": "unprocessed or minimally processed",
    "2": "processed culinary ingredients",
    "3": "processed",
    "4": "ultra-processed",
}


# ---------------------------------------------------------------- the MCP server, over stdio


def _saved_command(agent: str) -> tuple[str, list[str]]:
    """The command and arguments the dashboard saved for the Open Food Facts server."""
    home = os.environ.get("LIONCLAW_HOME", "")
    try:
        from olano.config.json5_fast import loads

        cfg = loads((Path(home) / "agents" / agent / "agent.json5").read_text(encoding="utf-8"))
        for srv in cfg.get("mcp_servers") or []:
            if "openfoodfacts" in json.dumps(srv):
                cmd = srv.get("command")
                if cmd:
                    return str(cmd), [str(a) for a in srv.get("args") or []]
    except Exception:
        pass
    return DEFAULT_COMMAND


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

        command, args = _saved_command(self.agent)
        if command == "npx" and "-y" not in args:
            args = ["-y", *args]
        params = StdioServerParameters(
            command=command,
            args=args,
            env=dict(os.environ),
            cwd=tempfile.mkdtemp(prefix="demo-snack-"),
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
            return asyncio.run_coroutine_threadsafe(self._call(tool, args), self.loop).result(120)
        except Exception as exc:  # shown as the tool's result, as a real failed call would be
            return f"Error: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        if self.stack is not None:
            fut = asyncio.run_coroutine_threadsafe(self.stack.aclose(), self.loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=20)
        self.loop.call_soon_threadsafe(self.loop.stop)


def _mcp(tool: str, key: str):
    def run(ctx: dict[str, Any]) -> str:
        if "mcp" not in ctx:
            ctx["mcp"] = _Session(ctx["agent"])
        ctx[key] = ctx["mcp"].call(tool, ctx["args"])
        return ctx[key]

    return run


# ---------------------------------------------------------------- reading the server's answers


def _field(text: str, label: str) -> str:
    """A ``**Label:** value`` line, or a nested ``- Label: value`` one (sugars under carbs)."""
    m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", text)
    if not m:
        m = re.search(rf"^\s*- {re.escape(label)}:\s*(.+)", text, flags=re.M)
    return m.group(1).strip() if m else ""


def _num(text: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def _product(text: str) -> dict[str, Any]:
    """The facts the reply needs, from one ``off_get_product`` answer (markdown)."""
    if not text or text.startswith("Error"):
        return {}
    name = re.search(r"^## (.+)$", text, flags=re.M)
    scores = _field(text, "Scores")
    nutri = re.search(r"Nutri-Score:\s*([a-e])", scores, flags=re.I)
    nova = re.search(r"NOVA:\s*(\d)", scores)
    serving = _field(text, "Serving size")
    per100 = text.split("### Nutrition per 100 g", 1)[-1].split("###", 1)[0]
    per_serving = ""
    if "### Nutrition per serving" in text:
        per_serving = text.split("### Nutrition per serving", 1)[1].split("###", 1)[0]
    serving_g = re.search(r"per ([\d.]+) ?g", text.split("### Nutrition per serving", 1)[-1][:80])
    additives = [
        a.strip().removeprefix("en:")
        for a in _field(text, "Additives").split(",")
        if a.strip().startswith("en:")
    ]
    return {
        "name": name.group(1).strip() if name else "",
        "brand": _field(text, "Brand"),
        "nutri": nutri.group(1).upper() if nutri else "",
        "nova": nova.group(1) if nova else "",
        "sugar100": _num(_field(per100, "Sugars")),
        "fibre100": _num(_field(per100, "Fiber")),
        "salt100": _num(_field(per100, "Salt")),
        "protein100": _num(_field(per100, "Protein")),
        "sugar_serving": _num(_field(per_serving, "Sugars")) if per_serving else None,
        "serving": re.sub(r"\s*\(parsed:[^)]*\)", "", serving).strip(),
        "serving_g": float(serving_g.group(1)) if serving_g else None,
        "additives": additives,
    }


def _g(v: float | None) -> str:
    if v is None:
        return "not entered"
    return f"{v:.0f} g" if v >= 10 else f"{v:.1f} g".replace(".0 g", " g")


def _additives(codes: list[str]) -> str:
    """E-numbers with their common names; a sub-code (E340iii) folds into its family (E340)."""
    codes = [c for c in codes if not any(c != o and c.startswith(o) for o in codes)]
    if not codes:
        return "none listed"
    return ", ".join(f"E{c[1:]} {ADDITIVE_NAMES.get(c, '')}".strip() for c in codes)


def _reply(ctx: dict[str, Any]) -> str:
    a = _product(str(ctx.get("first") or ""))
    b = _product(str(ctx.get("second") or ""))
    if "mcp" in ctx:
        ctx["mcp"].close()
    if not a or not b:
        return (
            "Open Food Facts did not give me both products this time, so I will not compare "
            "half a picture. I can try again in a minute."
        )
    na, nb = a["name"] or "the first box", b["name"] or "the second box"
    rows = [
        f"| | **{na}** | **{nb}** |",
        "|---|---|---|",
        f"| Nutri-Score (A best, E worst) | **{a['nutri'] or '?'}** | **{b['nutri'] or '?'}** |",
        f"| NOVA group | {a['nova'] or '?'}: {NOVA.get(a['nova'], 'not rated')} "
        f"| {b['nova'] or '?'}: {NOVA.get(b['nova'], 'not rated')} |",
        f"| Sugar per 100 g | {_g(a['sugar100'])} | {_g(b['sugar100'])} |",
        f"| Sugar per bowl | {_g(a['sugar_serving'])} in {a['serving'] or '?'} "
        f"| {_g(b['sugar_serving'])} in {b['serving'] or '?'} |",
        f"| Fibre per 100 g | {_g(a['fibre100'])} | {_g(b['fibre100'])} |",
        f"| Additives | {_additives(a['additives'])} | {_additives(b['additives'])} |",
    ]
    lines = ["Here is what the two labels say, from Open Food Facts:", "", "\n".join(rows), ""]
    grades = "ABCDE"
    if a["nutri"] in grades and b["nutri"] in grades and a["nutri"] != b["nutri"]:
        better, worse = (a, b) if grades.index(a["nutri"]) < grades.index(b["nutri"]) else (b, a)
        lines.append(
            f"By these scores **{better['name']}** comes out ahead: Nutri-Score "
            f"{better['nutri']} against {worse['nutri']}."
        )
    sa, sb = a["sugar_serving"], b["sugar_serving"]
    if sa and sb and max(sa, sb) >= 2 * min(sa, sb):
        hi, lo = (a, b) if sa > sb else (b, a)
        lines.append(
            f"A bowl of **{hi['name']}** has about **{max(sa, sb) / min(sa, sb):.0f} times** "
            f"the sugar of a bowl of {lo['name']}."
        )
    lines += [
        "",
        "These are the labels' own figures as entered on Open Food Facts, not dietary advice.",
    ]
    return "\n".join(lines)


def after_seed(**_: Any) -> None:
    """This machine only: Node 24 first on PATH, and Node's fetch through the HTTPS proxy."""
    if Path(NODE24).is_dir():
        os.environ["PATH"] = NODE24 + os.pathsep + os.environ.get("PATH", "")
    os.environ["NODE_USE_ENV_PROXY"] = "1"


TEAM = {
    "default_agent": "basil",
    "agents": [
        {
            "name": "basil",
            "emoji": "🥣",
            "description": "Groceries, meal plans and the family calendar.",
            "title": "Home Helper",
            "manager": None,
        },
    ],
    "files": {
        "basil": {
            "AGENTS.md": "# basil\n\nHome helper for the Okafor household: groceries and meal plans.\n"
        },
    },
    "turns": {
        "basil": [
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "off_compare_products",
                        "args": {"barcodes": [FROOT_LOOPS, CHEERIOS]},
                        "result": _mcp("off_compare_products", "compare"),
                        "took": 0.3,
                    },
                    {
                        "tool": "off_get_product",
                        "args": {"barcode": FROOT_LOOPS},
                        "result": _mcp("off_get_product", "first"),
                        "took": 0.3,
                    },
                    {
                        "tool": "off_get_product",
                        "args": {"barcode": CHEERIOS},
                        "result": _mcp("off_get_product", "second"),
                        "took": 0.3,
                    },
                    {"text": _reply, "wps": 26},
                ]
            },
        ],
    },
}
