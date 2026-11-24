"""Pinecrest Veterinary Clinic: the team in the external access (MCP server) video."""

DIARY = """\
# Appointment diary, this week

| Day | Vet | Free slots |
|---|---|---|
| Thursday | Dr. Abara | none |
| Friday | Dr. Abara | 10:20, 15:40 |
| Friday | Dr. Lindqvist | 11:00 |
| Saturday | Dr. Lindqvist | 09:30 |

A vaccination visit takes 20 minutes. Cats and dogs are seen in separate rooms.
"""

ANSWER = (
    "Yes. Friday has three free slots: 10:20 and 15:40 with Dr. Abara, and 11:00 with "
    "Dr. Lindqvist. A vaccination visit takes 20 minutes."
)

TEAM = {
    "default_agent": "juno",
    "agents": [
        {
            "name": "juno",
            "emoji": "🐾",
            "description": "Front desk: opening hours, free slots and visit prices.",
            "title": "Front Desk",
            "manager": "rhea",
        },
        {
            "name": "rhea",
            "emoji": "🩺",
            "description": "Runs the clinic's day and the vets' rota.",
            "title": "Practice Manager",
            "manager": None,
            "reports": ["juno", "tavi"],
        },
        {
            "name": "tavi",
            "emoji": "💊",
            "description": "Orders medicines and keeps the pharmacy stocked.",
            "title": "Pharmacy and Stock",
            "manager": "rhea",
        },
    ],
    "files": {
        "juno": {
            "AGENTS.md": "# juno\n\nFront desk at Pinecrest Veterinary Clinic.\n",
            "diary/this-week.md": DIARY,
        },
        "rhea": {"AGENTS.md": "# rhea\n\nPractice manager at Pinecrest Veterinary Clinic.\n"},
        "tavi": {"AGENTS.md": "# tavi\n\nPharmacy and stock at Pinecrest Veterinary Clinic.\n"},
    },
    "a2a": [
        {
            "from": "juno",
            "to": "rhea",
            "message": "Can Dr. Lindqvist take a Saturday morning slot this week?",
            "reply": "Yes. Saturday 09:30 is open; add it to the diary.",
        },
    ],
    "turns": {
        "juno": [
            {
                "steps": [
                    {"wait": 0.5},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "diary/this-week.md"},
                        "result": DIARY,
                        "took": 1.2,
                    },
                    {"text": ANSWER, "wps": 14},
                ]
            }
        ],
    },
}


def after_seed(dash: object = None, gateway: object = None, **_: object) -> None:
    """Two things a real box has from its boot and the launcher does not.

    The deployment's public address: a box takes it from LIONCLAW_PUBLIC_URL or from the address a
    browser reached the dashboard on; here it is the throwaway dashboard's own. And the activity
    publisher: the runtime binds it at start (``runtime/server.py``: ``talk_to.bind_runtime(self,
    publisher=...)``), which is how an inbound call reaches the Agent to Agent view and its log.
    The same call is made here; the call, its record and the view are the product's own.
    """
    import os

    from olano.runtime.api import _push_event
    from olano.tools import talk_to

    os.environ["LIONCLAW_PUBLIC_URL"] = dash.base_url  # type: ignore[attr-defined]
    talk_to.bind_runtime(gateway, publisher=_push_event)
