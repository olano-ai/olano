#!/usr/bin/env python3
"""Capture the dashboard screenshots the user guide shows.

    uv run python scripts/capture_docs_screenshots.py            # every shot
    uv run python scripts/capture_docs_screenshots.py chat files # some shots
    uv run python scripts/capture_docs_screenshots.py --list     # the names

Boots a throwaway dashboard through the same launcher the browser tests use
(``tests/e2e/harness.py``: the real runtime API over a mock gateway, the real
React app from source, a temporary home), fills it with a small fictional
team, and photographs each tab into ``docs/user-guide/assets/screenshots/``.
Nothing here reads the machine's own home, keys or agents, and no model is
called: the four demo agents answer from fixed text.

The images are WebP, 1600 px wide, taken at twice the pixel density of a
1280 x 800 window so text stays sharp when the reader zooms in. Run the script
again after a visible dashboard change and commit what changed; the pages
reference the images by name, and ``tests/test_user_guide_assets.py`` fails
when a page points at an image that does not exist or an image nothing uses.

Needs the dashboard's npm tree (``cd olano/dashboard_2.0 && npm install``) and
Playwright's Chromium, exactly like ``make test-e2e``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "user-guide" / "assets" / "screenshots"
E2E_DIR = ROOT / "tests" / "e2e"


def _use_e2e_harness() -> None:
    """Make the browser tests' launcher importable (``import harness``)."""
    if str(E2E_DIR) not in sys.path:
        sys.path.insert(0, str(E2E_DIR))


VIEWPORT = {"width": 1280, "height": 800}
SCALE = 2
OUT_WIDTH = 1600
WEBP_QUALITY = 82

# ── the demo team ────────────────────────────────────────────────────────────

# The dashboard opens on the first agent, so nova (whose memory, notes and
# chat the shots show) comes first.
AGENTS: list[dict[str, Any]] = [
    {
        "name": "nova",
        "emoji": "🔭",
        "description": "Researches suppliers, markets and competitors; writes reports.",
        "model": "olano:pro",
        "title": "Research Analyst",
        "manager": "atlas",
        "reports": [],
    },
    {
        "name": "atlas",
        "emoji": "🧭",
        "description": "Runs day-to-day operations and hands work to the team.",
        "model": "olano:pro",
        "title": "Head of Operations",
        "manager": None,
        "reports": ["aria", "nova", "quill"],
    },
    {
        "name": "aria",
        "emoji": "💬",
        "description": "Answers customers on WhatsApp and email, escalates refunds.",
        "model": "olano:turbo",
        "title": "Customer Support",
        "manager": "atlas",
        "reports": [],
    },
    {
        "name": "quill",
        "emoji": "✍️",
        "description": "Drafts the newsletter, blog posts and product copy.",
        "model": "olano:turbo",
        "title": "Content Writer",
        "manager": "atlas",
        "reports": [],
    },
]

SUPPLIER_REPORT = """\
# Coffee supplier comparison

Prepared by nova for the operations team.

| Supplier | Price per kg | Delivery | Minimum order | Notes |
|---|---|---|---|---|
| Highland Roasters | $18.40 | 2 days | 20 kg | Best price, single origin only |
| Coastal Beans Co. | $21.10 | Next day | 10 kg | Fastest, flexible minimum |
| Valley Green Farms | $19.75 | 4 days | 40 kg | Organic certified |

## Recommendation

Move the house blend to **Highland Roasters** and keep **Coastal Beans Co.**
for urgent top-ups. Savings at current volume: about $310 a month.
"""

SUPPLIERS_CSV = """\
supplier,price_per_kg,delivery_days,minimum_kg,organic
Highland Roasters,18.40,2,20,no
Coastal Beans Co.,21.10,1,10,no
Valley Green Farms,19.75,4,40,yes
"""

