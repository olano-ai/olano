"""Endgame Club, a Saturday chess club for kids at a neighbourhood library: the team in the chess video.

In the take the agent's MCP tab adds ``lichess-mcp`` (a published npm package, MIT, run locally with
``npx``; no account or key for public data) as a custom stdio server, and it connects for real: the
dashboard starts it and lists its tools. The club's coach, Nell, then answers two questions:

- today's Lichess puzzle: ``lichess_get_daily_puzzle`` is a real call to that server. Lichess gives
  the puzzle as the game's moves up to the puzzle, so the position (FEN) and the solution in normal
  notation are worked out from those moves with ``python-chess``; the board is drawn as HTML, saved
  with the product's ``write_file`` shape and turned into a PNG by the product's own
  ``render_html_image`` tool, which is how an agent makes a picture without any key.
- how Magnus Carlsen's rating has moved: ``lichess_get_user`` and ``lichess_get_rating_history`` on
  DrNykterstein, the Lichess account Carlsen plays on, both real calls; the chart is drawn from the
  history and rendered the same way.

Every reply is built from what the server returned. Lichess ratings only, never FIDE ones.

What this file adds for this machine only: Node's ``fetch`` here reaches the web only through an
HTTPS proxy, so the server is started with ``NODE_USE_ENV_PROXY=1`` (Node's own switch to honour
``HTTPS_PROXY``). Nothing of it is shown or needed on a normal machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_COMMAND = ("npx", ["lichess-mcp"])
CARLSEN = "DrNykterstein"


# ---------------------------------------------------------------- the MCP server, over stdio


def _saved_command(agent: str) -> tuple[str, list[str]]:
    """The command and arguments the dashboard saved for the Lichess server."""
    home = os.environ.get("LIONCLAW_HOME", "")
    try:
        from olano.config.json5_fast import loads

        cfg = loads((Path(home) / "agents" / agent / "agent.json5").read_text(encoding="utf-8"))
        for srv in cfg.get("mcp_servers") or []:
            if "lichess" in json.dumps(srv):
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
        env = {**os.environ, "NODE_USE_ENV_PROXY": "1"}
        params = StdioServerParameters(
            command=command, args=args, env=env, cwd=tempfile.mkdtemp(prefix="demo-chess-")
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
            return asyncio.run_coroutine_threadsafe(self._call(tool, args), self.loop).result(90)
        except Exception as exc:  # shown as the tool's result, as a real failed call would be
            return f"Error: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        if self.stack is not None:
            fut = asyncio.run_coroutine_threadsafe(self.stack.aclose(), self.loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=20)
        self.loop.call_soon_threadsafe(self.loop.stop)


def _mcp(tool: str, key: str):
    """A scripted tool step whose result is the server's own answer."""

    def run(ctx: dict[str, Any]) -> str:
        if "mcp" not in ctx:
            ctx["mcp"] = _Session(ctx["agent"])
        ctx[key] = ctx["mcp"].call(tool, ctx["args"])
        return ctx[key]

    return run


