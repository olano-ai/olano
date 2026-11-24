"""Marlow & Finch, a small accounting firm: the team in the control video."""

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

EMAIL = {
    "to": "priya.nair@oakfieldbakery.example",
    "subject": "Your Q3 VAT return is ready to review",
    "body": "Hi Priya, your Q3 VAT return is ready. The amount due is $4,218, payable by 30 November. "
    "Please reply to approve and I will file it.",
}

TEAM = {
    "default_agent": "aria",
    "vault": [
        {
            "name": "OPENAI_API_KEY",
            "value": "demo-value-not-a-real-key-1",
            "description": "Model provider key",
            "category": "provider",
        },
        {
            "name": "GMAIL_APP_PASSWORD",
            "value": "demo-value-not-a-real-key-2",
            "description": "Outbound mail for client emails",
            "category": "email",
        },
        {
            "name": "XERO_CLIENT_SECRET",
            "value": "demo-value-not-a-real-key-3",
            "description": "Ledger access for the bookkeeping agents",
            "category": "accounting",
        },
        {
            "name": "TELEGRAM_BOT_TOKEN",
            "value": "demo-value-not-a-real-key-4",
            "description": "Partner notifications",
            "category": "channel",
        },
        {
            "name": "STRIPE_API_KEY",
            "value": "demo-value-not-a-real-key-5",
            "description": "Invoice payments, read only",
            "category": "payments",
        },
    ],
    "agents": [
        {
            "name": "aria",
            "emoji": "💼",
            "description": "Client emails, reminders and document requests.",
            "title": "Client Manager",
            "manager": "finch",
        },
        {
            "name": "finch",
            "emoji": "📊",
            "description": "Runs the practice and reviews filings.",
            "title": "Practice Lead",
            "manager": None,
            "reports": ["aria", "ledger"],
        },
        {
            "name": "ledger",
            "emoji": "🧾",
            "description": "Bookkeeping, reconciliations and VAT returns.",
            "title": "Bookkeeper",
            "manager": "finch",
        },
    ],
    "files": {
        "aria": {
            "AGENTS.md": "# aria\n\nClient manager at Marlow & Finch. Always ask before sending email.\n",
            "clients/oakfield-bakery/q3-vat-return.md": "# Oakfield Bakery: Q3 VAT return\n\nAmount due: $4,218. Payable by 30 November.\n",
        },
        "ledger": {"AGENTS.md": "# ledger\n\nBookkeeper at Marlow & Finch.\n"},
        "finch": {"AGENTS.md": "# finch\n\nPractice lead at Marlow & Finch.\n"},
    },
    "turns": {
        "aria": [
            {
                "steps": [
                    {"wait": 0.7},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "clients/oakfield-bakery/q3-vat-return.md"},
                        "result": "Amount due: $4,218. Payable by 30 November.",
                        "took": 1.0,
                    },
                    {
                        "text": "The return is ready. I drafted the email to Priya at Oakfield Bakery. May I send it?\n\n",
                        "wps": 14,
                    },
                    {"approve": [{"name": "gmail_send", "args": EMAIL, "risk": "medium"}]},
                    {
                        "tool": "gmail_send",
                        "args": {"to": EMAIL["to"], "subject": EMAIL["subject"]},
                        "result": "Sent.",
                        "took": 0.9,
                    },
                    {"text": "Sent. I will let you know when Priya replies.", "wps": 14},
                ]
            }
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    """A month of usage for Insights and a few days of history for Revisions."""
    from olano.analytics.usage_ledger import record_usage_event
    from olano.revisions.engine import snapshot_home

    rng = random.Random(11)
    now = datetime.now(timezone.utc)
    mix = {
        "aria": ("openai:gpt-6-luna", 26, 0.021),
        "ledger": ("openai:gpt-6.1-sol", 14, 0.058),
        "finch": ("openai:gpt-6.1-sol", 6, 0.064),
    }
    for back in range(30, -1, -1):
        day = now - timedelta(days=back)
        weekend = day.weekday() >= 5
        for agent, (model, per_day, usd) in mix.items():
            turns = int(
                per_day * (0.25 if weekend else 1) * rng.uniform(0.7, 1.3) * (1 + (30 - back) / 60)
            )
            for _ in range(turns):
                ts = day.replace(
                    hour=rng.randint(8, 18), minute=rng.randint(0, 59), second=rng.randint(0, 59)
                )
                if ts > now:
                    continue
                tin = rng.randint(6000, 22000)
                record_usage_event(
                    agent,
                    model,
                    tin,
                    rng.randint(250, 1400),
                    usd * rng.uniform(0.5, 1.6),
                    cache_read_tokens=int(tin * 0.6),
                    calls=rng.randint(1, 5),
                    ts=ts.isoformat(),
                    origin=rng.choice(["chat", "chat", "chat", "scheduled"]),
                    thread_id=f"t{rng.randint(1, 40)}",
                )

    # Revisions: the first snapshot, then three changes, each by the agent that made it.
    ws = home / "agents"
    snapshot_home(reason="first snapshot", home=home)
    (ws / "aria" / "workspace" / "AGENTS.md").write_text(
        "# aria\n\nClient manager at Marlow & Finch. Always ask before sending email.\n\n"
        "## Tone\n- Plain words, short sentences.\n- Give the amount and the deadline in the first two lines.\n",
        encoding="utf-8",
    )
    snapshot_home(author="aria", reason="aria: turn in chat", home=home)
    (ws / "ledger" / "workspace" / "MEMORY.md").write_text(
        "# MEMORY.md\n\n## Clients\n- Oakfield Bakery files VAT quarterly; Priya Nair approves by email.\n",
        encoding="utf-8",
    )
    snapshot_home(author="ledger", reason="ledger: scheduled run", home=home)
    (ws / "aria" / "workspace" / "AGENTS.md").write_text(
        "# aria\n\nClient manager at Marlow & Finch. Always ask before sending email.\n\n"
        "## Tone\n- Plain words, short sentences.\n- Give the amount and the deadline in the first two lines.\n\n"
        "## Deadlines\n- Remind clients ten days before a filing date, and again three days before.\n",
        encoding="utf-8",
    )
    snapshot_home(author="aria", reason="aria: turn in chat", home=home)
