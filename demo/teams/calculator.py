"""Lantern Lane Dumplings, a small dumpling kitchen: the team in the calculator video.

Mei keeps the kitchen's books. In the video the owner is hiring a cook and asks what the monthly
CPF contributions will be; Mei opens the CPF Board's public CPF contribution calculator (a real
page that runs in the browser, no sign-in), fills in the numbers the owner gave in the chat, reads
the result and explains it. Every browser step of the scripted turn runs the product's own browser
tool for the agent in the product's headless browser against the live page; the reply is built
from the result text the calculator showed during the take, and the screenshot in the reply is
the one ``browser_screenshot`` saved.

The calculator's Calculate button is named ``submit`` in the page's code, so the browser's
consequence guard stops the click and asks first: that is the approval card in the video. The
guard raises a LangGraph interrupt from inside ``browser_click``; the scripted runner has no
graph, so the click runs in a one-node graph of its own (``_Click``), exactly as in
``teams/browser.py``: the interrupt is read with the runner's own ``_extract_interrupt_actions``
and risk classifier, shown as the chat's approval card, and the person's decision resumes the
graph, which is when the click happens.

The way out to the internet is the same as in ``teams/pricecheck.py``.
"""

from __future__ import annotations

import atexit
import re
from pathlib import Path
from typing import Any

from teams.pricecheck import as_agent, close_browsers, tool

URL = "https://www.cpf.gov.sg/member/tools-and-services/calculators/cpf-contribution-calculator"
WAGE = "3200"
BIRTH = "05/1991"
MONTH = "12/2026"
SHOT = "cpf-contribution-result"

TERMS = {"selector": "text=I have read and accepted the"}
START = {"selector": "button:has-text('Start')"}
CITIZEN = {"selector": "text=Singapore Citizen/3rd year Permanent Resident and above"}
BORN = {"selector": "input[placeholder='MM/YYYY'] >> nth=0", "text": BIRTH}
PAID = {"selector": "input[placeholder='MM/YYYY'] >> nth=1", "text": MONTH}
WAGES = {
    "fields": [
        {"selector": "input[placeholder='00.00'] >> nth=0", "value": WAGE},
        {"selector": "input[placeholder='00.00'] >> nth=1", "value": "0"},
    ]
}
CALCULATE = {"selector": "button:has-text('Calculate')"}


