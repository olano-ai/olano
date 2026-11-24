"""E2E: the Knowledge base page refuses to dispatch a tool the agent lacks.

The page has no KB backend. Every action is a ``kb_*`` tool call POSTed to
``/api/chat`` as an ordinary message (``views/knowledge/kb.ts``), so an agent
whose ``tools:`` list omits those names does not fail — its model reads
``kb_search("birdhouse", …)`` as a sentence and answers in prose ("I don't have
a kb_search tool connected in this chat"), which the reply parsers then render
under a **Results** header as though it were a hit list.

``kb_*`` are not universal defaults (``config/ops_defaults.py``), so most agents
are in exactly that state. These tests pin the guard: with the tools missing the
page says so and disables the dispatch; with them present nothing is in the way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

AGENT = "merli"

# Every kb_* name the page dispatches — mirrors KB_TOOL_NAMES in kb.ts.
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


def _write_agent_tools(tools: list[str]) -> None:
    """Rewrite the active home's ``merli`` config with exactly these extra tools.

    Ordered after the autouse ``_seed_agents_into_the_active_home`` (a plain
    fixture body runs after the autouse ones at the same scope), so this wins
    over the seeded copy — which carries no ``tools`` at all, i.e. the missing
    case these tests are about.
    """
    home = Path(os.environ["LIONCLAW_HOME"])
    path = home / "agents" / AGENT / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"name": AGENT}
    data["tools"] = tools
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _open_knowledge(page, dashboard: harness.RunningDashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/knowledge", wait_until="domcontentloaded")
    wait_for_app_shell(page)


def test_missing_kb_tools_blocks_the_dispatch(page, dashboard) -> None:
    """No kb_* tools on the agent ⇒ the notice names them and Search is dead.

    The query box is filled first: the button is also disabled on an empty
    query, so without typing this would pass whether or not the guard works.
    """
    _write_agent_tools([])
    _open_knowledge(page, dashboard)

    notice = page.locator("[data-testid='kb-missing-tools']")
    notice.wait_for(state="visible", timeout=15_000)
    # It names the agent that can't serve the page, and the tools it is missing
    # — the two facts needed to fix it.
    assert AGENT in notice.inner_text()
    for tool in KB_TOOLS:
        assert tool in notice.inner_text(), f"{tool} not listed as missing"

    page.locator("input[placeholder='Search across the knowledge base…']").fill("birdhouse")
    submit = page.locator("[data-testid='kb-search-submit']")
    assert submit.is_disabled(), "Search dispatched to an agent with no kb_search"


def test_present_kb_tools_leave_the_page_usable(page, dashboard) -> None:
    """The same page, with the tools granted: no notice, dispatch enabled."""
    _write_agent_tools(KB_TOOLS)
    _open_knowledge(page, dashboard)

    # The search card is the "page mounted" signal; the notice is absent only
    # once the availability probe has answered, so wait on the card first.
    page.locator("section.card:has-text('Semantic search across a store')").wait_for(
        state="visible", timeout=15_000
    )
    page.locator("input[placeholder='Search across the knowledge base…']").fill("birdhouse")

    submit = page.locator("[data-testid='kb-search-submit']")
    submit.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "() => { const b = document.querySelector(\"[data-testid='kb-search-submit']\");"
        " return b && !b.disabled; }",
        timeout=15_000,
    )
    assert page.locator("[data-testid='kb-missing-tools']").count() == 0
