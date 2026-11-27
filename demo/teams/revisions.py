"""Saltwick Paddle Co., a kayak and paddleboard hire: the team in the revisions video.

The history is the product's own: ``after_seed`` records a few days of earlier work with
``snapshot_home`` (each commit dated in the past and authored by the agent that made it), then
starts the real revisions worker (``get_revisions_worker``) with a short quiet period. The take's
chat turn changes three tracked files through the product's own code (the file edit, the
``memory_save`` tool, the ``self_update_prompt`` tool), and the worker commits them under the
agent's name, exactly as the runtime does after a real turn (``runner._notify_revisions``).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGENT = "cove"

PRICES_BEFORE = """# Saltwick Paddle Co.: hire prices

| Hire | Hour | Half day | Full day |
|---|---|---|---|
| Single kayak | $18 | $42 | $65 |
| Double kayak | $26 | $58 | $90 |
| Paddleboard | $20 | $45 | $70 |

Life jackets and dry bags are included. Wetsuits are $8 a day.
"""

OLD_ROW = "| Single kayak | $18 | $42 | $65 |"
NEW_ROW = "| Single kayak | $18 | $48 | $65 |"

PROMPT_BEFORE = (
    "You are Cove, the bookings desk at Saltwick Paddle Co., a kayak and paddleboard hire. "
    "Answer questions about hire, prices and tides in plain words, and take bookings."
)
PROMPT_AFTER = PROMPT_BEFORE + " School groups get 10% off every hire; mention it to teachers."

FACT = "School groups get 10% off every hire."

TEAM: dict[str, Any] = {
    "default_agent": AGENT,
    "agents": [
        {
            "name": "cove",
            "emoji": "🛶",
            "description": "Bookings, prices and questions from customers.",
            "title": "Bookings Desk",
            "manager": "tide",
        },
        {
            "name": "tide",
            "emoji": "🌊",
            "description": "Runs the day: weather, tides, staff and the fleet.",
            "title": "Operations Lead",
            "manager": None,
            "reports": ["cove", "gull"],
        },
        {
            "name": "gull",
            "emoji": "📣",
            "description": "Newsletter, social posts and the website.",
            "title": "Marketing",
            "manager": "tide",
        },
    ],
    "files": {
        "cove": {
            "AGENTS.md": "# cove\n\nBookings desk at Saltwick Paddle Co.\n",
            "prices.md": PRICES_BEFORE,
            "MEMORY.md": "# MEMORY.md\n\n## Customers\n- Most bookings come in the evening before.\n",
        },
        "tide": {
            "AGENTS.md": "# tide\n\nOperations lead at Saltwick Paddle Co.\n",
            "fleet.md": "# Fleet\n\n- 14 single kayaks\n- 8 double kayaks\n- 10 paddleboards\n",
        },
        "gull": {
            "AGENTS.md": "# gull\n\nMarketing at Saltwick Paddle Co.\n",
            "newsletter/june.md": "# June newsletter\n\nSunset paddles are back on Fridays.\n",
        },
    },
}


def _edit_prices(ctx: dict[str, Any]) -> str:
    """The file edit, done for real: one string replaced in the agent's workspace file."""
    path = _workspace(AGENT) / "prices.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(OLD_ROW, NEW_ROW, 1), encoding="utf-8")
    return f"Successfully replaced 1 instance(s) of the string in '{path}'"


def _memory_save(ctx: dict[str, Any]) -> str:
    """The product's own ``memory_save`` tool, on the agent's workspace."""
    from olano.memory.markdown_memory import MarkdownMemory, make_memory_tools

    tools = make_memory_tools(MarkdownMemory(_workspace(AGENT)))
    tool = next(t for t in tools if t.name == "memory_save")
    return str(tool.invoke({"fact": FACT, "section": "Prices"}))


def _update_prompt(ctx: dict[str, Any]) -> str:
    """The product's own ``self_update_prompt`` tool, run as the agent (it writes agent.json5)."""
    from olano.tools._credentials import agent_runtime_context
    from olano.tools.self_config_tools import self_update_prompt

    with agent_runtime_context(AGENT, {}):
        return str(self_update_prompt.invoke({"system_prompt": PROMPT_AFTER}))


TEAM["turns"] = {
    "cove": [
        {
            "steps": [
                {"wait": 0.6},
                {
                    "tool": "read_file",
                    "args": {"file_path": "prices.md"},
                    "result": lambda ctx: (_workspace(AGENT) / "prices.md").read_text("utf-8"),
                    "took": 0.8,
                },
                {
                    "tool": "edit_file",
                    "args": {
                        "file_path": "prices.md",
                        "old_string": OLD_ROW,
                        "new_string": NEW_ROW,
                    },
                    "result": _edit_prices,
                    "took": 0.9,
                },
                {
                    "tool": "memory_save",
                    "args": {"fact": FACT, "section": "Prices"},
                    "result": _memory_save,
                    "took": 0.8,
                },
                {
                    "text": "The half-day single kayak is now $48, and I saved the school discount "
                    "to memory. Adding it to my instructions changes my settings, so I need "
                    "your OK first.\n\n",
                    "wps": 16,
                },
                {
                    "tool": "self_update_prompt",
                    "args": {"system_prompt": PROMPT_AFTER},
                    "ask": lambda ctx: [
                        {
                            "name": "self_update_prompt",
                            "args": {"system_prompt": PROMPT_AFTER},
                            "risk": "high",
                        }
                    ],
                    "result": _update_prompt,
                    "took": 0.8,
                },
                {
                    "text": "Done. My instructions now mention the school discount. "
                    "All three changes are in Revisions under my name.",
                    "wps": 16,
                },
            ]
        }
    ]
}


