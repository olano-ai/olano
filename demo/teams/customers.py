"""Inkwell & Ivy, an online stationery shop: the team in the customers video.

Nell answers the shop's customers on Telegram. In the take her Multi-user tab turns customer service
mode on and adds an allowed action, ``my_order``, whose script (``actions/my_order.py``, seeded
here) reads the asking customer's identity from the environment Olano gives it.

Two customers then write to the shop's bot. Their messages are Bot API ``Update`` payloads handed to
the product's own Telegram adapter and runtime handler (``teams/_telegram.py`` explains the seam:
only Telegram's network is stood in for). Nell's replies are scripted, but every tool step in them
runs the product's own tool for real, inside the request context the handler pushed for that
sender: ``remember`` / ``recall`` / ``forget_me`` (``memory/customer_memory.py``) and
``execute_action`` (``tools/customer_actions.py``), built from the agent's ``agent.json5`` as it is
at that moment. The reply text is built from what those tools returned.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

AGENT = "nell"
PORT = int(os.environ.get("DEMO_CUSTOMERS_PORT", "8961"))

MAYA = {
    "id": 6123400871,
    "first_name": "Maya",
    "last_name": "Ortiz",
    "username": "maya_ortiz",
}
TOMAS = {
    "id": 7020455512,
    "first_name": "Tomas",
    "last_name": "Berg",
    "username": "tomasberg",
}

MY_ORDER = '''\
"""my_order: the latest order of the customer who is asking.