MARKET_BRIEF = """\
# Weekly market brief

- Green coffee futures are down 4% on the week.
- Two competitors opened in the harbour district; both price a flat white at $5.20.
- Oat milk demand keeps rising: 38% of milk-based orders last month.
"""

NOVA_MEMORY = """\
# MEMORY.md

## Preferences
- The owner wants prices in USD and monthly totals, not per-order costs.
- Reports go in `reports/`, one file per topic, with a recommendation at the end.

## Suppliers
- Highland Roasters is the current house-blend supplier (since August).
- Coastal Beans Co. delivers next day and is the backup for urgent orders.

## Team
- atlas approves any supplier change before it is announced.
- quill turns finished reports into the newsletter on Fridays.
"""

WORKSPACE_FILES: dict[str, dict[str, str]] = {
    "nova": {
        "AGENTS.md": "# nova\n\nResearch analyst for the team. Cite sources; save reports in reports/.\n",
        "SOUL.md": "# Soul\n\nCareful, numerate, brief. Shows the table before the prose.\n",
        "IDENTITY.md": "# Identity\n\nnova, Research Analyst. Reports to atlas.\n",
        "USER.md": "# User\n\nThe owner of a small coffee roastery with two shops.\n",
        "reports/supplier-comparison.md": SUPPLIER_REPORT,
        "reports/weekly-market-brief.md": MARKET_BRIEF,
        "data/suppliers.csv": SUPPLIERS_CSV,
    },
    "aria": {
        "AGENTS.md": "# aria\n\nCustomer support. Refunds over $50 go to atlas for approval.\n",
        "SOUL.md": "# Soul\n\nWarm, quick, never promises what the shop cannot do.\n",
    },
    "quill": {
        "AGENTS.md": "# quill\n\nContent writer. Newsletter every Friday.\n",
        "drafts/newsletter-week-41.md": "# This week at the roastery\n\nA new single origin from the highlands...\n",
    },
    "atlas": {
        "AGENTS.md": "# atlas\n\nHead of operations. Delegates to aria, nova and quill.\n",
    },
}

NOTES: dict[str, list[dict[str, Any]]] = {
    "nova": [
        {
            "title": "Supplier scoring method",
            "content": (
                "Score each supplier on **price** (40%), **delivery** (30%), "
                "**minimum order** (20%) and **certifications** (10%).\n\n"
                "Related: [[Highland Roasters]], [[Coastal Beans Co.]]"
            ),
            "tags": ["method", "suppliers"],
        },
        {
            "title": "Highland Roasters",
            "content": "House-blend supplier since August. Contact: orders desk, weekdays.",
            "tags": ["suppliers"],
        },
        {
            "title": "Coastal Beans Co.",
            "content": "Next-day delivery, 10 kg minimum. Backup for urgent orders.",
            "tags": ["suppliers"],
        },
    ]
}

TASKS: list[dict[str, str]] = [
    {
        "name": "Morning inbox triage",
        "prompt": "Read new customer messages, answer the simple ones, list the rest for me.",
        "schedule": "0 8 * * 1-5",
        "agent_name": "aria",
    },
    {
        "name": "Weekly market brief",
        "prompt": "Write the weekly market brief into reports/weekly-market-brief.md.",
        "schedule": "0 9 * * 1",
        "agent_name": "nova",
    },
    {
        "name": "Friday newsletter draft",
        "prompt": "Turn this week's reports into the newsletter draft.",
        "schedule": "0 14 * * 5",
        "agent_name": "quill",
    },
    {
        "name": "Month-end stock check",
        "prompt": "Compare stock levels with last month and flag anything below two weeks.",
        "schedule": "0 18 28-31 * *",
        "agent_name": "atlas",
    },
]

