"""Juniper Lane Veterinary Clinic, a two-vet practice: the team in the memory video.

MEMORY.md, the daily logs and the notes are saved through the dashboard's own API. The knowledge
base is real: the clinic's handbook pages are ingested with the product's ``kb_add_file`` tool and
the Knowledge page's searches are answered by the real ``kb_search`` / ``kb_list`` tools. Willa's
answer in the new chat is put together from what MEMORY.md and today's log hold at that moment, so
the fact added on the Memory page earlier in the take is in it only because it was saved.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KB_TOOLS = [
    "kb_search",
    "kb_list",
    "kb_add_file",
    "kb_add_url",
    "kb_create_store",
    "kb_sync",
    "kb_delete",
    "kb_remove_file",
]

MEMORY = """\
# MEMORY.md

## Clinic
- Open Monday to Saturday, 8:00 to 18:00. Surgery mornings are Tuesday and Thursday.
- Dr. Imani Castellan sees cats and dogs. Dr. Per Halvorsen also sees rabbits and birds.
- New clients fill in the intake form before the first visit.

## Preferences
- Dr. Castellan wants the day's appointment list by 7:30, shortest notes possible.
- Reminders to clients go out two days before a visit, never on a Sunday.
- Always give prices as a range until a vet has seen the animal.

## Clients
- Mrs. Alvarez (Biscuit, beagle, 6): Biscuit is allergic to chicken. No chicken-based treats.
- Mr. Nakamura (Miso, tabby, 11): on a kidney diet, weigh at every visit.
- The Brandt family (Pepper, rabbit, 2): prefers Dr. Halvorsen.
"""

VACCINES = """\
# Vaccination schedule

## Puppies
- First vaccination at 8 weeks.
- Second vaccination at 12 weeks.
- Rabies at 16 weeks, then a booster one year later.
- Puppies can meet other dogs one week after the second vaccination.

## Kittens
- First vaccination at 9 weeks, second at 12 weeks.
- Indoor cats still need the yearly booster.

## Adult dogs and cats
- A booster every year. We send a reminder two days before the appointment.
"""

SURGERY = """\
# Before and after surgery

## The night before
- No food after 20:00. Water is fine until the morning.
- Bring the animal in between 8:00 and 8:30 on the day.

## Going home
- Most animals go home the same afternoon, after 15:00.
- Keep the wound dry for ten days. No baths, no swimming.
- A check-up after three days is included in the price.
"""

PRICES = """\
# Price guide

Prices are a guide. A vet confirms the price after seeing the animal.

- Consultation: $55 to $70
- Vaccination, including a health check: $60 to $85
- Dental clean under anaesthetic: $280 to $420
- Neutering a cat: $120 to $160. Neutering a dog: $220 to $380, by weight.
"""

NOTES = [
    {
        "title": "Reminder texts",
        "content": "# Reminder texts\n\nWording the clinic has agreed on. See [[Front desk checklist]].\n\n"
        "## Vaccination\n> Hello, this is Juniper Lane Vets. {pet} is due a vaccination on {date}. "
        "Reply YES to confirm.\n\n## After surgery\n> How is {pet} today? A check-up is included: "
        "reply to book it.\n",
        "tags": ["clients", "templates"],
    },
    {
        "title": "Surgery mornings",
        "content": "# Surgery mornings\n\nTuesday and Thursday, 8:30 to 12:30.\n\n"
        "- Admit between 8:00 and 8:30.\n- Call owners by 13:00 with how it went.\n"
        "- Discharge after 15:00, with the aftercare sheet.\n\nLinked from [[Front desk checklist]].\n",
        "tags": ["surgery"],
    },
    {
        "title": "Front desk checklist",
        "content": "# Front desk checklist\n\nWhat Willa does every morning.\n\n"
        "1. Send Dr. Castellan the appointment list by 7:30.\n"
        "2. Check tomorrow's visits against [[Reminder texts]].\n"
        "3. On surgery days, follow [[Surgery mornings]].\n"
        "4. Note anything new about a client in memory.\n",
        "tags": ["daily", "front-desk"],
    },
]


def _kb(ctx: dict[str, Any]) -> str:
    """Answer the Knowledge page's tool-call message with the real tool's own output."""
    from olano.tools import knowledge_tools as kt

    message = ctx["message"]
    found = re.search(r"kb_search\(\"((?:[^\"\\]|\\.)*)\"(?:, k=(\d+))?(?:, collection=\"([^\"]*)\")?(?:, mode=\"([^\"]*)\")?", message)
    if found:
        query = found.group(1).replace('\\"', '"').replace("\\\\", "\\")
        return str(
            kt.kb_search.invoke(
                {
                    "query": query,
                    "k": int(found.group(2) or 5),
                    "collection": found.group(3) or "all",
                    "mode": found.group(4) or "similarity",
                }
            )
        )
    found = re.search(r"kb_list\(\"([^\"]*)\"\)", message)
    if found:
        return str(kt.kb_list.invoke({"collection": found.group(1) or "all"}))
    return "Error: this demo agent serves kb_search and kb_list only."


def _recall(ctx: dict[str, Any]) -> str:
    """Willa's answer about Biscuit, from what her memory files hold right now."""
    import os

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []
    for path in (workspace / "MEMORY.md", workspace / "memories" / f"{today}.md"):
        if path.is_file():
            lines += path.read_text(encoding="utf-8").splitlines()
    facts: list[str] = []
    for line in lines:
        text = re.sub(r"^[-*]\s*(\[[^\]]*\]\s*)?", "", line.strip())
        text = re.sub(r"^Mrs\. Alvarez \(Biscuit, beagle, 6\):\s*", "", text)
        if "biscuit" in line.lower() and text and text not in facts:
            facts.append(text)
    listed = "\n".join(f"- {fact}" for fact in facts) or "- Nothing on file yet."
    return (
        "Yes. Biscuit is Mrs. Alvarez's beagle, six years old. From what I have on file:\n\n"
        f"{listed}\n\n"
        "I will put both on tomorrow's list for Dr. Castellan, and keep chicken treats off the counter."
    )


