"""Willow Lane Vets, a small veterinary clinic: the team in the simple video.

The front desk gets the simple chat app instead of the full dashboard. The demo dashboard runs
single-user, where the interface is chosen by the ``ui_mode`` key of ``config.yaml`` (the same
switch an account's own setting overrides on a deployment with sign-in).
"""

from pathlib import Path

DIARY = """\
day,vet,free_slots
Thursday,Dr. Okafor,"10:20, 14:40, 16:10"
Friday,Dr. Okafor,"09:00, 11:30"
Friday,Dr. Lindqvist,"15:20"
"""

RECORD = """\
# Biscuit (Labrador, 4 years)

Owner: Hana Whitlock, hana.whitlock@mail.example
Booster due: this month. Last visit: 14 May, healthy, 31.2 kg.
"""

TEAM = {
    "default_agent": "poppy",
    "agents": [
        {
            "name": "poppy",
            "emoji": "🐾",
            "description": "Books appointments and answers the front desk's questions.",
            "title": "Front Desk",
            "manager": None,
            "reports": ["bram", "tess"],
        },
        {
            "name": "bram",
            "emoji": "💊",
            "description": "Keeps track of medicines, food and orders.",
            "title": "Pharmacy and Stock",
            "manager": "poppy",
        },
        {
            "name": "tess",
            "emoji": "🧾",
            "description": "Invoices, insurance claims and payment reminders.",
            "title": "Billing",
            "manager": "poppy",
        },
    ],
    "files": {
        "poppy": {
            "AGENTS.md": "# poppy\n\nFront desk at Willow Lane Vets. Check the diary before offering a time.\n",
            "diary/this-week.csv": DIARY,
            "patients/biscuit-whitlock.md": RECORD,
        },
        "bram": {"AGENTS.md": "# bram\n\nPharmacy and stock at Willow Lane Vets.\n"},
        "tess": {"AGENTS.md": "# tess\n\nBilling at Willow Lane Vets.\n"},
    },
    "threads": {
        "poppy": {
            "desk-monday": [
                (2900, "user", "Can Dr. Lindqvist see a rabbit on Friday?", {"channel": "api"}),
                (
                    2899,
                    "assistant",
                    "Yes. Dr. Lindqvist has 15:20 free on Friday and sees rabbits.",
                    {"channel": "api"},
                ),
            ],
        },
        "bram": {
            "stock-check": [
                (1500, "user", "Do we have the joint supplement for large dogs?", {"channel": "api"}),
                (
                    1499,
                    "assistant",
                    "Four tubs on the shelf. The next delivery is on Tuesday.",
                    {"channel": "api"},
                ),
            ],
        },
        "tess": {
            "claims": [
                (4300, "user", "Has the Okonjo claim been paid?", {"channel": "api"}),
                (4299, "assistant", "Yes, it was paid in full on Monday.", {"channel": "api"}),
            ],
        },
    },
    "turns": {
        "poppy": [
            {
                "match": "card",
                "steps": [
                    {"wait": 0.6},
                    {
                        "text": "Thanks. The card shows the last booster on **20 November 2025**, "
                        "so Biscuit is due this week.",
                        "wps": 16,
                    },
                ],
            },
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "patients/biscuit-whitlock.md"},
                        "result": "Biscuit (Labrador, 4 years). Booster due: this month.",
                        "took": 0.6,
                    },
                    {
                        "tool": "read_file",
                        "args": {"file_path": "diary/this-week.csv"},
                        "result": "Thursday, Dr. Okafor: 10:20, 14:40, 16:10",
                        "took": 0.6,
                    },
                    {
                        "text": "Biscuit's booster is due this month, so Thursday works well. "
                        "Dr. Okafor is free at **10:20**, **14:40** or **16:10**.\n\n"
                        "Shall I book one and send Hana a reminder?",
                        "wps": 16,
                    },
                ]
            }
        ],
        "milo": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "text": "Hello! Tell me which patients are going home today "
                        "and I will draft their aftercare notes.",
                        "wps": 16,
                    },
                ]
            }
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    """Give the (single) account the simple interface, the way the product stores it."""
    from olano.config.home import update_home_config

    update_home_config({"ui_mode": "simple"})
