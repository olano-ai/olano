"""Brightmoor Architects, a small architecture practice: the team in the seats video.

A multi-user dashboard: the owner is an admin, the others are members, and each person owns one
agent. Everything about people goes through the product's own auth: the first admin is created by
``/api/auth/bootstrap`` (the first-run endpoint), Tom's account by ``/api/admin/users`` as that
admin, and his seat by ``/api/agents/{name}/access``. Ines is invited during the take, from the
Admin page, and signs in through her invite link.

When cobb (Ines's agent) asks tilde (Tom's agent) for something, the step runs tilde's own runner
on the pair thread the real ``talk_to_agent`` uses (``acp::cobb::tilde``), so the exchange is in
tilde's transcript; tilde's later answer to Tom is read from that transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ADMIN = {
    "name": "Nadia Okafor",
    "email": "nadia@brightmoor.example",
    "password": "brightmoor-nadia-1",
    "username": "nadia",
}
TOM = {
    "name": "Tom Reyes",
    "email": "tom@brightmoor.example",
    "password": "brightmoor-tom-1",
    "username": "tom-reyes",
    "title": "Architect",
}

AREA_Q = "What is the floor area of the Fenwick extension in plan revision C?"
AREA_A = (
    "The Fenwick extension in revision C is 42 m² on the ground floor: "
    "a kitchen-diner of 31 m² and a utility room of 11 m². Roof lantern over the diner."
)

COST_REPLY = (
    "I asked Tom's agent, tilde, for the floor area in plan revision C: **42 m²**.\n\n"
    "| Item | Rate | Cost |\n|---|---|---|\n"
    "| Extension shell, 42 m² | £2,350 / m² | £98,700 |\n"
    "| Roof lantern | fixed | £6,800 |\n"
    "| Contingency, 10% | | £10,550 |\n"
    "| **Total** | | **£116,050** |\n\n"
    "The estimate is in `costs/fenwick-estimate.md`."
)

ESTIMATE = """# Fenwick extension: first estimate

Floor area: 42 m² (plan revision C, from tilde)

| Item | Rate | Cost |
|---|---|---|
| Extension shell, 42 m² | £2,350 / m² | £98,700 |
| Roof lantern | fixed | £6,800 |
| Contingency, 10% | | £10,550 |
| Total | | £116,050 |
"""

PAIR_THREAD = "acp::cobb::tilde"


def _serve() -> Any:
    """The running serve.py (it is ``__main__``): its gateway and loop."""
    return sys.modules["__main__"]


def _ask_tilde(ctx: dict) -> str:
    """tilde answers cobb on the pair thread, with its own runner, on the runtime's loop."""
    serve = _serve()
    runner = serve.GATEWAY._runners["tilde"]
    fut = asyncio.run_coroutine_threadsafe(
        runner.ainvoke(AREA_Q, thread_id=PAIR_THREAD), serve.LOOP
    )
    return fut.result(timeout=120)


def _what_came_in(ctx: dict) -> str:
    """tilde tells Tom what it answered for a colleague's agent, from its own transcript."""
    home = Path(os.environ["LIONCLAW_HOME"])
    path = home / "sessions" / "tilde" / f"{PAIR_THREAD}.jsonl"
    rows = []
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    asked = next((r["content"] for r in rows if r.get("role") == "user"), "")
    answered = next((r["content"] for r in reversed(rows) if r.get("role") == "assistant"), "")
    if not asked:
        return "Nothing came in for you today. No one else's agent has asked me anything."
    return (
        "One request today, from **cobb**, Ines's agent, working on the Fenwick costing.\n\n"
        f"> {asked}\n\n"
        f"I answered from plan revision C:\n\n> {answered}\n\n"
        "Nothing else was shared. Your drawings stayed in your workspace."
    )


TEAM = {
    "default_agent": "orla",
    "agents": [
        {
            "name": "orla",
            "emoji": "🧭",
            "description": "Runs Nadia's projects, fees and client letters.",
            "title": "Practice Lead",
            "manager": None,
            "reports": ["tilde", "cobb"],
        },
        {
            "name": "tilde",
            "emoji": "📐",
            "description": "Tom's design assistant: plans, revisions, areas.",
            "title": "Design",
            "manager": "orla",
        },
        {
            "name": "cobb",
            "emoji": "🧮",
            "description": "Cost plans and estimates for the cost planner.",
            "title": "Cost Planning",
            "manager": "orla",
        },
    ],
    "files": {
        "tilde": {
            "AGENTS.md": "# tilde\n\nDesign assistant to Tom Reyes at Brightmoor Architects.\n",
            "plans/fenwick-rev-c.md": "# Fenwick extension, revision C\n\nKitchen-diner 31 m²\n"
            "Utility 11 m²\nTotal 42 m²\n",
        },
        "cobb": {
            "AGENTS.md": "# cobb\n\nCost planning at Brightmoor Architects. Ask tilde for areas.\n",
            "rates/2026.md": "# Rates 2026\n\nSingle-storey extension shell: £2,350 / m²\n",
        },
    },
    "threads": {
        "tilde": {
            "fenwick-revisions": [
                (300, "user", "Move the utility door to the side wall.", {"channel": "api"}),
                (
                    299,
                    "assistant",
                    "Done. Plan revision C has the door on the side wall.",
                    {"channel": "api"},
                ),
            ],
        },
    },
    "turns": {
        "cobb": [
            {
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "talk_to_agent",
                        "args": {"agent": "tilde", "message": AREA_Q},
                        "result": _ask_tilde,
                        "took": 2.6,
                        "peer": {"to": "tilde", "message": AREA_Q, "reply": AREA_A},
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "costs/fenwick-estimate.md"},
                        "write": {"costs/fenwick-estimate.md": ESTIMATE},
                        "result": "Wrote costs/fenwick-estimate.md",
                        "took": 1.2,
                    },
                    {"text": COST_REPLY, "wps": 16},
                ]
            }
        ],
        "tilde": [
            {"match": "floor area", "steps": [{"wait": 0.6}, {"text": AREA_A, "wps": 30}]},
            {"steps": [{"wait": 0.8}, {"text": _what_came_in, "wps": 15}]},
        ],
    },
}


def after_seed(runtime_url: str, **_: Any) -> None:
    """Turn the home multi-user the way a first run does, then add Tom and his seat."""
    import httpx

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "e2e"))
    from harness import first_run_setup_code

    with httpx.Client(base_url=runtime_url, timeout=30.0) as api:
        r = api.post("/api/auth/bootstrap", json={**ADMIN, "setup_code": first_run_setup_code()})
        r.raise_for_status()
        nadia_id = r.json()["user"]["id"]
        api.post(
            "/api/agents/orla/access", json={"user_id": nadia_id, "role": "owner"}
        ).raise_for_status()
        r = api.post("/api/admin/users", json={**TOM, "is_admin": False})
        r.raise_for_status()
        tom_id = r.json()["user"]["id"]
        api.post(
            "/api/agents/tilde/access", json={"user_id": tom_id, "role": "owner"}
        ).raise_for_status()
