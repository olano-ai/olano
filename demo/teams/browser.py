"""Thistledown Pottery, a small ceramics studio: the team in the browser video.

Wren orders clay from the studio's supplier through the supplier's order form, in a real browser.
Every browser step of the scripted turn runs the product's own browser tool (``browser_navigate``,
``browser_fill``, ``browser_click``) for the agent, so the page in the video is a live Chromium in
the agent's own ``olano-browser`` container, the approval card is the one the consequence guard
raises, and the order confirmation is what the form's server answered.

What this file stands in for, and nothing more:

- **The supplier's website.** "Northgate Ceramic Supply" is made up, so its order form is a small
  page served by this process (``_Supplier``). The browser container reaches it on the docker
  network's gateway, which is a private address, and the browser's guard refuses private addresses
  by default. The product has a documented switch for exactly this case, a developer pointing an
  agent at a server on the same machine: ``LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK=1`` (see
  ``olano/browser/guard.py``). It is set here, and the address the video shows is that private one.
- **The graph around one tool call.** The consequence guard asks for approval by raising a
  LangGraph interrupt from inside ``browser_click``; outside a graph the tool refuses instead,
  because there is nobody to ask. The scripted runner has no graph, so the click runs in a
  one-node LangGraph graph of its own (``_Click``). The interrupt it raises is read with the
  runner's own ``_extract_interrupt_actions`` and risk classifier, shown as the chat's approval
  card, and the person's decision resumes the graph, which is when the click happens.
- **Clearing up the browser container.** See ``_remove_browser_container``.
"""

from __future__ import annotations

import contextlib
import html
import http.server
import os
import subprocess
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PORT = int(os.environ.get("DEMO_SUPPLIER_PORT", "8930"))
STUDIO = "Thistledown Pottery"
CONTACT = "orders@thistledownpottery.example"
ITEM = "Stoneware clay, buff (12.5 kg bag)"
QUANTITY = "8"
DELIVERY = "Standard delivery (3 to 5 working days)"

# The private-network switch described above. Read by the guard on every browser call.
os.environ["LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK"] = "1"

