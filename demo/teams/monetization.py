"""Puente Spanish, a small online Spanish-tutoring service: the team in the monetization video.

Lumi is Puente's Spanish tutor bot on Telegram, sold as a monthly subscription. In the take the
owner sets the plan on Lumi's Monetization tab (Stripe price, Telegram Stars amount, trial days) and
restarts the agent; the Stripe secret key is in Lumi's vault, saved through the product's own API.

The key is the **test-mode** key of a Stripe sandbox (``OLANO_STRIPE_SANDBOX_SECRET_KEY``,
``sk_test_``; this file refuses anything else), and the price is a real test-mode price created in
that sandbox for this video (``.work/monetization-stripe-setup.py``): product "Olano demo: Lumi
Spanish tutor monthly", $9 a month. No live key is ever read: every ``*LIVE*`` Stripe variable is
removed from this process's environment before anything else runs.

A learner writes to the bot on Telegram: Bot API updates handed to the product's own Telegram
adapter and runtime handler (``teams/_telegram.py``; only Telegram's network is stood in for). So
the entitlement gate, the trial, the paywall and the offer are the product's own code: the Stripe
rail creates a real Checkout session in the sandbox, and the Stars rail builds its invoice, which
the adapter then fails to send because there is no bot token here (``RecordingBot``).

Two things are played out, and nothing else:

- **Time.** The trial is three days; the take cannot wait for them. ``_end_trial`` moves the
  learner's stored ``trial_start`` back three days in the product's entitlement store, so the
  gate sees the trial as over, exactly as it would three days later.
- **Lumi's words.** Lumi's replies are scripted; the ``subscription_status`` step is the product's
  own tool, run for real for the learner who asked.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

# Never let a live Stripe credential near the take.
for _name in [n for n in os.environ if "STRIPE" in n.upper() and "LIVE" in n.upper()]:
    os.environ.pop(_name, None)

# Lumi's Stripe key goes into her vault, which opens itself from this key (as serve.py does for a
# team that seeds the vault).
os.environ.setdefault("OLANO_VAULT_KEY", "demo-passphrase-not-a-real-one")
os.environ.setdefault("LIONCLAW_VAULT_KEY", os.environ["OLANO_VAULT_KEY"])

AGENT = "lumi"
PORT = int(os.environ.get("DEMO_MONETIZATION_PORT", "8962"))
PRICE_ID = "price_1UgEVeCO5K7xqxurO81Hubo5"  # test mode: Olano demo: Lumi Spanish tutor monthly

HANA = {
    "id": 5823019447,
    "first_name": "Hana",
    "last_name": "Kobayashi",
    "username": "hana_k",
}


def _sandbox_key() -> str:
    key = os.environ.get("OLANO_STRIPE_SANDBOX_SECRET_KEY", "")
    if not key.startswith("sk_test_"):
        raise SystemExit(
            "monetization demo: OLANO_STRIPE_SANDBOX_SECRET_KEY must be a test-mode key"
        )
    return key


def _status(ctx: dict[str, Any]) -> str:
    """The product's own ``subscription_status`` tool, for the learner who asked."""
    from olano.monetization.tools import make_subscription_tools

    (tool,) = make_subscription_tools(AGENT)
    out = str(tool.invoke({}))
    ctx["status"] = out
    return out


def _lesson(ctx: dict[str, Any]) -> str:
    trial = "on the free trial" in ctx.get("status", "").lower()
    lead = (
        "Yes, you're on the free trial, so let's make the most of it!"
        if trial
        else "Let's practise!"
    )
    return (
        f"{lead}\n\n"
        "At a café, try: **«Quisiera un café con leche, por favor.»** "
        "(I'd like a coffee with milk, please.)\n\n"
        "Then ask for the bill: **«¿Me trae la cuenta, por favor?»**\n\n"
        "Your turn: how would you order a tea?"
    )


TEAM = {
    "default_agent": AGENT,
    "agents": [
        {
            "name": AGENT,
            "display_name": "Lumi",
            "emoji": "🌞",
            "description": "Puente Spanish's tutor on Telegram: daily practice, sold by the month.",
            "title": "Spanish Tutor",
        },
    ],
    "files": {
        AGENT: {
            "AGENTS.md": "# Lumi\n\nA friendly Spanish tutor for Puente Spanish learners.\n",
        },
    },
    "turns": {
        AGENT: [
            {
                "steps": [
                    {"wait": 0.8},
                    {"tool": "subscription_status", "args": {}, "result": _status, "took": 0.8},
                    {"wait": 0.4},
                    {"text": _lesson, "wps": 12},
                ]
            }
        ],
    },
}

_STATE: dict[str, Any] = {}


def _end_trial(user_id: str) -> bool:
    """Played-out time: the learner's trial started three days and an hour ago."""
    from olano.monetization.store import get_entitlement_store

    store = get_entitlement_store()
    record = store.get(AGENT, "telegram", user_id)
    if record is None:
        return False
    record.trial_start = time.time() - 3 * 86_400 - 3_600
    store.upsert(record)
    return True


def after_seed(home: Path, runtime_url: str, gateway: Any, **_: object) -> None:
    """Lumi's Stripe key into her vault; her Telegram adapter; the restart stand-in."""
    import asyncio

    import httpx

    import __main__
    from teams import _telegram

    with httpx.Client(base_url=runtime_url, timeout=30.0) as api:
        api.post(
            f"/api/agents/{AGENT}/vault/secrets",
            json={
                "name": "STRIPE_API_KEY",
                "value": _sandbox_key(),
                "description": "Stripe sandbox (test mode) secret key",
            },
        ).raise_for_status()

    loop = __main__.LOOP
    runner = _telegram.channel_runner(gateway._runners[AGENT])
    gateway._runners[AGENT] = runner
    seam = _telegram.TelegramSeam(AGENT, runner, "PuenteLumiBot")
    seams = {AGENT: seam}
    _telegram.attach_all(seams, loop)
    _telegram.serve(seams, loop, PORT, extra_routes={"/end-trial": _end_trial})
    _STATE["seam"] = seam

    # The agent page's Restart: the launcher's gateway has no agents to stop or boot, so these two
    # stand in for it. A restarted agent reads its agent.json5 again, which is how a saved
    # Monetization tab reaches the running agent (the tab says so); nothing else changes.
    async def _stop_agent(name: str) -> None:
        return None

    async def _boot_agent(name: str) -> None:
        from olano.config.loader import load_agent_config

        r = gateway._runners.get(name)
        if r is None:
            return
        cfg = load_agent_config(name)
        cfg.workspace = r.config.workspace
        r.config = cfg
        await asyncio.sleep(0)

    gateway._stop_agent = _stop_agent
    gateway._boot_agent = _boot_agent