def _json(text: Any) -> Any:
    try:
        return json.loads(str(text or ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- the product's own tools


@contextlib.contextmanager
def _as_agent(agent: str) -> Iterator[Path]:
    """The context a tool call of this agent runs in: its identity and its workspace."""
    from olano.security.request_context import push_request_context
    from olano.tools._credentials import agent_runtime_context

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / agent / "workspace"
    with (
        agent_runtime_context(agent, {}),
        push_request_context(metadata={"workspace_root": str(workspace)}),
    ):
        yield workspace


def _render(ctx: dict[str, Any]) -> str:
    from olano.tools.render_tools import render_html_image

    with _as_agent(ctx["agent"]):
        return str(render_html_image.invoke(ctx["args"]))


def _write(build):
    """``write_file``: the page the render step draws, built from the real result."""

    def run(ctx: dict[str, Any]) -> str:
        html = build(ctx)
        if not html:
            return "Error: nothing to draw from the server's answer."
        path = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
        path = path / ctx["args"]["file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return f"Updated file {ctx['args']['file_path']}"

    return run


# ---------------------------------------------------------------- the puzzle

_REPLAY = r"""
import json, sys, chess
data = json.loads(sys.stdin.read())
board = chess.Board()
last = None
for san in data["pgn"].split():
    last = board.push_san(san)
fen = board.fen()
turn = "white" if board.turn else "black"
sans = []
b = board.copy()
for uci in data["solution"]:
    move = chess.Move.from_uci(uci)
    sans.append(b.san(move))
    b.push(move)
print(json.dumps({"fen": fen, "turn": turn, "san": sans,
                  "last": [chess.square_name(last.from_square), chess.square_name(last.to_square)] if last else []}))
"""


def _position(puzzle: dict[str, Any]) -> dict[str, Any]:
    """The puzzle's position and solution, from the game's moves (python-chess)."""
    payload = json.dumps(
        {"pgn": str(puzzle.get("pgn") or ""), "solution": list(puzzle.get("solution") or [])}
    )
    try:
        out = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--quiet",
                "--with",
                "chess==1.11.2",
                "python",
                "-c",
                _REPLAY,
            ],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tempfile.gettempdir(),
        )
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def _puzzle(ctx: dict[str, Any]) -> dict[str, Any]:
    if "puzzle_parsed" not in ctx:
        data = _json(ctx.get("puzzle"))
        data = data if isinstance(data, dict) else {}
        pos = _position(data) if data.get("pgn") else {}
        ctx["puzzle_parsed"] = {**data, **pos}
    return ctx["puzzle_parsed"]


GLYPHS = {
    "K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
    "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟",
}  # fmt: skip


def _board_html(ctx: dict[str, Any]) -> str:
    p = _puzzle(ctx)
    fen = str(p.get("fen") or "")
    if not fen:
        return ""
    rows = []
    for rank in fen.split()[0].split("/"):
        row: list[str] = []
        for ch in rank:
            row += [""] * int(ch) if ch.isdigit() else [ch]
        rows.append(row)
    black = p.get("turn") == "black"
    ranks = list(range(8))
    files = list(range(8))
    if black:
        ranks.reverse()
        files.reverse()
    last = set(p.get("last") or [])
    cells = []
    for r in ranks:
        for f in files:
            sq = "abcdefgh"[f] + str(8 - r)
            light = (r + f) % 2 == 0
            piece = rows[r][f]
            cls = "l" if light else "d"
            if sq in last:
                cls += " m"
            colour = "w" if piece.isupper() else "b"
            glyph = GLYPHS.get(piece.lower(), "") if piece else ""
            coord = ""
            if f == files[0]:
                coord += f'<i class="rk">{8 - r}</i>'
            if r == ranks[-1]:
                coord += f'<i class="fl">{"abcdefgh"[f]}</i>'
            cells.append(f'<div class="{cls}">{coord}<span class="{colour}">{glyph}</span></div>')
    side = "Black" if black else "White"
    themes = [t for t in p.get("themes") or [] if isinstance(t, str)]
    mate = next((t for t in themes if t.startswith("mateIn")), "")
    goal = f"mate in {mate[6:]}" if mate else "find the best move"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; background: #1d1b18; font-family: 'DejaVu Sans', sans-serif; color: #f2ede4; }}
.wrap {{ padding: 28px 30px; }}
h1 {{ margin: 0 0 4px; font-size: 30px; }}
p {{ margin: 0 0 18px; color: #c9bfae; font-size: 19px; }}
.board {{ display: grid; grid-template-columns: repeat(8, 80px); grid-template-rows: repeat(8, 80px);
  border-radius: 6px; overflow: hidden; width: 640px; }}
.board div {{ position: relative; display: flex; align-items: center; justify-content: center; }}
.l {{ background: #f0d9b5; }} .d {{ background: #b58863; }}
.l.m {{ background: #cdd26a; }} .d.m {{ background: #aaa23a; }}
span {{ font-size: 64px; line-height: 1; }}
span.w {{ color: #fff; text-shadow: 0 0 2px #000, 0 0 2px #000, 0 0 1px #000; }}
span.b {{ color: #111; }}
i {{ position: absolute; font-style: normal; font-size: 14px; font-weight: 700; color: #6b5640; }}
.d i {{ color: #f0d9b5; }} .rk {{ top: 3px; left: 4px; }} .fl {{ bottom: 2px; right: 5px; }}
</style></head><body><div class="wrap">
<h1>Lichess puzzle of the day</h1>
<p>{side} to move: {goal} · rated {p.get("rating", "?")}</p>
<div class="board">{"".join(cells)}</div></div></body></html>"""


def _puzzle_file(ctx: dict[str, Any]) -> dict[str, Any]:
    pid = str((_json(ctx.get("puzzle")) or {}).get("id") or "today")
    ctx["board_png"] = f"puzzles/daily-{pid}.png"
    return {"file_path": f"puzzles/daily-{pid}.html"}


def _puzzle_render(ctx: dict[str, Any]) -> dict[str, Any]:
    stem = ctx["board_png"][: -len(".png")]
    return {"html": f"{stem}.html", "save_path": ctx["board_png"], "width": 700, "height": 772}


THEME_NAMES = {
    "mateIn1": "mate in 1", "mateIn2": "mate in 2", "mateIn3": "mate in 3",
    "mateIn4": "mate in 4", "mateIn5": "mate in 5", "exposedKing": "exposed king",
    "middlegame": "middlegame", "endgame": "endgame", "opening": "opening",
    "sacrifice": "sacrifice", "clearance": "clearance", "fork": "fork", "pin": "pin",
    "skewer": "skewer", "discoveredAttack": "discovered attack", "long": "long",
    "short": "short", "crushing": "crushing", "advantage": "advantage",
    "kingsideAttack": "kingside attack", "deflection": "deflection",
    "hangingPiece": "hanging piece", "backRankMate": "back-rank mate",
}  # fmt: skip


def _puzzle_reply(ctx: dict[str, Any]) -> str:
    p = _puzzle(ctx)
    rendered = str(ctx.get("rendered") or "")
    if not p.get("fen") or "Saved:" not in rendered:
        if "mcp" in ctx:
            ctx["mcp"].close()
        return (
            "Lichess did not give me today's puzzle this time, so I have no board to show. "
            "I can try again in a minute."
        )
    side = "Black" if p.get("turn") == "black" else "White"
    themes = [THEME_NAMES.get(t, t) for t in p.get("themes") or [] if isinstance(t, str)]
    mate = next((t for t in themes if t.startswith("mate in")), "")
    plays = p.get("plays")
    lines = [
        f"Here is today's Lichess puzzle. **{side} to move**"
        + (f", and it is **{mate}**." if mate else ": find the best move."),
        "",
        f"Saved: {ctx['board_png']}",
        "",
        f"- Puzzle rating: **{p.get('rating', '?')}**"
        + (f", tried **{plays:,}** times" if isinstance(plays, int) else ""),
    ]
    if themes:
        lines.append(f"- Themes: {', '.join(t for t in themes if t != mate)}")
    if p.get("san"):
        lines.append(f"- Solution, when you want it: {' '.join(p['san'])}")
    if p.get("gameUrl"):
        lines.append(f"- From the game {p['gameUrl']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Carlsen's rating

PERFS = ("bullet", "blitz", "rapid")
COLOURS = {"bullet": "#e8a33d", "blitz": "#5aa9e6", "rapid": "#7bc47f"}


def _history(ctx: dict[str, Any]) -> dict[str, list[tuple[date, int]]]:
    out: dict[str, list[tuple[date, int]]] = {}
    data = _json(ctx.get("history"))
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict) or row.get("timeControl") not in PERFS:
            continue
        points = []
        for pt in row.get("history") or []:
            try:
                points.append((date.fromisoformat(str(pt["date"])), int(pt["rating"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(points) >= 2:
            out[row["timeControl"]] = points
    return out


def _chart_html(ctx: dict[str, Any]) -> str:
    hist = _history(ctx)
    if not hist:
        return ""
    w, h, left, right, top, bottom = 1000, 520, 70, 30, 30, 50
    days = [d for pts in hist.values() for d, _ in pts]
    vals = [v for pts in hist.values() for _, v in pts]
    d0, d1 = min(days), max(days)
    lo, hi = (min(vals) // 100) * 100, (max(vals) // 100 + 1) * 100
    span = max((d1 - d0).days, 1)

    def x(d: date) -> float:
        return left + (w - left - right) * (d - d0).days / span

    def y(v: float) -> float:
        return top + (h - top - bottom) * (hi - v) / (hi - lo)

    grid = []
    for v in range(lo, hi + 1, 100):
        grid.append(
            f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="g"/>'
            f'<text x="{left - 10}" y="{y(v) + 5:.1f}" class="t" text-anchor="end">{v}</text>'
        )
    for year in range(d0.year + 1, d1.year + 1):
        gx = x(date(year, 1, 1))
        grid.append(
            f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="{top}" y2="{h - bottom}" class="g"/>'
            f'<text x="{gx:.1f}" y="{h - bottom + 26}" class="t" text-anchor="middle">{year}</text>'
        )
    lines = []
    legend = []
    for perf, pts in hist.items():
        path = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in pts)
        lines.append(
            f'<polyline points="{path}" fill="none" stroke="{COLOURS[perf]}" stroke-width="3"/>'
        )
        legend.append(f'<span><b style="background:{COLOURS[perf]}"></b>{perf.capitalize()}</span>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; background: #16181d; color: #e9eaee; font-family: 'DejaVu Sans', sans-serif; }}
.wrap {{ padding: 26px 30px; }} h1 {{ margin: 0 0 6px; font-size: 28px; }}
.k {{ color: #a9adb8; font-size: 17px; margin-bottom: 10px; }}
.k span {{ margin-right: 22px; }} .k b {{ display: inline-block; width: 22px; height: 6px;
  border-radius: 3px; margin-right: 8px; vertical-align: middle; }}
.g {{ stroke: #2c303a; stroke-width: 1; }} .t {{ fill: #8b909c; font-size: 15px; }}
</style></head><body><div class="wrap">
<h1>DrNykterstein on Lichess</h1>
<div class="k">{"".join(legend)}</div>
<svg width="{w}" height="{h}">{"".join(grid)}{"".join(lines)}</svg>
</div></body></html>"""


def _chart_render(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "html": "ratings/drnykterstein.html",
        "save_path": "ratings/drnykterstein.png",
        "width": 1060,
        "height": 640,
    }


def _rating_reply(ctx: dict[str, Any]) -> str:
    hist = _history(ctx)
    user = _json(ctx.get("user"))
    user = user if isinstance(user, dict) else {}
    if "mcp" in ctx:
        ctx["mcp"].close()
    if not hist:
        return (
            "Lichess did not return a rating history for DrNykterstein this time, "
            "so I will not guess. I can try again in a minute."
        )
    perfs = user.get("perfs") if isinstance(user.get("perfs"), dict) else {}
    title = f" ({user['title']})" if user.get("title") else ""
    rows = ["| | First rated | Peak | Now | Games |", "|---|---|---|---|---|"]
    for perf, pts in hist.items():
        first_d, first_v = pts[0]
        peak_d, peak_v = max(pts, key=lambda p: p[1])
        now = (perfs.get(perf) or {}).get("rating") or pts[-1][1]
        games = (perfs.get(perf) or {}).get("games")
        rows.append(
            f"| {perf.capitalize()} | {first_v} ({first_d:%b %Y}) | **{peak_v}** ({peak_d:%b %Y}) "
            f"| {now} | {f'{games:,}' if isinstance(games, int) else '?'} |"
        )
    saved = "Saved:" in str(ctx.get("chart") or "")
    lines = [
        f"Magnus Carlsen plays on Lichess as **DrNykterstein**{title}. "
        "His Lichess ratings since the account's first rated games:",
        "",
        "\n".join(rows),
    ]
    if saved:
        lines += ["", "Saved: ratings/drnykterstein.png"]
    lines += ["", "These are Lichess ratings, not his FIDE rating."]
    return "\n".join(lines)


TEAM = {
    "default_agent": "nell",
    "agents": [
        {
            "name": "nell",
            "emoji": "♞",
            "description": "Coaches the Saturday group and picks the weekly puzzles.",
            "title": "Club Coach",
            "manager": None,
            "reports": ["otto"],
        },
        {
            "name": "otto",
            "emoji": "🗓️",
            "description": "Sign-ups, room bookings and the club newsletter.",
            "title": "Club Secretary",
            "manager": "nell",
        },
    ],
    "files": {
        "nell": {"AGENTS.md": "# nell\n\nCoach at Endgame Club, a Saturday chess club for kids.\n"},
        "otto": {"AGENTS.md": "# otto\n\nSecretary at Endgame Club.\n"},
    },
    "turns": {
        "nell": [
            {
                "match": "carlsen",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "lichess_get_user",
                        "args": {"username": CARLSEN},
                        "result": _mcp("lichess_get_user", "user"),
                        "took": 0.3,
                    },
                    {
                        "tool": "lichess_get_rating_history",
                        "args": {"username": CARLSEN},
                        "result": _mcp("lichess_get_rating_history", "history"),
                        "took": 0.3,
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "ratings/drnykterstein.html"},
                        "result": _write(_chart_html),
                        "took": 0.4,
                    },
                    {
                        "tool": "render_html_image",
                        "args": _chart_render,
                        "result": lambda ctx: ctx.setdefault("chart", _render(ctx)),
                        "took": 0.2,
                    },
                    {"text": _rating_reply, "wps": 26},
                ],
            },
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "lichess_get_daily_puzzle",
                        "args": {},
                        "result": _mcp("lichess_get_daily_puzzle", "puzzle"),
                        "took": 0.3,
                    },
                    {
                        "tool": "write_file",
                        "args": _puzzle_file,
                        "result": _write(_board_html),
                        "took": 0.4,
                    },
                    {
                        "tool": "render_html_image",
                        "args": _puzzle_render,
                        "result": lambda ctx: ctx.setdefault("rendered", _render(ctx)),
                        "took": 0.2,
                    },
                    {"text": _puzzle_reply, "wps": 22},
                ],
            },
        ],
    },
}