ISSUES: list[dict[str, Any]] = [
    {
        "title": "Switch the house blend to Highland Roasters",
        "body": "nova's comparison shows about $310/month in savings. Needs sign-off.",
        "assignee": "ai-atlas",
        "labels": ["suppliers", "decision"],
        "type": "task",
        "comments": ["Report is in reports/supplier-comparison.md."],
    },
    {
        "title": "Customer asked for an oat-milk loyalty card",
        "body": "Three requests this week on WhatsApp. Worth a small promotion?",
        "assignee": "ai-quill",
        "labels": ["marketing"],
        "type": "feature",
        "comments": [],
    },
    {
        "title": "Refund for order #1042 (damaged bag)",
        "body": "Customer sent a photo. $64, above aria's limit.",
        "assignee": "ai-atlas",
        "labels": ["refund"],
        "type": "bug",
        "comments": [],
    },
    {
        "title": "Research competitor pricing in the harbour district",
        "body": "Two new shops opened. Collect their menu prices.",
        "assignee": "ai-nova",
        "labels": ["research"],
        "type": "task",
        "comments": [],
        "close": True,
    },
]

A2A: list[dict[str, str]] = [
    {
        "from": "atlas",
        "to": "nova",
        "message": "Can you compare our three coffee suppliers on price and delivery?",
        "reply": "Done. Highland Roasters is cheapest; Coastal Beans is fastest. "
        "Full table in reports/supplier-comparison.md.",
    },
    {
        "from": "atlas",
        "to": "quill",
        "message": "Please mention the new single origin in Friday's newsletter.",
        "reply": "Added a short paragraph and a photo caption to the draft.",
    },
    {
        "from": "aria",
        "to": "atlas",
        "message": "A customer wants a $64 refund for a damaged bag. Over my limit.",
        "reply": "Approved. Please apologise and offer a free coffee on the next visit.",
    },
]


def _chat_rows(now: datetime) -> list[dict[str, Any]]:
    """The supplier-comparison conversation, with its tool calls."""

    def ts(minutes_ago: float) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat()

    return [
        {
            "ts": ts(6),
            "role": "user",
            "content": "Compare our three coffee suppliers on price and delivery time "
            "and save a short summary with a recommendation.",
        },
        {
            "ts": ts(5.8),
            "role": "tool_call",
            "tool_call_id": "call_read",
            "name": "read_file",
            "args": json.dumps({"file_path": "data/suppliers.csv"}),
        },
        {
            "ts": ts(5.7),
            "role": "tool_result",
            "tool_call_id": "call_read",
            "name": "read_file",
            "content": SUPPLIERS_CSV,
        },
        {
            "ts": ts(5.5),
            "role": "tool_call",
            "tool_call_id": "call_write",
            "name": "write_file",
            "args": json.dumps(
                {"file_path": "reports/supplier-comparison.md", "content": SUPPLIER_REPORT}
            ),
        },
        {
            "ts": ts(5.4),
            "role": "tool_result",
            "tool_call_id": "call_write",
            "name": "write_file",
            "content": "Updated file reports/supplier-comparison.md",
        },
        {
            "ts": ts(5.2),
            "role": "assistant",
            "content": (
                "Here is how the three suppliers compare:\n\n"
                "| Supplier | Price per kg | Delivery |\n"
                "|---|---|---|\n"
                "| Highland Roasters | $18.40 | 2 days |\n"
                "| Coastal Beans Co. | $21.10 | Next day |\n"
                "| Valley Green Farms | $19.75 | 4 days |\n\n"
                "**Recommendation:** move the house blend to Highland Roasters and "
                "keep Coastal Beans Co. for urgent top-ups. That saves about $310 a "
                "month at current volume.\n\n"
                "Saved: reports/supplier-comparison.md"
            ),
        },
    ]


def _other_threads(now: datetime) -> dict[str, list[dict[str, Any]]]:
    def row(minutes_ago: float, role: str, content: str) -> dict[str, Any]:
        return {
            "ts": (now - timedelta(minutes=minutes_ago)).isoformat(),
            "role": role,
            "content": content,
        }

    return {
        "market-brief": [
            row(190, "user", "What changed in the coffee market this week?"),
            row(189, "assistant", "Futures are down 4%, and two competitors opened nearby."),
        ],
        "oat-milk": [
            row(1500, "user", "How fast is oat milk growing in our orders?"),
            row(1499, "assistant", "38% of milk-based orders last month, up from 29%."),
        ],
    }


