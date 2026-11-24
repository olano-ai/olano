"""Brightside Lettings, a small property manager: the team in the schedule video."""

LEDGER_CSV = """\
unit,tenant,rent,due,paid
4 Maple Court,R. Okafor,1450,2026-11-01,1450
7 Maple Court,L. Brandt,1450,2026-11-01,0
12 Quay Street,S. Marchetti,1820,2026-11-01,920
2 Fern Lane,A. Lindqvist,1275,2026-11-01,1275
9 Fern Lane,T. Abara,1275,2026-11-01,0
"""

REPORT = (
    "Three tenants are behind on rent this morning.\n\n"
    "| Unit | Tenant | Owed | Days late |\n|---|---|---|---|\n"
    "| 7 Maple Court | L. Brandt | $1,450 | 23 |\n"
    "| 9 Fern Lane | T. Abara | $1,275 | 23 |\n"
    "| 12 Quay Street | S. Marchetti | $900 | 23 |\n\n"
    "**Total owed: $3,625.** L. Brandt has now missed two months in a row. "
    "I drafted a reminder for each tenant and left them in `drafts/` for you to check.\n\n"
    "Saved: reports/rent-arrears.md"
)

TEAM = {
    "default_agent": "piper",
    "agents": [
        {
            "name": "piper",
            "emoji": "🏠",
            "description": "Rent, repairs and tenant questions.",
            "title": "Property Assistant",
            "manager": None,
            "reports": ["otto"],
        },
        {
            "name": "otto",
            "emoji": "🔧",
            "description": "Books contractors and tracks repairs.",
            "title": "Maintenance Coordinator",
            "manager": "piper",
        },
    ],
    "files": {
        "piper": {
            "AGENTS.md": "# piper\n\nProperty assistant at Brightside Lettings. Reports go in reports/.\n",
            "data/rent-ledger.csv": LEDGER_CSV,
        },
    },
    "tasks": [
        {
            "name": "Repairs follow-up",
            "prompt": "Check every open repair and chase contractors who have not replied.",
            "schedule": "0 10 * * mon,wed,fri",
            "agent_name": "otto",
        },
        {
            "name": "Month-end statements",
            "prompt": "Prepare the owner statements for the month.",
            "schedule": "0 17 28 * *",
            "agent_name": "piper",
        },
        {
            "name": "Viewing reminders",
            "prompt": "Remind tomorrow's viewers of their appointment.",
            "schedule": "0 16 * * mon-fri",
            "agent_name": "piper",
        },
    ],
    "turns": {
        "piper": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "data/rent-ledger.csv"},
                        "result": LEDGER_CSV,
                        "took": 0.8,
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "reports/rent-arrears.md"},
                        "result": "Updated file reports/rent-arrears.md",
                        "took": 0.7,
                        "write": {
                            "reports/rent-arrears.md": "# Rent arrears\n\n"
                            + REPORT.split("Saved:")[0]
                        },
                    },
                    {"text": REPORT, "wps": 60},
                ]
            }
        ],
    },
}
