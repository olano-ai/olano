"""Little Fig Café, a small neighbourhood café: the team in the freeimages video.

In the take the agent's MCP tab connects Pipeworx's public gateway for Openverse
(``https://gateway.pipeworx.io/openverse/mcp``, no account) as a custom server. The scripted turn's
``openverse_search_images`` step is a real call to that server with the official ``mcp`` Python SDK,
and the reply (the photos, their licences and the credit lines) is built from what came back. The
follow-up writes those credit lines to a file in the agent's workspace.

Anonymous use of the gateway is capped at 50 calls a day and Openverse itself throttles busy
callers, so a successful answer is kept in ``.work/freeimages-cache.json`` and later takes reuse
it instead of spending another call. Every answer in that file came from the server; delete it to
ask again.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

URL = "https://gateway.pipeworx.io/openverse/mcp"
TOOL = "openverse_search_images"
SEARCH_ARGS = {"query": "latte art", "license_type": "commercial", "page_size": 8}
CACHE = Path(__file__).resolve().parents[1] / ".work" / "freeimages-cache.json"
SHOW = 4

_RESULTS: dict[str, Any] = {}


async def _call(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    text = "\n".join(str(getattr(c, "text", "")) for c in res.content)
    return bool(res.isError), text


def _search(ctx: dict[str, Any]) -> str:
    """The server's own answer: from the cache of an earlier real call, or asked now."""
    key = json.dumps({"tool": TOOL, "args": SEARCH_ARGS}, sort_keys=True)
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        text = cache[key]["text"]
    else:
        try:
            is_error, text = asyncio.run(_call(TOOL, SEARCH_ARGS))
        except Exception as exc:
            is_error, text = True, f"{type(exc).__name__}: {exc}"
        if is_error:
            text = "Error: " + text
        elif _photos(text):
            cache[key] = {"text": text, "asked": datetime.now().isoformat()}
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    _RESULTS["search"] = text
    return text


def _photos(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text[text.find("{") :]) if "{" in text else {}
    except ValueError:
        return []
    rows = data.get("results") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("license")]


def _licence(r: dict[str, Any]) -> str:
    lic = str(r.get("license") or "").lower()
    ver = str(r.get("license_version") or "").strip()
    if lic in ("cc0", "pdm"):
        return "CC0 (public domain)" if lic == "cc0" else "Public Domain Mark"
    return f"CC {lic.upper()} {ver}".strip()


def _credit(r: dict[str, Any]) -> str:
    if r.get("attribution"):
        return str(r["attribution"]).strip()
    title = str(r.get("title") or "Untitled").strip()
    creator = str(r.get("creator") or "").strip()
    by = f" by {creator}" if creator else ""
    lic_url = f" ({r['license_url']})" if r.get("license_url") else ""
    return f'"{title}"{by} is licensed under {_licence(r)}{lic_url}.'


def _picked() -> list[dict[str, Any]]:
    return _photos(str(_RESULTS.get("search") or ""))[:SHOW]


def _search_reply(ctx: dict[str, Any]) -> str:
    rows = _picked()
    if not rows:
        return (
            "Openverse did not return any photos this time, so I have nothing to show you yet. "
            "I can try again in a few minutes."
        )
    lines = [
        f"Here are {len(rows)} photos of latte art you can use on a poster, commercial use allowed:\n"
    ]
    for i, r in enumerate(rows, 1):
        title = str(r.get("title") or "Untitled").strip()
        creator = str(r.get("creator") or "unknown creator").strip()
        src = str(r.get("source") or r.get("provider") or "").strip()
        link = r.get("foreign_landing_url") or r.get("url") or ""
        name = f"[{title}]({link})" if link else title
        lines.append(
            f"{i}. **{name}** by {creator}, {_licence(r)}" + (f", from {src}" if src else "")
        )
    needs = [r for r in rows if str(r.get("license") or "").lower() not in ("cc0", "pdm")]
    if needs:
        lines.append(
            f"\n{len(needs)} of them {'needs' if len(needs) == 1 else 'need'} a credit line on the "
            "poster. Want me to write them out?"
        )
    return "\n".join(lines)


def _credits_file() -> str:
    rows = _picked()
    body = ["# Photo credits for the latte art poster", ""]
    for r in rows:
        body.append(f"- {_credit(r)}")
    body.append("")
    body.append("Found with Openverse. Check each source page before printing.")
    return "\n".join(body) + "\n"


def _write(ctx: dict[str, Any]) -> str:
    path = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
    path = path / "poster" / "photo-credits.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _credits_file()
    path.write_text(text, encoding="utf-8")
    return f"Updated file {path}"


def _credits_reply(ctx: dict[str, Any]) -> str:
    rows = _picked()
    if not rows:
        return "I have no photos to credit yet: the search came back empty."
    lines = ["Here are the credit lines, one per photo:\n"]
    lines += [f"- {_credit(r)}" for r in rows]
    lines.append("\nSaved: poster/photo-credits.md")
    return "\n".join(lines)


TEAM = {
    "default_agent": "otto",
    "agents": [
        {
            "name": "otto",
            "emoji": "🖼️",
            "description": "Posters, menus and the café's social posts.",
            "title": "Marketing Helper",
            "manager": None,
        },
    ],
    "files": {
        "otto": {
            "AGENTS.md": "# otto\n\nMarketing helper at Little Fig Café, a neighbourhood café.\n",
        },
    },
    "turns": {
        "otto": [
            {
                "match": "write them",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "write_file",
                        "args": lambda ctx: {
                            "file_path": "poster/photo-credits.md",
                            "content": _credits_file(),
                        },
                        "result": _write,
                        "took": 0.5,
                    },
                    {"text": _credits_reply, "wps": 28},
                ],
            },
            {
                "steps": [
                    {"wait": 0.4},
                    {"tool": TOOL, "args": SEARCH_ARGS, "result": _search, "took": 0.5},
                    {"text": _search_reply, "wps": 28},
                ],
            },
        ],
    },
}