# ── the runner the demo agents answer with ───────────────────────────────────


class _DemoRunner:
    """Answers from fixed text; the shape the chat endpoints call."""

    def __init__(self, config: Any) -> None:
        self.config = config

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        return "Noted."

    async def astream_events(self, message: Any, **_: Any):
        for word in "Noted. I will get back to you shortly.".split(" "):
            yield ("text", word + " ")


def build_demo_gateway() -> Any:
    _use_e2e_harness()
    from harness import _gateway_for

    from olano.config.agent_config import AgentConfig, OrgConfig
    from olano.config.backend_config import FilesystemBackendConfig

    runners: dict[str, Any] = {}
    for spec in AGENTS:
        cfg = AgentConfig(
            name=spec["name"],
            model=spec["model"],
            backend=FilesystemBackendConfig(root_dir="workspace"),
        )
        cfg.emoji = spec["emoji"]
        cfg.description = spec["description"]
        cfg.channels = []
        cfg.org = OrgConfig(
            title=spec["title"], manager=spec["manager"], reports=list(spec["reports"])
        )
        runners[spec["name"]] = _DemoRunner(cfg)
    return _gateway_for(runners)


# ── seeding the throwaway home ───────────────────────────────────────────────


def seed_home(gateway: Any, home: Path, runtime_url: str, push_event: Callable) -> None:
    import httpx

    now = datetime.now(timezone.utc)
    (home / "config.yaml").write_text("default_agent: nova\n", encoding="utf-8")
    for spec in AGENTS:
        name = spec["name"]
        workspace = home / "agents" / name / "workspace"
        # The configs were built before the launcher picked the home; point
        # them at it so every surface agrees on where the files are.
        gateway._runners[name].config.workspace = str(workspace)
        entry = {
            "name": name,
            "model": spec["model"],
            "description": spec["description"],
            "emoji": spec["emoji"],
            "backend": {"type": "filesystem", "root_dir": "workspace"},
            "org": {
                "title": spec["title"],
                "manager": spec["manager"],
                "reports": spec["reports"],
            },
        }
        cfg_path = home / "agents" / name / "agent.json5"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        for rel, text in WORKSPACE_FILES.get(name, {}).items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    sessions = home / "sessions" / "nova"
    sessions.mkdir(parents=True, exist_ok=True)
    threads = {"supplier-comparison": _chat_rows(now), **_other_threads(now)}
    for thread_id, rows in threads.items():
        with (sessions / f"{thread_id}.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    with httpx.Client(base_url=runtime_url, timeout=30.0) as api:

        def call(method: str, url: str, **kw: Any) -> Any:
            resp = api.request(method, url, **kw)
            resp.raise_for_status()
            return resp.json()

        call("PUT", "/api/memory", params={"agent": "nova"}, json={"content": NOVA_MEMORY})
        for agent, notes in NOTES.items():
            for note in notes:
                call("POST", "/api/notes", params={"agent": agent}, json=note)
        for task in TASKS:
            call("POST", "/api/scheduler/tasks", json={**task, "timezone": "UTC"})
        for issue in ISSUES:
            created = call(
                "POST",
                "/api/issues",
                json={k: issue[k] for k in ("title", "body", "assignee", "labels", "type")},
            )["issue"]
            for comment in issue["comments"]:
                call("POST", f"/api/issues/{created['id']}/comment", json={"body": comment})
            if issue.get("close"):
                call("POST", f"/api/issues/{created['id']}/close", json={})

    for i, msg in enumerate(A2A):
        sent = (now - timedelta(minutes=40 - i * 10)).isoformat()
        replied = (now - timedelta(minutes=39 - i * 10)).isoformat()
        base = {"kind": "peer", "from": msg["from"], "to": msg["to"], "message": msg["message"]}
        push_event("agent_message", {**base, "phase": "send", "ts": sent})
        push_event(
            "agent_message",
            {**base, "phase": "reply", "reply": msg["reply"], "ts_sent": sent, "ts_reply": replied},
        )
    time.sleep(1.0)


# ── the shots ────────────────────────────────────────────────────────────────


@dataclass
class Shot:
    name: str
    path: str
    wait_for: str
    caption: str
    before: Callable[[Any], None] | None = None
    settle_s: float = 1.5
    viewport: dict[str, int] = field(default_factory=lambda: dict(VIEWPORT))
    shell: str = "aside.sidebar"


def _open_nova_chat(page: Any) -> None:
    sidebar = page.locator("aside.sidebar")
    sidebar.get_by_text("nova", exact=True).first.click()
    page.get_by_text("Compare our three coffee suppliers", exact=False).first.click()
    page.locator(".bubble.assistant").first.wait_for(timeout=20_000)


def _open_supplier_report(page: Any) -> None:
    page.locator("[role='treeitem'][data-kind='agent'][data-agent='nova']").first.click()
    for folder in ("workspace", "reports"):
        page.locator("[role='treeitem']", has_text=folder).last.click()
    page.get_by_text("supplier-comparison.md", exact=True).first.click()


def _scroll_config(page: Any) -> None:
    # Past the header line, which names the throwaway home's temp path. The
    # General card, not the model cards below it: those name the concrete
    # models a box falls back to, which the guide never shows.
    card = page.locator("section.card:has-text('Defaults the CLI and dashboard use')").first
    card.wait_for(timeout=30_000)
    card.evaluate(
        """el => {
            el.scrollIntoView({block: 'start'});
            let box = el.parentElement;
            while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement;
            (box || document.scrollingElement).scrollBy(0, -72);
        }"""
    )


def _open_mobile_chat(page: Any) -> None:
    page.locator(".bubble.assistant").first.wait_for(timeout=20_000)


def _reset_setup(page: Any) -> None:
    # Re-arms the first-run wizard. Taken last: the shots after it would land
    # in the wizard too.
    page.evaluate("() => fetch('/api/onboarding', {method: 'DELETE'})")
    page.reload()
    page.locator(".ob-welcome").wait_for(timeout=30_000)


def _setup_account_step(page: Any) -> None:
    page.locator(".ob-welcome .btn.primary").click()
    page.locator(".ob-form input[name='name']").fill("Sam Rivera")
    page.locator(".ob-form input[type='email']").fill("sam@example.com")


def _open_first_note(page: Any) -> None:
    page.get_by_text("Supplier scoring method", exact=True).first.click()


SHOTS: list[Shot] = [
    Shot("overview", "/overview", "text=Scheduled tasks", "The Overview tab"),
    Shot("chat", "/chat", ".chat-header", "A chat with tool calls", before=_open_nova_chat),
    Shot("agents", "/agents", "main button:has-text('New agent')", "The Agents roster"),
    Shot("agent-detail", "/agents/nova?tab=overview", "text=Identity", "An agent's Overview tab"),
    Shot("scheduler", "/scheduler", "text=Weekly market brief", "The Scheduler tab"),
    Shot("calendar", "/calendar", "text=Today", "The Calendar tab"),
    Shot("issues", "/issues", "text=Switch the house blend", "The Issues tab"),
    Shot(
        "files",
        "/files?agent=nova",
        "text=reports",
        "The Files tab",
        before=_open_supplier_report,
    ),
    Shot("memory", "/memory", "text=MEMORY.md", "The Memory tab"),
    Shot(
        "notes", "/notes", "text=Supplier scoring method", "The Notes tab", before=_open_first_note
    ),
    Shot("connections", "/connections", ".card:has-text('GitHub')", "The Connections tab"),
    Shot("providers", "/providers", "text=Olano (managed)", "The Providers tab"),
    Shot("tools", "/tools", "text=Toolkits", "The Tools tab"),
    Shot("org-chart", "/orgchart", "text=Head of Operations", "The Org Chart tab"),
    Shot("agent-to-agent", "/activity", "[data-testid='a2a-rail']", "The Agent to Agent tab"),
    Shot(
        "config",
        "/config",
        "section.card:has-text('Defaults the CLI and dashboard use')",
        "The Config tab",
        before=_scroll_config,
    ),
    Shot("vault", "/vault", "text=Vault", "The Vault tab"),
    Shot(
        "chat-mobile",
        "/chat?thread=supplier-comparison",
        ".bubble.assistant",
        "The chat on a phone",
        before=_open_mobile_chat,
        viewport={"width": 390, "height": 844},
        shell="header.mobile-topbar",
    ),
    # Last, see _reset_setup.
    Shot("setup-welcome", "/overview", ".ob-welcome", "The first-run setup", before=_reset_setup),
    Shot(
        "setup-account",
        "/overview",
        ".ob-form",
        "Setup: the owner account",
        before=_setup_account_step,
        shell=".ob-root",
    ),
]


def _to_webp(png: bytes, dest: Path) -> int:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width > OUT_WIDTH:
        height = round(img.height * OUT_WIDTH / img.width)
        img = img.resize((OUT_WIDTH, height), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "WEBP", quality=WEBP_QUALITY, method=6)
    return dest.stat().st_size


def capture(names: list[str], out_dir: Path) -> int:
    import logging

    _use_e2e_harness()
    from harness import launch_dashboard
    from playwright.sync_api import sync_playwright

    wanted = [s for s in SHOTS if not names or s.name in names]
    unknown = sorted(set(names) - {s.name for s in SHOTS})
    if unknown:
        print(f"unknown shot(s): {', '.join(unknown)}; see --list", file=sys.stderr)
        return 2

    # The runtime logs every request it makes; the report below is the output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    gateway = build_demo_gateway()
    failures = 0
    with launch_dashboard(gateway) as dash, sync_playwright() as pw:
        home = Path(os.environ["LIONCLAW_HOME"])
        seed_home(gateway, home, dash.runtime_url, dash.push_event)
        launch_args: dict[str, Any] = {"args": ["--no-sandbox"]}
        exe = os.environ.get("LIONCLAW_PW_EXECUTABLE")
        if exe:
            launch_args["executable_path"] = exe
        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme="dark"
        )
        page = context.new_page()
        for shot in wanted:
            try:
                page.set_viewport_size(shot.viewport)
                page.goto(dash.base_url + shot.path)
                page.locator(shot.shell).first.wait_for(timeout=30_000)
                if shot.before is not None:
                    shot.before(page)
                page.locator(shot.wait_for).first.wait_for(timeout=30_000)
                # Not "networkidle": the live event stream keeps a request open
                # for as long as the page is, so the network is never idle.
                time.sleep(shot.settle_s)
                size = _to_webp(page.screenshot(), out_dir / f"{shot.name}.webp")
                print(f"  {shot.name:16s} {size / 1024:6.1f} KB  {shot.path}")
            except Exception as exc:
                failures += 1
                print(f"  {shot.name:16s} FAILED: {exc}", file=sys.stderr)
                debug = out_dir / f"{shot.name}.failed.png"
                page.screenshot(path=str(debug))
                print(f"  {'':16s} what the page showed: {debug}", file=sys.stderr)
        browser.close()
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="shots to take (default: all)")
    parser.add_argument("--list", action="store_true", help="print the shot names and exit")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    args = parser.parse_args(argv)
    if args.list:
        for s in SHOTS:
            print(f"{s.name:16s} {s.path}")
        return 0
    return capture(args.names, args.out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
