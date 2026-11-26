"""Pebble & Thread, a small tailoring shop: the team in the price-check video.

Juno buys the shop's odds and ends. In the video Juno compares noise-cancelling earbuds on
Amazon.sg (a real public website with no API and no account) and then books a daily price
check on the scheduler. Every browser step of the scripted turn runs the product's own browser
tool for the agent (``browser_navigate``) in the product's headless
browser, against the live site; the reply is built from what that call returned, so prices,
ratings and names on screen are whatever Amazon.sg showed during the take. The scheduler step
is the product's own ``scheduler_create`` tool, so the task in the Scheduler tab is the one it
saved. (No screenshot: this machine's proxy refuses Amazon's image and style hosts, so a picture of
the page would show it unstyled; the page's text, which the reply is built from, is complete.) Nothing is added to a cart, nobody signs in, nothing is bought.

What this file sets, and why:

- **The way out to the internet.** This machine reaches the web only through a local HTTPS
  proxy (``HTTPS_PROXY``), which re-terminates TLS with its own certificate authority. The
  product's documented per-agent setting for a browser proxy, ``BROWSER_PROXY_URL``, is pointed
  at it, so the browser's own egress guard still sees and vets every request (the proxy is its
  upstream). Chromium trusts that authority through the machine's NSS store, which the proxy's
  set-up already filled; no certificate check is turned off.
- **Browser mode.** Off, as on a new install; the video turns it to Headless in Config.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# The per-agent proxy setting, read from the environment as the last resolver layer.
if os.environ.get("HTTPS_PROXY"):
    os.environ.setdefault("BROWSER_PROXY_URL", os.environ["HTTPS_PROXY"])

BUDGET = 80.0
SEARCH = "https://www.amazon.sg/s?k=noise+cancelling+earbuds"
NAV: dict[str, str] = {}  # the browser_navigate result of this take, for the reply builder
NUMBERS = {1: "one", 2: "two", 3: "three"}


@contextlib.contextmanager
def as_agent(agent: str) -> Iterator[None]:
    """The context a tool call of this agent runs in: its identity and its workspace."""
    from olano.security.request_context import push_request_context
    from olano.tools._credentials import agent_runtime_context

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / agent / "workspace"
    with (
        agent_runtime_context(agent, {}),
        push_request_context(metadata={"workspace_root": str(workspace)}),
    ):
        yield


def close_browsers() -> None:
    """Close the take's browser sessions when the dashboard stops."""
    with contextlib.suppress(Exception):
        from olano.browser import loop as browser_loop
        from olano.browser import sessions

        browser_loop.submit(sessions.close_all(save=False)).result(10)


def tool(name: str, args: dict[str, Any]):
    """A step result that runs the product's own tool for the agent, for real."""

    def run(ctx: dict) -> str:
        import olano.tools.browser as browser_tools
        import olano.tools.scheduler_tools as scheduler_tools

        fn = getattr(browser_tools, name, None) or getattr(scheduler_tools, name)
        with as_agent(ctx["agent"]):
            out = str(fn.invoke(args))
        ctx[name] = out
        return out

    return run


def _navigate(ctx: dict) -> str:
    out = tool("browser_navigate", {"url": SEARCH, "wait_for": "load"})(ctx)
    NAV["text"] = out
    return out


def parse_results(snapshot: str) -> list[dict[str, Any]]:
    """The search results in a ``browser_navigate`` snapshot of an Amazon results page."""
    items: list[dict[str, Any]] = []
    for block in re.split(r"\n- listitem \[ref=", snapshot):
        title = re.search(r'heading "(.+?)" \[level=2\]', block)
        price = re.search(r"S\$\s?([\d,]+\.\d\d)", block)
        rating = re.search(r"(\d\.\d) out of 5 stars", block)
        asin = re.search(r"/dp/([A-Z0-9]{10})", block)
        count = re.search(r'link "([\d,]+) ratings?"', block)
        if not (title and price and rating and asin):
            continue
        name = title.group(1).replace('\\"', '"')
        short = re.split(r",| - | \| |\(", name)[0].strip()
        words = short.split()
        if len(words) > 1 and words[0].lower() == words[1].lower():  # "Brand Brand Model"
            words = words[1:]
            short = " ".join(words)
        if len(words) > 5:
            short = " ".join(words[:5])
        items.append(
            {
                "name": short,
                "price": float(price.group(1).replace(",", "")),
                "rating": float(rating.group(1)),
                "ratings": int(count.group(1).replace(",", "")) if count else 0,
                "url": f"https://www.amazon.sg/dp/{asin.group(1)}",
            }
        )
    seen: set[str] = set()
    unique = []
    for it in items:  # one row per product: colour variants share a name
        key = it["name"].lower()
        if it["url"] not in seen and key not in seen:
            seen.update((it["url"], key))
            unique.append(it)
    return unique


