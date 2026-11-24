"""Saltmarsh Cycles, a bike shop with a workshop: the team in the groups video.

A group turn is an ordinary turn of each member's runner, so the scripted agents answer here the
way they do in a 1:1 chat. The first member answers a message that names nobody; a reply that
names another member with ``@name`` hands the conversation on (the product's own cascade).
"""

DIARY = """\
day,slot,job
Thursday,09:00,Wheel build (Okoro)
Thursday,13:00,Full service (Adeyemi)
Friday,09:00,Brake bleed (Lindgren)
Friday,14:00,free
Saturday,10:00,Collection only
"""

STOCK = """\
part,on_shelf,price
Disc brake pads (sintered),2,24.00
Disc brake pads (organic),5,18.00
Chain 10-speed,7,32.00
"""

TEAM = {
    "default_agent": "mara",
    "agents": [
        {
            "name": "mara",
            "emoji": "🚲",
            "description": "Talks to customers and books jobs in.",
            "title": "Shop Lead",
            "manager": None,
            "reports": ["otto", "fern"],
        },
        {
            "name": "otto",
            "emoji": "🔧",
            "description": "Plans the workshop diary and estimates repairs.",
            "title": "Workshop",
            "manager": "mara",
        },
        {
            "name": "fern",
            "emoji": "📦",
            "description": "Keeps parts in stock and places supplier orders.",
            "title": "Parts and Stock",
            "manager": "mara",
        },
    ],
    "files": {
        "mara": {
            "AGENTS.md": "# mara\n\nShop lead at Saltmarsh Cycles. Ask otto about workshop time and fern about parts.\n",
            "customers/lena-voss.md": "# Lena Voss\n\nCargo e-bike, bought March 2025. Last service: November 2025.\n"
            "Contact: lena.voss@mail.example\n",
        },
        "otto": {
            "AGENTS.md": "# otto\n\nWorkshop planner at Saltmarsh Cycles.\n",
            "workshop/diary.csv": DIARY,
        },
        "fern": {
            "AGENTS.md": "# fern\n\nParts and stock at Saltmarsh Cycles.\n",
            "stock/parts.csv": STOCK,
        },
    },
    "threads": {
        "mara": {
            "weekend-hours": [
                (1700, "user", "What are the workshop hours on Saturday?", {"channel": "api"}),
                (
                    1699,
                    "assistant",
                    "Saturday is collection only, from 10:00 to 13:00.",
                    {"channel": "api"},
                ),
            ],
        },
    },
    "turns": {
        "mara": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "customers/lena-voss.md"},
                        "result": "Cargo e-bike, bought March 2025. Last service: November 2025.",
                        "took": 0.9,
                    },
                    {
                        "text": "Lena's cargo e-bike is due: its last service was a year ago. "
                        "A full service with new pads takes about two hours.\n\n"
                        "@otto, is there a workshop slot before Saturday?",
                        "wps": 17,
                    },
                ]
            }
        ],
        "otto": [
            {
                "steps": [
                    {"wait": 1.6},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "workshop/diary.csv"},
                        "result": DIARY,
                        "took": 1.4,
                    },
                    {
                        "text": "Yes. **Friday at 14:00** is free, and two hours is enough, "
                        "so the bike is ready for Saturday's collection.\n\n"
                        "@fern, do we have sintered disc pads on the shelf?",
                        "wps": 40,
                    },
                ]
            }
        ],
        "fern": [
            {
                "match": "how many sets",
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "stock/parts.csv"},
                        "result": STOCK,
                        "took": 0.8,
                    },
                    {
                        "text": "One sintered set and five organic sets. "
                        "I will add two sintered sets to Monday's order list.",
                        "wps": 17,
                    },
                ],
            },
            {
                "steps": [
                    {"wait": 1.6},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "stock/parts.csv"},
                        "result": STOCK,
                        "took": 1.4,
                    },
                    {
                        "text": "Two sets on the shelf, **$24** a set. "
                        "I have put one aside for Friday's job.",
                        "wps": 40,
                    },
                ],
            },
        ],
    },
}