class _Click:
    """``browser_click`` on Calculate, inside a one-node graph (see the module docstring)."""

    def __init__(self) -> None:
        self.graph: Any = None
        self.config: dict[str, Any] = {}
        self.direct = ""

    def ask(self, ctx: dict) -> list[dict[str, Any]]:
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
            with as_agent(agent):
                return {"result": str(browser_click.invoke(CALCULATE))}

        builder = StateGraph(State)
        builder.add_node("browser_click", click)
        builder.add_edge(START, "browser_click")
        builder.add_edge("browser_click", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.config = {"configurable": {"thread_id": uuid.uuid4().hex}}
        out = self.graph.invoke({}, self.config)
        state = self.graph.get_state(self.config)
        if not state.interrupts:  # the guard did not ask: the click already ran
            self.direct = str(out.get("result", ""))
            return []
        actions = AgentRunner._extract_interrupt_actions(
            SimpleNamespace(interrupts=state.interrupts)
        )
        for action in actions:
            action.setdefault("risk", _classify_interrupt_action_risk(action))
        return actions

    def result(self, ctx: dict) -> str:
        from langgraph.types import Command

        if self.direct:
            return self.direct
        decisions = ctx.get("decisions") or [{"type": "reject"}]
        out = self.graph.invoke(Command(resume={"decisions": list(decisions)}), self.config)
        return str(out.get("result", ""))


CALC = _Click()


def _money(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else ""


def read_result(ctx: dict) -> dict[str, str]:
    body = ctx.get("browser_extract", "")
    part = body.split("RESULTS", 1)[-1]
    return {
        "month": _money(part, r"For ([A-Z][a-z]{2} \d{4})"),
        "total": _money(part, r"Total CPF contributions\s*\$([\d,]+\.\d\d)"),
        "employee": _money(part, r"\$([\d,]+\.\d\d)\s*Employee's share"),
        "employer": _money(part, r"\$([\d,]+\.\d\d)\s*Employer's share"),
        "rate": _money(part, r"\((\d+(?:\.\d+)?)% x \$"),
        "employee_rate": _money(part, r"Employee's share\s*\((\d+(?:\.\d+)?)% x"),
        "age": _money(body, r"Age group\s*\n+\s*(.+?)\n"),
    }


def explain(ctx: dict) -> str:
    """Mei's answer, from the result the calculator showed."""
    r = read_result(ctx)
    shot = re.search(r"Screenshot saved: (\S+\.png)", ctx.get("browser_screenshot", ""))
    saved = f"\n\nSaved: {shot.group(1)}" if shot else ""
    if not (r["total"] and r["employee"] and r["employer"]):
        return (
            "The calculator did not show a result I could read, so I will not guess a number. "
            f"I can run it again.{saved}"
        )
    wage = float(WAGE)
    take_home = wage - float(r["employee"].replace(",", ""))
    month = f" for {r['month']}" if r["month"] else ""
    rates = ""
    if r["rate"] and r["employee_rate"]:
        rates = (
            f" The calculator used a total rate of {r['rate']}% of the wage, "
            f"{r['employee_rate']}% of the wage from Wei Jie"
            + (f" (age group: {r['age'].lower()})." if r["age"] else ".")
        )
    return (
        f"The CPF Board's calculator gives, for a S${wage:,.0f} monthly wage{month}:\n\n"
        "| | Each month |\n|---|---|\n"
        f"| Wei Jie's share (taken from his pay) | S${r['employee']} |\n"
        f"| Our share (paid on top) | S${r['employer']} |\n"
        f"| **Total into his CPF** | **S${r['total']}** |\n\n"
        f"In plain words: S${r['employee']} comes out of Wei Jie's pay, so he takes home "
        f"S${take_home:,.2f} before income tax. We add S${r['employer']} on top of his wage. "
        f"Both shares go into his CPF account together each month.{rates}"
        f"{saved}"
    )


def _step(name: str, args: dict[str, Any], took: float = 0.3) -> dict[str, Any]:
    return {"tool": name, "args": args, "result": tool(name, args), "took": took}


TEAM = {
    "default_agent": "mei",
    "agents": [
        {
            "name": "mei",
            "emoji": "🥟",
            "description": "Back office: payroll, suppliers and licences.",
            "title": "Back Office",
            "manager": None,
            "reports": ["bao", "lin"],
        },
        {
            "name": "bao",
            "emoji": "🧾",
            "description": "Takes catering orders and confirms pick-up times.",
            "title": "Orders",
            "manager": "mei",
        },
        {
            "name": "lin",
            "emoji": "🥬",
            "description": "Orders vegetables, flour and pork for the week.",
            "title": "Kitchen Supplies",
            "manager": "mei",
        },
    ],
    "files": {
        "mei": {
            "AGENTS.md": "# mei\n\nBack office at Lantern Lane Dumplings, a small dumpling kitchen.\n",
        },
        "bao": {"AGENTS.md": "# bao\n\nOrders at Lantern Lane Dumplings.\n"},
        "lin": {"AGENTS.md": "# lin\n\nKitchen supplies at Lantern Lane Dumplings.\n"},
    },
    "turns": {
        "mei": [
            {
                "steps": [
                    {"wait": 0.6},
                    _step("browser_navigate", {"url": URL, "wait_for": "load"}),
                    {"wait": 1.2},
                    _step("browser_click", TERMS),
                    _step("browser_click", START),
                    {"wait": 0.8},
                    _step("browser_click", CITIZEN),
                    _step("browser_type", BORN),
                    _step("browser_press_key", {"key": "Tab"}),
                    _step("browser_type", PAID),
                    _step("browser_press_key", {"key": "Tab"}),
                    _step("browser_fill", WAGES),
                    {"wait": 0.6},
                    {
                        "tool": "browser_click",
                        "args": CALCULATE,
                        "ask": CALC.ask,
                        "result": CALC.result,
                        "took": 0.2,
                    },
                    {"wait": 1.2},
                    _step("browser_extract", {"selector": "body"}),
                    _step("browser_screenshot", {"filename": SHOT, "full_page": False}),
                    {"wait": 0.6},
                    {"text": explain, "wps": 16},
                ]
            },
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    """The browser in Headless mode, as an administrator set it in Config, Browser."""
    cfg = home / "config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "browser:\n  mode: headless\n", encoding="utf-8"
    )
    atexit.register(close_browsers)