def picks() -> list[dict[str, Any]]:
    found = [i for i in parse_results(NAV.get("text", "")) if i["price"] <= BUDGET]
    found.sort(key=lambda i: (-i["rating"], -i["ratings"], i["price"]))
    return found[:3]


def compare(ctx: dict) -> str:
    """Juno's answer, from the results page the browser call returned."""
    best = picks()
    if not best:
        return (
            "Amazon.sg did not show results I could read this time, so I have nothing to "
            "compare yet. I can try again in a minute."
        )
    rows = "\n".join(
        f"| [{i['name']}]({i['url']}) | S${i['price']:.2f} | {i['rating']:.1f} of 5 |" for i in best
    )
    SCHEDULE_ARGS.update(_schedule_args())  # what the next turn's call shows and sends
    cheapest = min(best, key=lambda i: i["price"])
    top = best[0]
    count = NUMBERS.get(len(best), str(len(best)))
    if cheapest is top:
        verdict = f"**{top['name']}** has the best rating and is also the cheapest."
    else:
        verdict = f"**{top['name']}** has the best rating; **{cheapest['name']}** is the cheapest."
    return (
        f"{count.capitalize()} noise-cancelling earbuds under S${BUDGET:.0f} on Amazon.sg, "
        "best rated first:\n\n"
        "| Earbuds | Price | Rating |\n|---|---|---|\n"
        f"{rows}\n\n"
        f"{verdict} Prices are as the results page showed them just now."
    )


def _schedule_args() -> dict[str, Any]:
    best = picks()
    links = "\n".join(f"- {i['name']}: {i['url']} (S${i['price']:.2f} today)" for i in best)
    return {
        "name": "Earbuds price check",
        "prompt": "Open these on Amazon.sg and tell me if any is cheaper than before:\n"
        + (links or SEARCH),
        "schedule": "0 9 * * *",
        "timezone": "Asia/Singapore",
    }


SCHEDULE_ARGS: dict[str, Any] = {
    "name": "Earbuds price check",
    "schedule": "0 9 * * *",
    "timezone": "Asia/Singapore",
}


def schedule(ctx: dict) -> str:
    SCHEDULE_ARGS.update(_schedule_args())
    return tool("scheduler_create", dict(SCHEDULE_ARGS))(ctx)


def booked(ctx: dict) -> str:
    out = ctx.get("scheduler_create", "")
    if "Task created" not in out:
        return f"I could not save the task: {out.strip()[:200]}"
    n = len(picks())
    what = f"the {NUMBERS.get(n, n)} earbuds" if n > 1 else "the earbuds search"
    return (
        f"Done. **Earbuds price check** runs every day at 9:00, Singapore time. "
        f"Each morning I open {what} on Amazon.sg and tell you if a price has dropped. "
        "It is in the Scheduler, where you can change or pause it."
    )


TEAM = {
    "default_agent": "juno",
    "agents": [
        {
            "name": "juno",
            "emoji": "🧵",
            "description": "Buys supplies and small equipment for the shop.",
            "title": "Purchasing",
            "manager": "marisol",
        },
        {
            "name": "marisol",
            "emoji": "✂️",
            "description": "Runs the shop: fittings, alterations and the week's orders.",
            "title": "Shop Manager",
            "manager": None,
            "reports": ["juno", "teo"],
        },
        {
            "name": "teo",
            "emoji": "📦",
            "description": "Handles customer pickups and deliveries.",
            "title": "Orders",
            "manager": "marisol",
        },
    ],
    "files": {
        "juno": {
            "AGENTS.md": "# juno\n\nPurchasing at Pebble & Thread, a tailoring shop. "
            "Read-only on shopping sites: compare, never buy.\n",
        },
        "marisol": {"AGENTS.md": "# marisol\n\nShop manager at Pebble & Thread.\n"},
        "teo": {"AGENTS.md": "# teo\n\nOrders at Pebble & Thread.\n"},
    },
    "tasks": [
        {
            "name": "Fabric stock count",
            "prompt": "Count the fabric bolts and list what runs low.",
            "schedule": "0 17 * * fri",
            "agent_name": "marisol",
        },
    ],
    "turns": {
        "juno": [
            {
                "match": "every morning",
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "scheduler_create",
                        "args": SCHEDULE_ARGS,
                        "result": schedule,
                        "took": 0.4,
                    },
                    {"wait": 0.4},
                    {"text": booked, "wps": 14},
                ],
            },
            {
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "browser_navigate",
                        "args": {"url": SEARCH, "wait_for": "load"},
                        "result": _navigate,
                        "took": 0.3,
                    },
                    {"wait": 1.2},
                    {"wait": 1.0},
                    {"text": compare, "wps": 16},
                ]
            },
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    atexit.register(close_browsers)
