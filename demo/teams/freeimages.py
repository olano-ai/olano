"""Little Fig Café, a small neighbourhood café: the team in the freeimages video.

In the take the agent's MCP tab connects Pipeworx's public gateway for Wikimedia Commons
(``https://gateway.pipeworx.io/commons-wikimedia/mcp``, no account) as a custom server. The scripted
turn's ``commons_wikimedia_search`` and ``file_info`` steps are real calls to that server with the
official ``mcp`` Python SDK (the files looked up are the first photos the search returned), and the
reply, with each photo's licence and credit line, is built from what came back. The follow-up writes
the credit lines to a file in the agent's workspace.

The brief asked for Pipeworx's Openverse server (``/openverse/mcp``); on the day of recording every
call to its image search came back ``Openverse: rate-limit (HTTP 429)`` for over an hour, so the
video uses the Wikimedia Commons server on the same gateway, which answers the same question.

Anonymous use of the gateway is capped at 50 calls a day, so each successful answer is kept in
``.work/freeimages-cache.json`` and later takes reuse it instead of spending another call. Every
answer in that file came from the server; delete it to ask again.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

URL = "https://gateway.pipeworx.io/commons-wikimedia/mcp"
SEARCH_ARGS = {"query": "latte art", "limit": 8}
CACHE = Path(__file__).resolve().parents[1] / ".work" / "freeimages-cache.json"
SHOW = 3

_RESULTS: dict[str, Any] = {"info": []}


async def _call_async(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    text = "\n".join(str(getattr(c, "text", "")) for c in res.content)
    return bool(res.isError), text


def _call(tool: str, args: dict[str, Any]) -> str:
    """The server's own answer: from the cache of an earlier real call, or asked now."""
    key = json.dumps({"url": URL, "tool": tool, "args": args}, sort_keys=True)
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]["text"]
    try:
        is_error, text = asyncio.run(_call_async(tool, args))
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"
    if is_error:
        return "Error: " + text
    cache[key] = {"text": text, "asked": datetime.now().isoformat()}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return text


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text[text.find("{") :]) if "{" in text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _titles() -> list[str]:
    hits = (_json(str(_RESULTS.get("search") or "")).get("query") or {}).get("search") or []
    out = []
    for h in hits:
        title = str(h.get("title") or "") if isinstance(h, dict) else ""
        if re.search(r"\.(jpe?g|png|webp)$", title, flags=re.I):
            out.append(title)
    return out[:SHOW]


def _search(ctx: dict[str, Any]) -> str:
    _RESULTS["search"] = _call("commons_wikimedia_search", SEARCH_ARGS)
    _RESULTS["info"] = []
    return _RESULTS["search"]


def _info_args(i: int):
    def args(ctx: dict[str, Any]) -> dict[str, Any]:
        titles = _titles()
        return {"title": titles[i] if i < len(titles) else "File:Latte art.jpg"}

    return args


def _info(ctx: dict[str, Any]) -> str:
    text = _call("file_info", ctx["args"])
    _RESULTS["info"].append(text)
    return text


def _plain(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _photos() -> list[dict[str, Any]]:
    out = []
    for text in _RESULTS.get("info") or []:
        pages = (_json(str(text)).get("query") or {}).get("pages") or []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0] if isinstance(page, dict) else {}
            meta = info.get("extmetadata") or {}

            def m(name: str, meta: dict[str, Any] = meta) -> str:
                return _plain((meta.get(name) or {}).get("value"))

            licence = m("LicenseShortName")
            if not licence:
                continue
            out.append(
                {
                    "title": m("ObjectName") or str(page.get("title", "")).removeprefix("File:"),
                    "artist": m("Artist") or str(info.get("user") or ""),
                    "licence": licence,
                    "licence_url": m("LicenseUrl"),
                    "page": str(info.get("descriptionurl") or ""),
                    "credit_needed": m("AttributionRequired").lower() != "false",
                    "nc": "nc" in licence.lower(),
                }
            )
    return [p for p in out if not p["nc"]]


def _credit(p: dict[str, Any]) -> str:
    by = f" by {p['artist']}" if p["artist"] else ""
    lic = f"{p['licence']} ({p['licence_url']})" if p["licence_url"] else p["licence"]
    return f'"{p["title"]}"{by}, {lic}, via Wikimedia Commons.'


def _search_reply(ctx: dict[str, Any]) -> str:
    rows = _photos()
    if not rows:
        return (
            "Wikimedia Commons did not give me any photos with a licence I could read this time, "
            "so I have nothing to show you yet. I can try again in a few minutes."
        )
    lines = [
        f"Here are {len(rows)} photos of latte art you can use on a poster, from Wikimedia Commons:\n"
    ]
    for i, p in enumerate(rows, 1):
        name = f"[{p['title']}]({p['page']})" if p["page"] else p["title"]
        lines.append(
            f"{i}. **{name}**" + (f" by {p['artist']}" if p["artist"] else "") + f": {p['licence']}"
        )
    lines.append("\nAll of them allow commercial use, so a café poster is fine.")
    if any("by-sa" in p["licence"].lower() for p in rows):
        lines[-1] += (
            " BY-SA means: credit the photographer, and if you edit the photo, share your "
            "version under the same licence."
        )
    needs = [p for p in rows if p["credit_needed"]]
    if needs:
        who = "Each one needs" if len(needs) == len(rows) else f"{len(needs)} of them need"
        lines.append(f"\n{who} a credit line on the poster. Want me to write them out?")
    return "\n".join(lines)


def _credits_file() -> str:
    body = ["# Photo credits for the latte art poster", ""]
    body += [f"- {_credit(p)}" for p in _photos()]
    body += ["", "From Wikimedia Commons. Check each file page before printing."]
    return "\n".join(body) + "\n"


def _write(ctx: dict[str, Any]) -> str:
    path = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
    path = path / "poster" / "photo-credits.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_credits_file(), encoding="utf-8")
    return f"Updated file {path}"


def _credits_reply(ctx: dict[str, Any]) -> str:
    rows = _photos()
    if not rows:
        return "I have no photos to credit yet: the search came back empty."
    lines = ["Here are the credit lines, one per photo:\n"]
    lines += [f"- {_credit(p)}" for p in rows]
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
                    {
                        "tool": "commons_wikimedia_search",
                        "args": SEARCH_ARGS,
                        "result": _search,
                        "took": 0.5,
                    },
                    *[
                        {"tool": "file_info", "args": _info_args(i), "result": _info, "took": 0.3}
                        for i in range(SHOW)
                    ],
                    {"text": _search_reply, "wps": 28},
                ],
            },
        ],
    },
}
