"""Saltmarsh Cycles, a bike shop with a workshop: the team in the connect video.

Nothing is seeded as connected. In the take the MCP server is added from the directory and
connects for real (Parallel's free web search server, which needs no account), and the link Milo
sends is minted by the product's own ``magic_link_connect_service`` tool.
"""

from __future__ import annotations

from typing import Any


def _magic(tool: str, **kwargs: Any):
    """A scripted tool step whose result is the real magic-link tool's own output."""

    def run(ctx: dict[str, Any]) -> str:
        from olano.security.request_context import push_request_context
        from olano.tools import magic_link_tools
        from olano.tools._credentials import agent_runtime_context

        with agent_runtime_context(ctx["agent"], None), push_request_context(
            channel="api", session_id=ctx["thread_id"], is_owner=True
        ):
            return str(getattr(magic_link_tools, tool).invoke(kwargs))

    return run


TEAM = {
    "default_agent": "milo",
    "agents": [
        {
            "name": "milo",
            "emoji": "🚲",
            "description": "Bookings, parts orders and customer questions.",
            "title": "Service Desk",
            "manager": None,
            "reports": ["wren"],
        },
        {
            "name": "wren",
            "emoji": "🔩",
            "description": "Workshop jobs and the parts shelf.",
            "title": "Workshop Planner",
            "manager": "milo",
        },
    ],
    "files": {
        "milo": {"AGENTS.md": "# milo\n\nService desk at Saltmarsh Cycles.\n"},
        "wren": {"AGENTS.md": "# wren\n\nWorkshop planner at Saltmarsh Cycles.\n"},
    },
    "turns": {
        "milo": [
            {
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "magic_link_connect_service",
                        "args": {"service_name": "slack"},
                        "result": _magic("magic_link_connect_service", service_name="slack"),
                        "took": 0.9,
                    },
                    {
                        "text": "Here is a secure link for Slack. Use the button below and enter the PIN. "
                        "You sign in with Slack itself, and nothing is pasted into this chat.",
                        "wps": 14,
                    },
                ]
            },
        ],
    },
}


def after_seed(dash: Any, **_: object) -> None:
    """Magic links are built on the address the dashboard is served from."""
    import os

    os.environ["LIONCLAW_PUBLIC_URL"] = dash.base_url