Olano runs this script for one customer at a time. Who that is comes
from the environment, never from the agent, so it cannot be changed
by anything said in the chat.
"""
import json
import os
from pathlib import Path

platform = os.environ["LIONCLAW_CUSTOMER_PLATFORM"]   # telegram
customer = os.environ["LIONCLAW_CUSTOMER_ID"]         # their Telegram id

orders = json.loads((Path(__file__).parents[1] / "shop/orders.json").read_text())
mine = [o for o in orders if o["platform"] == platform and o["customer"] == customer]
print(json.dumps(mine[-1] if mine else {"order": None}, indent=2))
'''

ORDERS = [
    {
        "order": "II-2031",
        "platform": "telegram",
        "customer": "5550193120",
        "items": ["A5 dot-grid notebook, sage"],
        "status": "delivered",
        "carrier": "Parcelway",
        "eta": "delivered 18 Nov",
    },
    {
        "order": "II-2038",
        "platform": "telegram",
        "customer": str(TOMAS["id"]),
        "items": ["Fountain pen ink, 50 ml, deep teal", "Ink blotter cards (20)"],
        "status": "delivered",
        "carrier": "Parcelway",
        "eta": "delivered 21 Nov",
    },
    {
        "order": "II-2044",
        "platform": "telegram",
        "customer": str(MAYA["id"]),
        "items": ["Linen-bound sketchbook, A4", "Brass pencil sharpener"],
        "status": "shipped",
        "carrier": "Parcelway",
        "tracking": "PW 4471 2208 93",
        "eta": "Friday 29 Nov",
    },
    {
        "order": "II-2047",
        "platform": "telegram",
        "customer": str(TOMAS["id"]),
        "items": ["Wax seal kit, forest green"],
        "status": "packing",
        "carrier": "Parcelway",
        "eta": "ships Monday 2 Dec",
    },
]

_HOME: dict[str, Path] = {}


def _workspace() -> Path:
    return _HOME["home"] / "agents" / AGENT / "workspace"


def _tools() -> dict[str, Any]:
    """Nell's per-customer tools, built as the product's builder builds them, from agent.json5 now."""
    from olano.config.home import get_olano_paths
    from olano.config.loader import load_agent_config
    from olano.memory.customer_memory import make_customer_memory_tools
    from olano.tools.customer_actions import make_customer_action_tools

    cfg = load_agent_config(AGENT)
    roots = [_workspace(), get_olano_paths().agent_workspace_dir(AGENT).parent]
    tools = [
        *make_customer_memory_tools(AGENT),
        *make_customer_action_tools(AGENT, cfg.allowed_actions, roots),
    ]
    return {t.name: t for t in tools}


def _tool(name: str, args: dict[str, Any], key: str):
    def run(ctx: dict[str, Any]) -> str:
        tool = _tools().get(name)
        out = "Error: tool not available" if tool is None else str(tool.invoke(args))
        ctx[key] = out
        return out

    return run


def _order(ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(ctx.get("order", ""))
    except ValueError:
        return {}


def _maya_reply(ctx: dict[str, Any]) -> str:
    o = _order(ctx)
    if not o.get("order"):
        return "Thanks, Maya. I could not find an order for you yet. Could you send the order number?"
    items = " and ".join(i.lower() if n else i for n, i in enumerate(o["items"]))
    return (
        f"Hi Maya! Your order {o['order']} ({items}) has {o['status']} with {o['carrier']}. "
        f"Tracking: {o.get('tracking', 'not yet')}. It should arrive {o['eta']}."
    )


def _tomas_reply(ctx: dict[str, Any]) -> str:
    o = _order(ctx)
    lead = (
        f"Your order {o['order']} ({o['items'][0].lower()}) is {o['status']} and {o['eta']}."
        if o.get("order")
        else "I could not find an order for you yet."
    )
    other = (
        "I can only see your own account, so I can't share anything about another customer."
        if "Nothing matching" in ctx.get("recall", "")
        else "I can only share your own orders."
    )
    return f"{lead}\n\n{other}"


def _forget_reply(ctx: dict[str, Any]) -> str:
    if ctx.get("forget", "").startswith("Deleted"):
        return (
            "Done. Everything I had saved about you is deleted, Maya. "
            "If you write again, I will start fresh."
        )
    return "I could not delete your details just now. Please try again in a moment."


TEAM = {
    "default_agent": AGENT,
    "agents": [
        {
            "name": AGENT,
            "display_name": "Nell",
            "emoji": "🖋️",
            "description": "Answers Inkwell & Ivy's customers on Telegram: orders, delivery, returns.",
            "title": "Customer Support",
        },
    ],
    "files": {
        AGENT: {
            "AGENTS.md": "# Nell\n\nCustomer support for Inkwell & Ivy, an online stationery shop.\n",
            "actions/my_order.py": MY_ORDER,
            "shop/orders.json": json.dumps(ORDERS, indent=2) + "\n",
            "shop/returns.md": "# Returns\n\n- 30 days, unused, in the original wrapping.\n",
        },
    },
    "turns": {
        AGENT: [
            {
                "match": "forget me",
                "steps": [
                    {"wait": 0.8},
                    {"tool": "forget_me", "args": {}, "result": _tool("forget_me", {}, "forget"), "took": 0.8},
                    {"wait": 0.4},
                    {"text": _forget_reply, "wps": 12},
                ],
            },
            {
                "match": "what did maya",
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "execute_action",
                        "args": {"action": "my_order"},
                        "result": _tool("execute_action", {"action": "my_order"}, "order"),
                        "took": 1.0,
                    },
                    {
                        "tool": "recall",
                        "args": {"query": "Maya"},
                        "result": _tool("recall", {"query": "Maya"}, "recall"),
                        "took": 0.7,
                    },
                    {"wait": 0.4},
                    {"text": _tomas_reply, "wps": 12},
                ],
            },
            {
                "match": "my order",
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "remember",
                        "args": {"fact": "Name: Maya Ortiz. Prefers delivery updates in this chat."},
                        "result": _tool(
                            "remember",
                            {"fact": "Name: Maya Ortiz. Prefers delivery updates in this chat."},
                            "remember",
                        ),
                        "took": 0.7,
                    },
                    {
                        "tool": "execute_action",
                        "args": {"action": "my_order"},
                        "result": _tool("execute_action", {"action": "my_order"}, "order"),
                        "took": 1.0,
                    },
                    {"wait": 0.4},
                    {"text": _maya_reply, "wps": 12},
                ],
            },
        ],
    },
}


def after_seed(home: Path, gateway: Any, **_: object) -> None:
    """Connect Nell's Telegram adapter to the runtime handler and open the local message endpoint."""
    import __main__

    from teams import _telegram

    _HOME["home"] = home
    loop = __main__.LOOP
    runner = _telegram.channel_runner(gateway._runners[AGENT])
    gateway._runners[AGENT] = runner
    seams = {AGENT: _telegram.TelegramSeam(AGENT, runner)}
    _telegram.attach_all(seams, loop)
    _telegram.serve(seams, loop, PORT)
