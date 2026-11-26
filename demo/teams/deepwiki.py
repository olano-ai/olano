"""Tessellate Data, a small data platform team: the team in the DeepWiki video.

DeepWiki's public MCP server (``https://mcp.deepwiki.com/mcp``, no account) is added from the MCP
directory during the take and connects for real. In the chat, Ines's tool steps are real calls to
that server, made with the official ``mcp`` Python SDK, and the reply is built from what they
returned: the wiki's own section list and the answer DeepWiki gave to the question.

Olano's own repository is not indexed on DeepWiki, so the question is about a well-known public
repository that is: ``langchain-ai/langgraph``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

URL = "https://mcp.deepwiki.com/mcp"
REPO = "langchain-ai/langgraph"
QUESTION = "How is the repository organised into packages under libs/, and what does each one do?"


def _call(tool: str, args: dict[str, Any]) -> str:
    """One real call to DeepWiki's MCP server; the text it returned."""
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


def _tool(tool: str, args: dict[str, Any], key: str):
    def run(ctx: dict[str, Any]) -> str:
        ctx[key] = _call(tool, args)
        return ctx[key]

    return run


def _plain(text: str) -> str:
    """The house style has no long dashes; DeepWiki's prose does."""
    return text.replace(" \u2014 ", ": ", 1).replace("\u2014", ",")


def _reply(ctx: dict[str, Any]) -> str:
    structure = ctx.get("structure", "")
    answer = ctx.get("answer", "")
    sections = re.findall(r"^- (\d+) (.+)$", structure, flags=re.M)
    bullets = [_plain(ln.strip()) for ln in answer.splitlines() if ln.strip().startswith("- `")]
    parts = [f"Here is how **{REPO}** is organised, from its DeepWiki pages."]
    if sections:
        parts.append(
            "**The wiki's sections:** " + "; ".join(f"{n}. {t.strip()}" for n, t in sections) + "."
        )
    if bullets:
        parts.append(
            "**The packages in `libs/`**, as DeepWiki describes them:\n\n" + "\n".join(bullets)
        )
    elif answer and not answer.startswith("Error"):
        body = answer.split("## Answer", 1)[-1].split("## Notes", 1)[0].strip()
        parts.append("**DeepWiki's answer:**\n\n" + _plain(body[:900]))
    if not sections and not bullets:
        parts.append("DeepWiki did not return an answer this time, so I have nothing to add yet.")
    link = re.search(r"https://deepwiki\.com/search/\S+", answer)
    if link:
        parts.append(f"The full answer, with its sources: {link.group(0)}")
    return "\n\n".join(parts)


TEAM = {
    "default_agent": "ines",
    "agents": [
        {
            "name": "ines",
            "emoji": "📚",
            "description": "Reads code and docs, and answers the team's how-does-it-work questions.",
            "title": "Platform Engineer",
            "manager": None,
            "reports": ["arlo"],
        },
        {
            "name": "arlo",
            "emoji": "🛠️",
            "description": "Keeps the pipelines and their alerts running.",
            "title": "Pipeline On-call",
            "manager": "ines",
        },
    ],
    "files": {
        "ines": {"AGENTS.md": "# ines\n\nPlatform engineer at Tessellate Data.\n"},
        "arlo": {"AGENTS.md": "# arlo\n\nPipeline on-call at Tessellate Data.\n"},
    },
    "turns": {
        "ines": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_wiki_structure",
                        "args": {"repoName": REPO},
                        "result": _tool("read_wiki_structure", {"repoName": REPO}, "structure"),
                        "took": 0.4,
                    },
                    {
                        "tool": "ask_wiki_question",
                        "args": {"repoName": REPO, "question": QUESTION},
                        "result": _tool(
                            "ask_wiki_question", {"repoName": REPO, "question": QUESTION}, "answer"
                        ),
                        "took": 0.4,
                    },
                    {"text": _reply, "wps": 36},
                ]
            },
        ],
    },
}
