"""Tidewater Cycles, a bike shop with a workshop: the team in the mobile app video.

The agents' Cortex items are written with the product's own ``cortex_items`` code, and the approval
in the Inbox is a real pending request made with ``security.hitl.request_approval`` (the call a
scheduled turn makes when it reaches a gated action).
"""

import asyncio
from pathlib import Path

JOBS = """\
# Workshop jobs, this week

| Job | Bike | Customer | Status |
|---|---|---|---|
| 4127 | Halden e-bike | Noor Castellan | Ready: new chain and brake pads fitted |
| 4130 | Road bike, full service | Idris Okafor | Waiting on a rear cassette |
| 4133 | Kids' bike, new tyres | Lena Brandt | In the stand today |
"""

EMAIL = {
    "to": "noor.castellan@riverbend.example",
    "subject": "Your Halden e-bike is ready",
    "body": "Hi Noor, your e-bike is ready to collect. We fitted a new chain and brake pads; "
    "the total is $186. We are open until 6 pm today.",
}

TEAM = {
    "default_agent": "milo",
    "agents": [
        {
            "name": "milo",
            "emoji": "🔧",
            "description": "Plans the workshop: repair jobs, parts and pickups.",
            "title": "Workshop Planner",
            "manager": None,
            "reports": ["wren", "otto"],
        },
        {
            "name": "wren",
            "emoji": "💬",
            "description": "Answers customers and sends pickup notices.",
            "title": "Customer Desk",
            "manager": "milo",
        },
        {
            "name": "otto",
            "emoji": "📦",
            "description": "Keeps stock, orders parts and tracks deliveries.",
            "title": "Stock and Orders",
            "manager": "milo",
        },
    ],
    "files": {
        "milo": {
            "AGENTS.md": "# milo\n\nWorkshop planner at Tidewater Cycles.\n",
            "workshop/jobs.md": JOBS,
        },
        "wren": {
            "AGENTS.md": "# wren\n\nCustomer desk at Tidewater Cycles. Ask before sending email.\n"
        },
        "otto": {"AGENTS.md": "# otto\n\nStock and orders at Tidewater Cycles.\n"},
    },
    # Earlier conversations: (minutes ago, role, content)
    "threads": {
        "wren": {
            "saturday-hours": [
                (
                    190,
                    "user",
                    "What did we tell customers about Saturday hours?",
                    {"channel": "dashboard"},
                ),
                (
                    189,
                    "assistant",
                    "Saturday is 9 to 4 through the end of the month. The notice is on the site and in the newsletter.",
                    {"channel": "dashboard"},
                ),
            ]
        },
        "otto": {
            "tyre-order": [
                (320, "user", "When do the 700c tyres arrive?", {"channel": "dashboard"}),
                (
                    319,
                    "assistant",
                    "Thursday. Forty tyres from Calder Supply, tracked and on schedule.",
                    {"channel": "dashboard"},
                ),
            ]
        },
    },
    "turns": {
        "milo": [
            {
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "workshop/jobs.md"},
                        "result": JOBS,
                        "took": 1.3,
                    },
                    {
                        "tool": "talk_to_agent",
                        "args": {"agent": "otto", "message": "Is the invoice for job 4127 closed?"},
                        "result": "Yes. Invoice 4127 is closed at $186.",
                        "took": 1.5,
                        "peer": {
                            "to": "otto",
                            "message": "Is the invoice for job 4127 closed?",
                            "reply": "Yes. Invoice 4127 is closed at $186.",
                        },
                    },
                    {
                        "text": "Yes. Noor's Halden e-bike is ready: job 4127, with a new chain and brake pads. "
                        "The invoice is closed at $186.\n\n"
                        "Wren has a pickup notice drafted and is waiting for your approval to send it.",
                        "wps": 13,
                    },
                ]
            }
        ],
    },
}


def after_seed(home: Path, dash: object = None, **_: object) -> None:
    """Cortex items for two agents, an alert, and one approval waiting in the Inbox."""
    from olano.cognition import cortex_items
    from olano.runtime import notifications
    from olano.security import hitl

    def item(
        agent: str, kind: str, title: str, summary: str, detail: str, score: int, why: str, rec: str
    ) -> None:
        workspace = home / "agents" / agent / "workspace"
        row = cortex_items.add_item(
            workspace, kind=kind, title=title, summary=summary, detail=detail
        )
        cortex_items.set_rating(workspace, row["id"], score, rationale=why, recommendation=rec)

    item(
        "milo",
        "idea",
        "Text customers when a repair is ready",
        "A pickup text the moment a job closes would clear the stand a day sooner.",
        "## Why now\n\nSeven finished bikes waited more than two days for pickup last week. "
        "A short text when the job closes gets most of them collected the same day.\n\n"
        "## First step\n\nSend the text for e-bike jobs only, for two weeks, and compare pickup times.",
        8,
        "Frees workshop space at almost no cost.",
        "promote",
    )
    item(
        "otto",
        "idea",
        "Reorder brake pads at ten sets",
        "We ran out twice in March; a reorder point of ten sets covers a busy week.",
        "## The numbers\n\nThe workshop uses six to eight sets a week. Delivery takes four days.",
        7,
        "Small change, prevents stalled jobs.",
        "promote",
    )
    item(
        "wren",
        "brainstorm",
        "A spring service reminder",
        "Customers who serviced a bike last spring could get a reminder this April.",
        "## Angles\n\n- A plain reminder with a booking link\n- A reminder with the last service date in it",
        6,
        "Worth exploring before the season starts.",
        "explore",
    )
    item(
        "milo",
        "prototype",
        "Workshop board for the shop screen",
        "A one-page board of today's jobs for the screen behind the counter.",
        "## What it shows\n\nEach job, its bike, and whether it is waiting, in the stand or ready.",
        7,
        "Useful once the status column is kept up to date.",
        "keep",
    )

    notifications.notify(
        kind="scheduled_run",
        level="warn",
        title="otto's supplier sign-in needs renewing",
        body="Calder Supply asked otto to sign in again. Reconnect it from the dashboard.",
        agent="otto",
    )

    # The approval a scheduled turn of wren's is waiting on: the request the runtime files when an
    # unattended turn reaches a gated action. Made on the runtime's own loop, where its future lives.
    loop = dash._loop_box[0]  # type: ignore[attr-defined]
    asyncio.run_coroutine_threadsafe(
        hitl.request_approval(
            "wren",
            "gmail_send",
            "Send the pickup notice to Noor Castellan",
            payload={
                "args": EMAIL,
                "origin": {"root_source": "scheduler", "call_path": ["scheduler"]},
            },
            risk="medium",
            timeout_seconds=6 * 3600,
        ),
        loop,
    )