TEAM = {
    "default_agent": "willa",
    "agents": [
        {
            "name": "willa",
            "emoji": "🐾",
            "description": "Appointments, reminders and client questions.",
            "title": "Front Desk",
            "manager": None,
            "reports": ["reed"],
        },
        {
            "name": "reed",
            "emoji": "📦",
            "description": "Orders medicines and tracks stock.",
            "title": "Supplies",
            "manager": "willa",
        },
    ],
    "files": {
        "willa": {
            "AGENTS.md": "# willa\n\nFront desk at Juniper Lane Veterinary Clinic.\n",
            "knowledgebase/vaccination-schedule.md": VACCINES,
            "knowledgebase/surgery-aftercare.md": SURGERY,
            "knowledgebase/price-guide.md": PRICES,
        },
        "reed": {"AGENTS.md": "# reed\n\nSupplies at Juniper Lane Veterinary Clinic.\n"},
    },
    "memory": {"willa": MEMORY},
    "notes": {"willa": NOTES},
    "turns": {
        "willa": [
            {"match": "kb_", "steps": [{"text": _kb, "wps": 4000}]},
            {
                "steps": [
                    {"wait": 0.7},
                    {
                        "tool": "memory_search",
                        "args": {"query": "Biscuit"},
                        "result": lambda ctx: _recall(ctx).split("\n\n")[1],
                        "took": 1.0,
                    },
                    {"text": _recall, "wps": 20},
                ]
            },
        ],
    },
}


def after_seed(home: Path, runtime_url: str, gateway: Any, **_: object) -> None:
    import json

    import httpx

    from olano.memory.rag_memory import RAGMemory
    from olano.tools import knowledge_tools as kt

    workspace = home / "agents" / "willa" / "workspace"

    # The knowledge-base tools are not granted by default: name them, as the Knowledge page asks.
    cfg_path = home / "agents" / "willa" / "agent.json5"
    entry = json.loads(cfg_path.read_text(encoding="utf-8"))
    entry["tools"] = KB_TOOLS
    cfg_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
    gateway._runners["willa"].config.tools = list(KB_TOOLS)

    # A real index of the handbook pages, built by the product's own tool.
    kt.set_rag(RAGMemory(persist_dir=workspace / "rag", collection_name="knowledgebase"))
    for page in sorted((workspace / "knowledgebase").glob("*.md")):
        kt.kb_add_file.invoke({"path": str(page)})

    # Daily logs: yesterday's as a file, today's through the Memory page's own endpoint.
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    (workspace / "memories").mkdir(exist_ok=True)
    (workspace / "memories" / f"{yesterday}.md").write_text(
        f"# {yesterday}\n\n"
        "- Mr. Nakamura moved Miso's check-up to Friday at 10:30.\n"
        "- Two clients asked about puppy classes. We do not run any; pointed them to the town hall list.\n"
        "- The dental scaler is back from repair.\n",
        encoding="utf-8",
    )
    with httpx.Client(base_url=runtime_url, timeout=30.0) as api:
        for note in (
            "Dr. Halvorsen is away next Monday. Rabbit and bird appointments move to Wednesday.",
            "The Brandt family asked for a text, not a call, when Pepper is ready to go home.",
        ):
            api.post("/api/memory", params={"agent": "willa"}, json={"content": note}).raise_for_status()
