"""Nightjar Records, an independent record shop with a web store: the team in the themes video.

Five agents so the org chart and the Overview have something to show, a month of usage for the
Overview's numbers, a few scheduled tasks and earlier conversations, and one scripted turn that
answers with a chart made by the product's own ``charts`` skill (``assets/themes-genres.png``,
rendered with ``node chart.mjs assets/themes-genres.json assets/themes-genres.png 1200x640 clean``).
"""

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"

SALES_CSV = """\
genre,shop,online,total,october_total
Jazz,251,161,412,298
Soul,204,152,356,331
Rock,189,129,318,322
Electronic,98,143,241,236
Hip-hop,77,120,197,205
Folk,96,47,143,151
"""

TEAM = {
    "default_agent": "lyra",
    "agents": [
        {
            "name": "otis",
            "emoji": "🎚️",
            "description": "Runs the shop day to day and hands work to the others.",
            "title": "Shop Manager",
            "reports": ["lyra", "fern", "ivy", "jasper"],
        },
        {
            "name": "lyra",
            "emoji": "📊",
            "description": "Sales figures, trends and the monthly report.",
            "title": "Sales Analyst",
            "manager": "otis",
        },
        {
            "name": "fern",
            "emoji": "💬",
            "description": "Answers customers about orders, pre-orders and returns.",
            "title": "Customer Care",
            "manager": "otis",
        },
        {
            "name": "ivy",
            "emoji": "📦",
            "description": "Stock levels, reorders and the distributors.",
            "title": "Buyer",
            "manager": "otis",
        },
        {
            "name": "jasper",
            "emoji": "🎙️",
            "description": "New-arrivals posts, the newsletter and in-store events.",
            "title": "Social and Events",
            "manager": "otis",
        },
    ],
    "files": {
        "lyra": {
            "AGENTS.md": "# lyra\n\nSales analyst for Nightjar Records. Save reports in reports/.\n",
            "data/november-sales.csv": SALES_CSV,
        },
        "otis": {"AGENTS.md": "# otis\n\nShop manager at Nightjar Records.\n"},
        "fern": {"AGENTS.md": "# fern\n\nCustomer care at Nightjar Records.\n"},
        "ivy": {"AGENTS.md": "# ivy\n\nBuyer at Nightjar Records.\n"},
        "jasper": {"AGENTS.md": "# jasper\n\nSocial and events at Nightjar Records.\n"},
    },
    "tasks": [
        {
            "name": "Morning order check",
            "prompt": "List new web orders and anything waiting to ship.",
            "schedule": "0 9 * * mon-sat",
            "agent_name": "fern",
        },
        {
            "name": "Weekly sales summary",
            "prompt": "Summarise the week's sales by genre, shop and online.",
            "schedule": "30 8 * * mon",
            "agent_name": "lyra",
        },
        {
            "name": "Low-stock reorder list",
            "prompt": "List every title with fewer than three copies left.",
            "schedule": "0 10 * * tue,fri",
            "agent_name": "ivy",
        },
        {
            "name": "New arrivals post",
            "prompt": "Draft this week's new-arrivals post from the delivery notes.",
            "schedule": "0 15 * * thu",
            "agent_name": "jasper",
        },
    ],
    "issues": [
        {
            "title": "Reorder the Blue Note reissues",
            "body": "Two copies left of the November reissues; the distributor needs a week.",
            "assignee": "ai-ivy",
            "labels": ["stock"],
            "type": "task",
        },
        {
            "title": "Poster for the Saturday listening night",
            "body": "Jazz session on 6 December, 7 pm.",
            "assignee": "ai-jasper",
            "labels": ["events"],
            "type": "task",
        },
    ],
    "threads": {
        "lyra": {
            "october-report": [
                (4400, "user", "How did October go overall?", {"channel": "api"}),
                (
                    4399,
                    "assistant",
                    "October was steady: 1,543 records sold, close to September.",
                    {"channel": "api"},
                ),
            ],
        },
        "fern": {
            "preorders": [
                (900, "user", "How many pre-orders for the Record Store Day release?", {"channel": "api"}),
                (899, "assistant", "Twenty-three so far, nine of them for shop pickup.", {"channel": "api"}),
            ],
        },
        "ivy": {
            "distributors": [
                (1600, "user", "When does the next delivery arrive?", {"channel": "api"}),
                (1599, "assistant", "Thursday morning, two boxes from the jazz distributor.", {"channel": "api"}),
            ],
        },
    },
    "a2a": [
        {
            "from": "otis",
            "to": "ivy",
            "message": "Which titles should we reorder before December?",
            "reply": "The Blue Note reissues first: two copies left, a week to restock.",
        },
        {
            "from": "otis",
            "to": "jasper",
            "message": "Please announce the Saturday listening night.",
            "reply": "Posted, and added to Friday's newsletter.",
        },
    ],
    "turns": {
        "lyra": [
            {
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "data/november-sales.csv"},
                        "result": SALES_CSV,
                        "took": 0.8,
                    },
                    {
                        "tool": "render_html_image",
                        "args": {
                            "html": "reports/november-genres.html",
                            "save_path": "reports/november-genres.png",
                        },
                        "result": "Saved reports/november-genres.png (1200x640)",
                        "took": 1.0,
                        "write": {
                            "reports/november-genres.png": (ASSETS / "themes-genres.png").read_bytes()
                        },
                    },
                    {
                        "text": "**Jazz** led November with **412** records, up 38% on October. "
                        "Soul and rock follow.\n\n"
                        "| Genre | Shop | Online | Total |\n|---|---|---|---|\n"
                        "| Jazz | 251 | 161 | 412 |\n| Soul | 204 | 152 | 356 |\n"
                        "| Rock | 189 | 129 | 318 |\n\n"
                        "Chart: reports/november-genres.png",
                        "wps": 18,
                    },
                ]
            }
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    """A month of usage, so the Overview has real numbers to draw."""
    from olano.analytics.usage_ledger import record_usage_event

    rng = random.Random(23)
    now = datetime.now(timezone.utc)
    mix = {
        "lyra": ("olano:pro", 12, 0.05),
        "fern": ("olano:turbo", 30, 0.012),
        "ivy": ("olano:turbo", 10, 0.015),
        "jasper": ("olano:pro", 8, 0.04),
        "otis": ("olano:pro", 6, 0.06),
    }
    for back in range(30, -1, -1):
        day = now - timedelta(days=back)
        for agent, (model, per_day, usd) in mix.items():
            for _ in range(int(per_day * rng.uniform(0.6, 1.3))):
                ts = day.replace(
                    hour=rng.randint(8, 19), minute=rng.randint(0, 59), second=rng.randint(0, 59)
                )
                if ts > now:
                    continue
                tin = rng.randint(5000, 20000)
                record_usage_event(
                    agent,
                    model,
                    tin,
                    rng.randint(200, 1200),
                    usd * rng.uniform(0.5, 1.6),
                    cache_read_tokens=int(tin * 0.6),
                    calls=rng.randint(1, 4),
                    ts=ts.isoformat(),
                    origin=rng.choice(["chat", "chat", "scheduled"]),
                    thread_id=f"t{rng.randint(1, 30)}",
                )
