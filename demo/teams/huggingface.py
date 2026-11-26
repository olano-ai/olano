"""Lontar Labs, a small team building a voice-notes app: the team in the Hugging Face video.

Hugging Face's MCP server (``https://huggingface.co/mcp``) is added during the take as a custom
server with no account, and connects for real; ``hf_whoami`` is switched off on screen. In the
chat, Sekar's tool steps are real calls to that server, made with the official ``mcp`` Python SDK:
a Hub search, then the details of the top results the search returned. The reply is built from
those two results only (names, licences, languages, links), and the model it recommends is the
first of them, the most downloaded, which is what the reply says.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

URL = "https://huggingface.co/mcp"
SEARCH = {
    "query": "indonesian",
    "repo_types": ["model"],
    "filters": ["automatic-speech-recognition"],
    "sort": "downloads",
    "limit": 5,
}


def _call(tool: str, args: dict[str, Any]) -> str:
    """One real, anonymous call to Hugging Face's MCP server; the text it returned."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go() -> str:
        async with streamablehttp_client(URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, args)
                return "\n".join(str(getattr(c, "text", "")) for c in res.content).strip()

    try:
        return asyncio.run(asyncio.wait_for(go(), 120))
    except Exception as exc:  # shown as the tool's result, as a real failed call would be
        return f"Error: {type(exc).__name__}: {exc}"


def _search(ctx: dict[str, Any]) -> str:
    ctx["search"] = _call("hub_repo_search", SEARCH)
    ctx["ids"] = re.findall(r"^### (\S+/\S+)\s*$", ctx["search"], flags=re.M)[:3]
    return ctx["search"]


def _details_args(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"repo_ids": ctx.get("ids") or [], "repo_type": "model"}


def _details(ctx: dict[str, Any]) -> str:
    ctx["details"] = _call("hub_repo_details", ctx["args"]) if ctx["args"].get("repo_ids") else ""
    return ctx["details"]


def _cards(text: str) -> list[dict[str, str]]:
    """The model cards in a hub_repo_details result, field by field."""
    cards = []
    for block in re.split(r"^# ", text, flags=re.M)[1:]:
        name = block.splitlines()[0].strip()

        def field(label: str, block: str = block) -> str:
            m = re.search(rf"\*\*{label}:\*\*\s*(.+)", block)
            return m.group(1).strip() if m else ""

        link = re.search(r"\*\*Link:\*\* \[(https://[^\]]+)\]", block)
        cards.append(
            {
                "name": name,
                "license": field("License"),
                "language": field("Language"),
                "datasets": field("Datasets"),
                "arch": field("Architecture"),
                "link": link.group(1) if link else f"https://hf.co/{name}",
            }
        )
    return cards


def _reply(ctx: dict[str, Any]) -> str:
    cards = [c for c in _cards(ctx.get("details", "")) if c["name"]]
    if not cards:
        return (
            "The Hub search did not give me model cards I could compare this time, "
            "so I have no recommendation yet."
        )
    lines = [
        "I searched the Hub for Indonesian speech-to-text models, by downloads, and read the top cards:\n"
    ]
    for c in cards:
        bits = [b for b in (c["arch"], f"licence {c['license']}" if c["license"] else "") if b]
        lang = f", languages: {c['language']}" if c["language"] else ""
        lines.append(f"- **{c['name']}**: {', '.join(bits)}{lang}")
    top = cards[0]
    lic = f" It is licensed **{top['license']}**." if top["license"] else ""
    data = f" Trained on: {top['datasets']}." if top["datasets"] else ""
    lines.append(
        f"\n**My pick: {top['name']}**, the most downloaded of these.{lic}{data}\n\n{top['link']}"
    )
    return "\n".join(lines)


TEAM = {
    "default_agent": "sekar",
    "agents": [
        {
            "name": "sekar",
            "emoji": "🎙️",
            "description": "Researches models and libraries for the app's speech features.",
            "title": "ML Engineer",
            "manager": None,
            "reports": ["dimas"],
        },
        {
            "name": "dimas",
            "emoji": "📱",
            "description": "Builds and ships the mobile app.",
            "title": "App Developer",
            "manager": "sekar",
        },
    ],
    "files": {
        "sekar": {"AGENTS.md": "# sekar\n\nML engineer at Lontar Labs.\n"},
        "dimas": {"AGENTS.md": "# dimas\n\nApp developer at Lontar Labs.\n"},
    },
    "turns": {
        "sekar": [
            {
                "steps": [
                    {"wait": 0.5},
                    {"tool": "hub_repo_search", "args": SEARCH, "result": _search, "took": 0.4},
                    {
                        "tool": "hub_repo_details",
                        "args": _details_args,
                        "result": _details,
                        "took": 0.4,
                    },
                    {"text": _reply, "wps": 34},
                ]
            },
        ],
    },
}