def _workspace(name: str) -> Path:
    return Path(os.environ["LIONCLAW_HOME"]) / "agents" / name / "workspace"


def _at(when: datetime) -> None:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.environ["GIT_AUTHOR_DATE"] = stamp
    os.environ["GIT_COMMITTER_DATE"] = stamp


def after_seed(home: Path, gateway: Any = None, **_: object) -> None:
    """A few days of history, the real revisions worker, and its turn-end hook."""
    from olano.admin.agents import update_agent
    from olano.config.system_settings import set_revisions_settings
    from olano.revisions.engine import snapshot_home
    from olano.revisions.worker import get_revisions_worker, notify_turn

    # A short quiet period (the product's minimum) so the take's turn is committed promptly.
    set_revisions_settings({"debounce_seconds": 5})
    # Each agent.json5 through the product's own save path, so later diffs are line diffs.
    update_agent("cove", {"system_prompt": PROMPT_BEFORE})
    for name in ("tide", "gull"):
        update_agent(name, {})
    # The dashboard's own reader rewrites each file once on its first read (it drops a legacy
    # key the save path writes); do it now, so that rewrite is not part of the take's commit.
    from olano.runtime.api import _read_agent_config

    for name in ("cove", "tide", "gull"):
        _read_agent_config(name)

    now = datetime.now(timezone.utc)
    ws = home / "agents"
    try:
        _at(now - timedelta(days=4, hours=3))
        snapshot_home(reason="first snapshot", home=home)

        _at(now - timedelta(days=3, hours=6))
        (ws / "gull" / "workspace" / "newsletter" / "june.md").write_text(
            "# June newsletter\n\nSunset paddles are back on Fridays.\n\n"
            "## New this month\n- Paddleboard yoga on Sunday mornings, 8 am.\n",
            encoding="utf-8",
        )
        snapshot_home(author="gull", reason="turn: gull", home=home)

        _at(now - timedelta(days=2, hours=1))
        (ws / "tide" / "workspace" / "MEMORY.md").write_text(
            "# MEMORY.md\n\n## Safety\n- No hires when the wind is over 20 knots.\n",
            encoding="utf-8",
        )
        snapshot_home(author="tide", reason="turn: tide", home=home)

        _at(now - timedelta(days=1, hours=5))
        (ws / "cove" / "workspace" / "MEMORY.md").write_text(
            "# MEMORY.md\n\n## Customers\n- Most bookings come in the evening before.\n"
            "- Regulars ask for the same kayak; note the number on the booking.\n",
            encoding="utf-8",
        )
        snapshot_home(author="cove", reason="turn: cove", home=home)

        _at(now - timedelta(hours=20))
        (ws / "tide" / "workspace" / "fleet.md").write_text(
            "# Fleet\n\n- 14 single kayaks\n- 8 double kayaks\n- 12 paddleboards (2 new)\n",
            encoding="utf-8",
        )
        snapshot_home(author="tide", reason="turn: tide", home=home)

        # A note with a password inside a connection URL: history holds it back.
        _at(now - timedelta(hours=3))
        notes = ws / "tide" / "workspace" / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "booking-system.md").write_text(
            "# Booking system\n\nThe nightly export reads the bookings database:\n\n"
            "postgres://export:paddle-2026-night@db.saltwick.example/bookings\n",
            encoding="utf-8",
        )
        (ws / "gull" / "workspace" / "newsletter" / "july.md").write_text(
            "# July newsletter\n\nSchool holidays start on the 20th: book family doubles early.\n",
            encoding="utf-8",
        )
        snapshot_home(author="gull", reason="turn: gull", home=home)
    finally:
        os.environ.pop("GIT_AUTHOR_DATE", None)
        os.environ.pop("GIT_COMMITTER_DATE", None)

    # The worker the runtime server starts at boot, and the hook a real turn calls at its end.
    get_revisions_worker(start=True)
    if gateway is not None:
        for name, runner in gateway._runners.items():
            _hook_turn_end(runner, name, notify_turn)


def _hook_turn_end(runner: Any, name: str, notify: Any) -> None:
    inner = runner.astream_events

    async def astream_events(*args: Any, **kwargs: Any):
        async for event in inner(*args, **kwargs):
            yield event
        notify(name)

    runner.astream_events = astream_events