PRICES = {
    "Stoneware clay, buff (12.5 kg bag)": 14.50,
    "Porcelain clay (12.5 kg bag)": 23.00,
    "Earthenware clay, red (12.5 kg bag)": 11.75,
    "Clear glaze, brush-on (1 litre)": 18.20,
}
DELIVERIES = [DELIVERY, "Collect from the Northgate warehouse"]

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Liberation Sans', Arial, sans-serif; background: #f4f1ea; color: #2b2a27; }
header { background: #3b4a3f; color: #f4f1ea; padding: 22px 56px; display: flex; align-items: baseline; gap: 18px; }
header b { font-size: 26px; letter-spacing: .01em; }
header span { font-size: 15px; opacity: .8; }
main { display: grid; grid-template-columns: 1.25fr 1fr; gap: 40px; padding: 30px 56px; max-width: 1280px; }
h1 { font-size: 28px; margin: 0 0 6px; }
p.lead { margin: 0 0 20px; color: #6a665d; font-size: 16px; }
label { display: block; font-size: 14px; font-weight: 700; margin: 0 0 6px; color: #4b483f; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.f { margin-bottom: 16px; }
input, select, textarea { width: 100%; font: inherit; font-size: 17px; padding: 11px 13px; border: 1.5px solid #cfc9bb;
  border-radius: 8px; background: #fff; color: inherit; }
button { font: inherit; font-size: 18px; font-weight: 700; padding: 13px 30px; border: 0; border-radius: 8px;
  background: #b4532a; color: #fff; cursor: pointer; }
aside { background: #fff; border-radius: 12px; padding: 22px 24px; align-self: start; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
aside h2 { margin: 0 0 12px; font-size: 14px; letter-spacing: .1em; text-transform: uppercase; color: #6a665d; }
table { width: 100%; border-collapse: collapse; font-size: 16px; }
td { padding: 10px 0; border-top: 1px solid #ece7dc; }
td:last-child { text-align: right; font-weight: 700; white-space: nowrap; }
.note { margin: 14px 0 0; font-size: 14px; color: #6a665d; }
.ok { display: inline-block; background: #dfeedd; color: #2f5d34; font-weight: 700; font-size: 14px;
  padding: 6px 12px; border-radius: 99px; margin-bottom: 14px; }
.ref { font-size: 20px; margin: 0 0 22px; }
.ref b { font-size: 24px; }
a.btn { display: inline-block; margin-top: 22px; font-size: 17px; font-weight: 700; padding: 12px 24px; border-radius: 8px;
  border: 2px solid #3b4a3f; color: #3b4a3f; text-decoration: none; background: #fff; }
ol.steps { list-style: none; padding: 0; margin: 0; font-size: 18px; }
ol.steps li { padding: 14px 0 14px 40px; border-top: 1px solid #ece7dc; position: relative; color: #8b877c; }
ol.steps li::before { content: ''; position: absolute; left: 6px; top: 17px; width: 16px; height: 16px; border-radius: 50%;
  border: 2px solid #cfc9bb; background: #fff; }
ol.steps li.done { color: #2b2a27; font-weight: 700; }
ol.steps li.done::before { background: #2f5d34; border-color: #2f5d34; }
ol.steps li span { display: block; font-size: 14px; font-weight: 400; color: #6a665d; margin-top: 3px; }
"""


def _page(title: str, body: str) -> bytes:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{_CSS}</style></head><body>"
        "<header><b>Northgate Ceramic Supply</b><span>Trade orders for studios and schools</span></header>"
        f"{body}</body></html>"
    ).encode()


def _price_list() -> str:
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>${v:.2f}</td></tr>" for k, v in PRICES.items()
    )
    return (
        f"<aside><h2>Trade prices</h2><table>{rows}</table>"
        '<p class="note">Prices per unit, before delivery. Orders placed before 14:00 are packed the same day.</p></aside>'
    )


def _form() -> bytes:
    items = "".join(f"<option>{html.escape(k)}</option>" for k in PRICES)
    ways = "".join(f"<option>{html.escape(k)}</option>" for k in DELIVERIES)
    return _page(
        "Northgate Ceramic Supply: trade order form",
        "<main><section><h1>Trade order form</h1>"
        '<p class="lead">Tell us what you need. We confirm every order by email.</p>'
        '<form method="post" action="/orders">'
        '<div class="row"><div class="f"><label for="studio">Studio or school</label>'
        '<input id="studio" name="studio" autocomplete="off"></div>'
        '<div class="f"><label for="email">Contact email</label>'
        '<input id="email" name="email" type="email" autocomplete="off"></div></div>'
        '<div class="f"><label for="item">Item</label>'
        f'<select id="item" name="item"><option value="">Choose an item</option>{items}</select></div>'
        '<div class="row"><div class="f"><label for="quantity">Quantity</label>'
        '<input id="quantity" name="quantity" type="number" min="1" max="200"></div>'
        '<div class="f"><label for="delivery">Delivery</label>'
        f'<select id="delivery" name="delivery">{ways}</select></div></div>'
        '<div class="f"><label for="notes">Notes for the warehouse</label>'
        '<textarea id="notes" name="notes" rows="2"></textarea></div>'
        '<button type="submit">Place order</button></form></section>'
        f"{_price_list()}</main>",
    )


class _Supplier:
    """The made-up supplier's site: an order form, the orders placed on it, an order's status."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def place(self, fields: dict[str, str]) -> str | None:
        item = fields.get("item", "")
        try:
            quantity = int(fields.get("quantity", ""))
        except ValueError:
            return None
        if item not in PRICES or not 1 <= quantity <= 200 or not fields.get("studio"):
            return None
        with self._lock:
            ref = f"NG-{2041 + len(self.orders)}"
            self.orders[ref] = {
                "studio": fields["studio"],
                "email": fields.get("email", ""),
                "item": item,
                "quantity": quantity,
                "delivery": fields.get("delivery", DELIVERIES[0]),
                "notes": fields.get("notes", ""),
                "total": PRICES[item] * quantity,
            }
        return ref

    def confirmation(self, ref: str) -> bytes:
        o = self.orders[ref]
        e = html.escape
        rows = (
            f"<tr><td>{e(o['item'])}</td><td>{o['quantity']} x ${PRICES[o['item']]:.2f}</td></tr>"
            f"<tr><td>{e(o['delivery'])}</td><td>included</td></tr>"
            f"<tr><td><b>Total</b></td><td>${o['total']:.2f}</td></tr>"
        )
        notes = f'<p class="note">Note for the warehouse: {e(o["notes"])}</p>' if o["notes"] else ""
        return _page(
            f"Order {ref} received: Northgate Ceramic Supply",
            '<main><section><span class="ok">Order received</span>'
            f"<h1>Thank you, {e(o['studio'])}</h1>"
            f'<p class="ref">Your order reference is <b>{ref}</b></p>'
            f'<p class="lead">A confirmation is on its way to {e(o["email"])}.</p>'
            f'<a class="btn" id="track" href="/orders/{ref}/status">Track this order</a></section>'
            f"<aside><h2>Order {ref}</h2><table>{rows}</table>{notes}</aside></main>",
        )

    def status(self, ref: str) -> bytes:
        o = self.orders[ref]
        return _page(
            f"Order {ref} status: Northgate Ceramic Supply",
            f"<main><section><h1>Order {ref}</h1>"
            f'<p class="lead">{o["quantity"]} x {html.escape(o["item"])}, for {html.escape(o["studio"])}.</p>'
            '<ol class="steps"><li class="done">Received<span>We have your order.</span></li>'
            "<li>Packed<span>Orders placed before 14:00 are packed the same day.</span></li>"
            "<li>Out for delivery<span>We email you the morning it leaves.</span></li>"
            "<li>Delivered</li></ol>"
            f'<a class="btn" href="/orders/{ref}">Back to the order</a></section>'
            f"{_price_list()}</main>",
        )

    def serve(self) -> None:
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, body: bytes, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parts = [p for p in self.path.split("?")[0].split("/") if p]
                if not parts:
                    self._send(_form())
                elif parts[0] == "orders" and len(parts) >= 2 and parts[1] in site.orders:
                    status = len(parts) == 3 and parts[2] == "status"
                    self._send(site.status(parts[1]) if status else site.confirmation(parts[1]))
                else:
                    self._send(
                        _page("Not found", "<main><section><h1>Not found</h1></section></main>"),
                        404,
                    )

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                ref = site.place(fields) if self.path == "/orders" else None
                if ref is None:
                    self._send(_form(), 400)
                    return
                self.send_response(303)
                self.send_header("Location", f"/orders/{ref}")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()


SUPPLIER = _Supplier()
SITE = {"url": ""}  # set in after_seed, once the docker network's gateway is known
NAVIGATE = {"url": ""}  # the browser_navigate arguments the chat shows: the same address


@contextlib.contextmanager
def _as_agent(agent: str) -> Iterator[None]:
    """The context a tool call of this agent runs in: its identity and its workspace."""
    from olano.security.request_context import push_request_context
    from olano.tools._credentials import agent_runtime_context

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / agent / "workspace"
    with (
        agent_runtime_context(agent, {}),
        push_request_context(metadata={"workspace_root": str(workspace)}),
    ):
        yield


def read_supplier(ctx: dict) -> str:
    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
    return (workspace / "suppliers" / "northgate.md").read_text(encoding="utf-8")


def navigate(ctx: dict) -> str:
    """The agent's ``browser_navigate`` call, for real."""
    from olano.tools.browser import browser_navigate

    with _as_agent(ctx["agent"]):
        return str(browser_navigate.invoke({"url": SITE["url"]}))


FIELDS = [
    {"selector": "#studio", "value": STUDIO},
    {"selector": "#email", "value": CONTACT},
    {"selector": "#item", "value": ITEM},
    {"selector": "#quantity", "value": QUANTITY},
    {"selector": "#delivery", "value": DELIVERY},
]


def fill(ctx: dict) -> str:
    """The agent's ``browser_fill`` call, for real: five fields, nothing submitted."""
    from olano.tools.browser import browser_fill

    with _as_agent(ctx["agent"]):
        return str(browser_fill.invoke({"fields": FIELDS}))


CLICK = {"selector": "text=Place order"}


class _Click:
    """``browser_click`` on the form's submit button, inside a one-node graph (see the module docstring)."""

    def __init__(self) -> None:
        self.graph: Any = None
        self.config: dict[str, Any] = {}

    def ask(self, ctx: dict) -> list[dict[str, Any]]:
        """Run the click. The guard stops it; return the approval it asks for, as the runner would."""
        import uuid
        from types import SimpleNamespace
        from typing import TypedDict

        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph

        from olano.core.runner import AgentRunner, _classify_interrupt_action_risk
        from olano.tools.browser import browser_click

        class State(TypedDict, total=False):
            result: str

        agent = ctx["agent"]

        def click(_: State) -> State:
            with _as_agent(agent):
                return {"result": str(browser_click.invoke(CLICK))}

        builder = StateGraph(State)
        builder.add_node("browser_click", click)
        builder.add_edge(START, "browser_click")
        builder.add_edge("browser_click", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.config = {"configurable": {"thread_id": uuid.uuid4().hex}}
        self.graph.invoke({}, self.config)
        state = self.graph.get_state(self.config)
        actions = AgentRunner._extract_interrupt_actions(
            SimpleNamespace(interrupts=state.interrupts)
        )
        for action in actions:
            action.setdefault("risk", _classify_interrupt_action_risk(action))
        return actions

    def result(self, ctx: dict) -> str:
        """Resume with the person's decision: approved, the click goes through."""
        from langgraph.types import Command

        decisions = ctx.get("decisions") or [{"type": "reject"}]
        out = self.graph.invoke(Command(resume={"decisions": list(decisions)}), self.config)
        return str(out.get("result", ""))


PLACE = _Click()


def summary(ctx: dict) -> str:
    """The agent's closing message, from what the supplier's server recorded."""
    if not SUPPLIER.orders:
        return "I did not place the order: the last step was not approved, so nothing was sent."
    ref, order = next(reversed(SUPPLIER.orders.items()))
    return (
        f"Done. Northgate confirmed the order.\n\n"
        f"- Reference: **{ref}**\n"
        f"- {order['quantity']} bags of buff stoneware clay\n"
        f"- Total: ${order['total']:.2f}, standard delivery\n"
    )


TEAM = {
    "default_agent": "wren",
    "agents": [
        {
            "name": "wren",
            "emoji": "🏺",
            "description": "Orders clay and glazes, and keeps the studio stocked.",
            "title": "Supplies",
            "manager": "hazel",
        },
        {
            "name": "hazel",
            "emoji": "🗓️",
            "description": "Runs the studio's week: classes, kiln firings and the rota.",
            "title": "Studio Manager",
            "manager": None,
            "reports": ["wren", "pip"],
        },
        {
            "name": "pip",
            "emoji": "🛍️",
            "description": "Looks after the online shop and its orders.",
            "title": "Online Shop",
            "manager": "hazel",
        },
    ],
    "files": {
        "wren": {
            "AGENTS.md": "# wren\n\nSupplies at Thistledown Pottery. Supplier details are in suppliers/.\n",
            "stock/clay.md": "# Clay in stock\n\n- Buff stoneware: 2 bags (reorder at 3)\n- Porcelain: 5 bags\n",
        },
        "hazel": {"AGENTS.md": "# hazel\n\nStudio manager at Thistledown Pottery.\n"},
        "pip": {"AGENTS.md": "# pip\n\nOnline shop at Thistledown Pottery.\n"},
    },
    "turns": {
        "wren": [
            {
                "steps": [
                    {"wait": 0.7},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "suppliers/northgate.md"},
                        "result": read_supplier,
                        "took": 0.9,
                    },
                    {
                        "tool": "browser_navigate",
                        "args": NAVIGATE,
                        "result": navigate,
                        "took": 0.4,
                    },
                    {"wait": 5.5},
                    {
                        "tool": "browser_fill",
                        "args": {"fields": FIELDS},
                        "result": fill,
                        "took": 0.4,
                    },
                    {"wait": 3.2},
                    {
                        # Short lines: the browser window sits over the right of the conversation.
                        "text": "The form is filled in: 8 bags, standard delivery.\n\n"
                        "Placing the order needs your approval.\n\n",
                        "wps": 14,
                    },
                    {
                        "tool": "browser_click",
                        "args": CLICK,
                        "ask": PLACE.ask,
                        "result": PLACE.result,
                        "took": 0.2,
                    },
                    {"text": summary, "wps": 14},
                ]
            }
        ],
    },
}


def _remove_browser_container() -> None:
    """Remove Wren's browser container, and only that one.

    A runtime removes the browser containers nobody owns when it boots (``sidecar.reap_orphans``)
    and closes its sessions when it stops; the launcher does neither, so a take would leave its
    container running. The product's sweep takes every browser container on the machine, which
    could belong to a real install next to this one, so this removes the take's own by name.
    """
    from olano.browser import sidecar

    subprocess.run(
        ["docker", "rm", "-f", sidecar.container_name("wren")], capture_output=True, check=False
    )


def after_seed(home: Path, **_: object) -> None:
    """Start the supplier's site and write its address into the agent's notes."""
    import atexit

    from olano.backends.lionclaw_docker import resolve_bridge_network

    _remove_browser_container()
    atexit.register(_remove_browser_container)

    network = resolve_bridge_network()  # the network the agent's browser container joins
    gateway = subprocess.run(
        ["docker", "network", "inspect", network, "-f", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    SITE["url"] = NAVIGATE["url"] = f"http://{gateway}:{PORT}/"
    SUPPLIER.serve()
    notes = home / "agents" / "wren" / "workspace" / "suppliers" / "northgate.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        "# Northgate Ceramic Supply\n\n"
        f"- Trade order form: {SITE['url']}\n"
        f"- We order as: {STUDIO}, {CONTACT}\n"
        "- Usual order: buff stoneware clay, 12.5 kg bags, standard delivery\n",
        encoding="utf-8",
    )
