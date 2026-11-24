"""Casa Lumen, a small hotel: the team in the create video. Maya is made during the take."""

TEAM = {
    "default_agent": "iris",
    "agents": [
        {
            "name": "iris",
            "emoji": "🛎️",
            "description": "Takes bookings and answers availability questions.",
            "title": "Reservations",
            "manager": None,
            "reports": ["sol"],
        },
        {
            "name": "sol",
            "emoji": "🧹",
            "description": "Plans room cleaning and tracks maintenance.",
            "title": "Housekeeping Planner",
            "manager": "iris",
        },
    ],
    "files": {"iris": {"AGENTS.md": "# iris\n\nReservations at Casa Lumen.\n"}},
    "turns": {
        "maya": [
            {
                "steps": [
                    {"wait": 0.8},
                    {
                        "text": "Good morning! Breakfast is served from **7:00 to 10:30** in the courtyard, "
                        "and until 11:00 on weekends.\n\n"
                        "Would you like me to book a table, or have it brought to your room?",
                        "wps": 13,
                    },
                ]
            }
        ],
    },
}
