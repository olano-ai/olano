"""Fernway Garden Centre, a family plant nursery: the team in the cortex video.

Everything on the Cortex page is made with the product's own code: the funnel items with
``cortex_items.add_item`` and ``set_rating``, the Pulse card with ``PulseEngine.add``, the
promotion approval with the triage phase's ``queue_promotion_approval``. Approving it in the take
goes down the real path (job queue, drainer, ``run_promote``); the one stand-in is the background
worker, which writes the prototype's files from a script, as the chat agents answer from one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

SLOW = float(os.environ.get("DEMO_SLOW", "1"))

RESTOCK_IDEA = """\
## The idea

When a plant sells out, the shelf label keeps its QR code. A customer scans it, leaves a mobile number, and gets one text the day the plant is back on the bench.

## Why it fits us

- 41 people asked for the Japanese maples after they sold out in October. Nobody wrote the names down.
- The stock sheet already says what arrived each morning, so the trigger exists.
- One text per person per plant. No newsletter, no list to manage.

## What it needs

- A sign-up form behind the QR code
- A check of each morning's delivery sheet against the waiting list
- Lena's approval before the first text goes out
"""

PROTO_README = """\
# Back-in-stock texts

A waiting list and a morning check that lists who to text when a plant is back.

## How it works

1. `waitlist.csv` holds one row per request: plant, mobile number, date asked.
2. `check_restock.py` reads the morning delivery sheet and prints who to text.
3. Nothing is sent. The script only lists the messages, for Lena to approve.

## Run it

    python3 check_restock.py data/deliveries-sample.csv
"""

PROTO_SCRIPT = """\
import csv
import sys

arrived = {row["plant"] for row in csv.DictReader(open(sys.argv[1]))}
for row in csv.DictReader(open("waitlist.csv")):
    if row["plant"] in arrived:
        print(f"text {row['mobile']}: your {row['plant']} is back in stock")
"""

PROTO_WAITLIST = """\
plant,mobile,asked
Japanese maple 'Katsura',+1 555 0101,2026-10-14
Japanese maple 'Katsura',+1 555 0147,2026-10-15
Olive tree (standard),+1 555 0122,2026-10-20
Hellebore 'Winter Bells',+1 555 0163,2026-11-02
"""

PROTO_DELIVERIES = "plant,quantity\nJapanese maple 'Katsura',12\nHellebore 'Winter Bells',30\n"

PROTO_RESULTS = """\
# Results

Ran `python3 check_restock.py data/deliveries-sample.csv` on the sample sheet.

- 3 of the 4 requests on the waiting list matched a delivery.
- The olive tree request stays on the list: nothing arrived.
- Nothing was sent. Sending a text needs Lena's approval.
"""

PLAN = """\
# Plan

## Phase 1: the cards
- [x] Pick the 20 best-selling houseplants
- [x] Write a care card for each (light, water, one common mistake)
- [x] Print a test sheet of QR labels

## Phase 2: in the shop
- [ ] Put the labels on the houseplant benches
- [ ] Count scans for two weeks

## Phase 3: decide
- [ ] Extend to the outdoor plants if 1 in 10 buyers scans
"""

TEAM = {
    "default_agent": "sage",
    "agents": [
        {
            "name": "sage",
            "emoji": "🌿",
            "description": "Marketing, newsletters and new ideas for the shop.",
            "title": "Marketing Lead",
            "manager": None,
            "reports": ["bram"],
        },
        {
            "name": "bram",
            "emoji": "🪴",
            "description": "Stock, suppliers and the morning delivery sheet.",
            "title": "Stock Coordinator",
            "manager": "sage",
        },
    ],
    "files": {
        "sage": {
            "AGENTS.md": "# sage\n\nMarketing lead at Fernway Garden Centre. Lena Okafor owns the shop.\n",
            "CORTEX.md": "# Creative direction\n\n## Goals\n- Bring customers back between seasons.\n"
            "- Small things two people can run.\n\n## Avoid\n- Discounts and loyalty points.\n",
        },
        "bram": {"AGENTS.md": "# bram\n\nStock coordinator at Fernway Garden Centre.\n"},
    },
    "turns": {},
}


async def scripted_worker(*, workspace_root: Path, worker_label: str = "worker", **_: Any) -> Any:
    """The background worker of the take: builds the prototype's files step by step.

    It logs each step into the running Cortex job the way a real worker's callbacks do, so the
    run's panel shows them, then hands back a normal outcome; registering the prototype, linking
    it to the idea and marking the idea promoted is the product's own ``run_promote``.
    """
    from olano.cognition import jobs
    from olano.cognition.worker import WorkerOutcome

    folder = workspace_root / "prototypes" / "back-in-stock-texts"
    files = {
        "README.md": PROTO_README,
        "waitlist.csv": PROTO_WAITLIST,
        "data/deliveries-sample.csv": PROTO_DELIVERIES,
        "check_restock.py": PROTO_SCRIPT,
    }
    jobs.note({"file_path": "ideas/text-me-when-it-s-back-in-stock/idea.md"}, type="tool", tool="read_file")
    await asyncio.sleep(0.5 * SLOW)
    jobs.note(RESTOCK_IDEA[:300], type="tool_result", tool="read_file")
    for rel, body in files.items():
        jobs.note({"file_path": f"prototypes/back-in-stock-texts/{rel}"}, type="tool", tool="write_file")
        await asyncio.sleep(0.45 * SLOW)
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        jobs.note(f"Updated file prototypes/back-in-stock-texts/{rel}", type="tool_result", tool="write_file")
    jobs.note({"command": "python3 check_restock.py data/deliveries-sample.csv"}, type="tool", tool="safe_shell")
    await asyncio.sleep(0.6 * SLOW)
    ran = await asyncio.to_thread(
        subprocess.run,
        ["python3", "check_restock.py", "data/deliveries-sample.csv"],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=30,
    )
    printed = ran.stdout.strip()
    jobs.note(printed, type="tool_result", tool="safe_shell")
    (folder / "RESULTS.md").write_text(
        PROTO_RESULTS + "\n## Output\n\n" + "\n".join(f"    {line}" for line in printed.splitlines()) + "\n",
        encoding="utf-8",
    )
    await asyncio.sleep(0.3 * SLOW)
    made = [f"prototypes/back-in-stock-texts/{rel}" for rel in [*files, "RESULTS.md"]]
    summary = (
        "Built a small prototype: a waiting list and a morning check against the delivery sheet. "
        "On the sample data 3 of 4 requests matched. Nothing is sent without approval.\n"
        "ARTIFACTS: " + ", ".join(made)
    )
    jobs.note(summary, type="text")
    jobs.note(f"Worker {worker_label} finished: completed", type="worker")
    return WorkerOutcome(status="completed", summary=summary, artifacts=made, duration_seconds=3.0)


def after_seed(home: Path, gateway: Any, **_: object) -> None:
    from olano.cognition import cortex_items as ci
    from olano.cognition import ideation
    from olano.cognition.triage import queue_promotion_approval
    from olano.config.system_settings import set_auto_approve, set_cortex_master
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane, PulsePriority, PulseType
    from olano.runtime.server import RuntimeServer

    # Cortex on for the team, with promotions waiting for a person (Fully Autonomous off).
    set_cortex_master(True)
    set_auto_approve(False)

    sage = home / "agents" / "sage" / "workspace"
    bram = home / "agents" / "bram" / "workspace"

    def rate(ws: Path, item: dict, score: int, why: str, rec: str, nxt: str) -> dict:
        return ci.set_rating(ws, item["id"], score, rationale=why, recommendation=rec, next_step=nxt) or item

    # bram has ideas of his own
    s1 = ci.add_item(
        bram,
        kind="idea",
        title="Delivery calendar for the till staff",
        summary="A one-page view of what arrives this week, so staff can answer 'when is it in?'",
        detail="## The idea\n\nA weekly page made from the supplier confirmations, pinned by the tills.\n",
        tags=["stock"],
    )
    rate(bram, s1, 7, "Answers the most common question at the tills with data that already exists.", "keep", "Draft next week's page.")
    ci.add_item(
        bram,
        kind="brainstorm",
        title="Fewer part-empty delivery vans",
        summary="Could two small weekly orders become one? Which suppliers would allow it?",
        detail="## Angles\n\n- Combine the Tuesday and Thursday orders\n- Share a van with the farm shop\n",
        tags=["stock", "cost"],
    )

    # Brainstorms
    b1 = ci.add_item(
        sage,
        kind="brainstorm",
        title="Reasons to visit in winter",
        summary="Twelve angles for the quiet months: wreath evenings, a houseplant clinic, soup on Saturdays.",
        detail="## Angles\n\n1. Wreath-making evenings in December\n2. A houseplant clinic: bring a sick plant, leave with a plan\n"
        "3. Soup and seed catalogues on Saturdays\n4. A swap shelf for cuttings\n",
        tags=["winter", "footfall"],
    )
    rate(sage, b1, 6, "Several angles overlap; the houseplant clinic is the strongest.", "explore", "Cost the houseplant clinic for one Saturday.")
    b2 = ci.add_item(
        sage,
        kind="brainstorm",
        title="What to do with unsold seedlings",
        summary="Late-season trays go to compost. Could they go to schools, a rescue table or a bundle?",
        detail="## Angles\n\n- A rescue table by the tills\n- Trays for the primary school garden\n- A mixed 'surprise' bundle\n",
        tags=["waste"],
    )
    b3 = ci.add_item(
        sage,
        kind="brainstorm",
        title="Things customers ask that we never write down",
        summary="The same eight questions every week. Each one is a sign, a card or a newsletter piece.",
        detail="## Questions heard this week\n\n- Will it survive frost?\n- When is it back in stock?\n- Is it safe for cats?\n",
        tags=["customers"],
    )
    rate(sage, b3, 7, "Grounded in what the till staff hear; two angles already became ideas.", "keep", "Ask the till staff for a week of tallies.")

    # Ideas
    rescue = ci.add_item(
        sage,
        kind="idea",
        title="Seedling rescue table",
        summary="Late trays at a third of the price on one table by the tills, restocked on Fridays.",
        detail="## The idea\n\nOne table, one sign, restocked every Friday afternoon from the trays that would be composted.\n",
        tags=["waste"],
        links={"from_item": b2["id"], "from_path": b2["path"]},
    )
    ideation._mark_promoted(sage, b2, rescue)
    rate(sage, rescue, 7, "Cheap to try and it cuts waste. It leans on a price cut, which the direction says to avoid.", "keep", "Try it for two Fridays and count trays.")
    i3 = ci.add_item(
        sage,
        kind="idea",
        title="Rainy-day workshop series",
        summary="Short indoor sessions that only run when the forecast is bad: repotting, terrariums, seed sowing.",
        detail="## The idea\n\nA standing list of three workshops, announced the evening before a wet Saturday.\n",
        tags=["winter", "events"],
    )
    rate(sage, i3, 6, "Good fit for the quiet months, but it needs a second member of staff on the day.", "explore", "Ask who could run a terrarium session.")
    i4 = ci.add_item(
        sage,
        kind="idea",
        title="Loyalty points for repeat buyers",
        summary="A stamp card: every tenth plant free.",
        detail="## The idea\n\nA paper stamp card kept at the till.\n",
        tags=["sales"],
    )
    rate(sage, i4, 3, "The creative direction says to avoid discounts and loyalty points.", "discard", "")

    # A prototype that became a project
    proto = ci.add_item(
        sage,
        kind="prototype",
        title="Care cards with QR codes",
        summary="A printed label per plant that opens a one-page care card. Tested on five houseplants.",
        detail="# Care cards with QR codes\n\nA label on the pot opens a one-page care card.\n\n## How to run\n\n    python3 make_cards.py plants.csv\n",
        tags=["customers"],
    )
    (sage / proto["path"] / "RESULTS.md").write_text(
        "# Results\n\nFive cards made from plants.csv. 23 scans in the first week on the test bench.\n",
        encoding="utf-8",
    )
    project = ci.add_item(
        sage,
        kind="project",
        title="Care cards for the houseplant benches",
        summary="Roll the care cards out to the 20 best-selling houseplants and count scans for two weeks.",
        detail="# Care cards for the houseplant benches\n\nA QR label on every best-selling houseplant, each opening a one-page care card.\n",
        tags=["customers"],
        source="manual",
        links={"from_item": proto["id"], "from_path": proto["path"]},
    )
    folder = sage / project["path"]
    (folder / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (folder / "STATUS.md").write_text(
        "# Status\n\nPhase 1 done. The test sheet of labels is printed. Next: label the benches.\n", encoding="utf-8"
    )
    (folder / "JOURNAL.md").write_text(
        "# Journal\n\n- Wrote 20 care cards and checked them with Lena.\n- Printed a test sheet of QR labels.\n",
        encoding="utf-8",
    )
    ideation._mark_promoted(sage, proto, project)
    rate(sage, project, 8, "Phase 1 is finished and the next step needs no one's sign-off.", "execute", "Label the houseplant benches.")

    # The idea the take promotes: made last, so it tops the list.
    restock = ci.add_item(
        sage,
        kind="idea",
        title="Text me when it's back in stock",
        summary="A QR code on the empty shelf. One text when the plant returns. No newsletter, no list to manage.",
        detail=RESTOCK_IDEA,
        tags=["customers", "sales"],
        links={"from_item": b3["id"], "from_path": b3["path"]},
    )
    restock = rate(
        sage,
        restock,
        9,
        "41 people asked for sold-out maples in October and none were told when they came back. "
        "The delivery sheet already holds the trigger.",
        "promote",
        "Build the waiting list and the morning check on sample data.",
    )

    # Pulse: something sage noticed, and the promotion that waits for a person.
    cfg = gateway._runners["sage"].config
    engine = PulseEngine(cfg, sage / ".olano")
    engine.add(
        content="Scans of the test care cards doubled on Saturday (9 of the week's 23). "
        "The monstera card was opened most; the fern card was never opened.",
        source="cognition",
        type=PulseType.FINDING,
        priority=PulsePriority.NORMAL,
        lane=PulseLane.PULSE,
        title="Care card scans doubled on Saturday",
        why_now="The first full week of scan counts came in this morning.",
        suggested_next_step="Move the fern card to eye level before next weekend.",
        references=[f"{proto['path']}/RESULTS.md"],
        expires_in_hours=None,
    )
    queue_promotion_approval(cfg, sage, restock)

    # The approval path as on a running deployment: the drainer runs the queued promotion at
    # once. The worker it starts is the scripted one above.
    ideation.run_worker = scripted_worker  # type: ignore[assignment]

    def wake() -> None:
        asyncio.get_running_loop().create_task(RuntimeServer._drain_cognition_jobs(gateway))

    gateway.wake_cognition_drainer = wake
