"""E2E: the dashboard's address says where you are, so reload and Back/Forward work.

The agent sits in the path of every per-agent view (`/chat/<agent>?thread=<id>`,
`/agents/<name>/<tab>`), the open item in the query, and moving between places
is a history entry (core/routeUrls.ts). What only a real browser can show:

* **A reload keeps the agent and the thread.** The bug this exists for: the
  selected agent lived only in memory, so a reload fell back to the first agent
  in the roster and opened the thread under it: a 404 and an empty chat.
  Reproduced with the second agent of the roster, which is exactly the case
  that broke.
* **Back/Forward step between conversations and between an agent's tabs**,
  reopening what the URL names.
* **Every link already in circulation still lands**: `?agent=`, `?tab=`, the
  bare `/chat?thread=` a bookmark holds, an agent the roster no longer has.
* **Opening an issue is a Back step**: Back returns to the list.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

FIRST = "merli"  # the running agent: first in the roster
SECOND = "dexter"  # configured on disk only: second in the roster
M_OLD = "aaaaaaaa-0000-4000-8000-000000000001"
M_NEW = "aaaaaaaa-0000-4000-8000-000000000002"
D_THREAD = "dddddddd-0000-4000-8000-000000000001"


def _write_thread(agent: str, thread_id: str, text: str, reply: str, age: timedelta) -> None:
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - age
    rows = [
        {"ts": ts.isoformat(), "role": "user", "content": text, "channel": "dashboard"},
        {"ts": (ts + timedelta(seconds=5)).isoformat(), "role": "assistant", "content": reply},
    ]
    (sessions / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture
def two_agents(dashboard) -> None:
    """A second agent on disk (after merli in the roster) and threads for both."""
    import os
    from pathlib import Path

    home = Path(os.environ["LIONCLAW_HOME"])
    cfg = home / "agents" / SECOND / "agent.json5"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "name": SECOND,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "description": "Second agent for URL routing tests",
                "emoji": "🦊",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            }
        ),
        encoding="utf-8",
    )
    _write_thread(FIRST, M_OLD, "first-agent older question", "older answer", timedelta(days=2))
    _write_thread(FIRST, M_NEW, "first-agent newer question", "newer answer", timedelta(hours=1))
    _write_thread(SECOND, D_THREAD, "second-agent question", "second answer", timedelta(hours=3))


def _base(dashboard) -> str:
    return dashboard.base_url.rstrip("/")


def _path(page) -> str:
    return unquote(urlsplit(page.url).path)


def _thread(page) -> str:
    for part in unquote(urlsplit(page.url).query).split("&"):
        if part.startswith("thread="):
            return part[len("thread=") :]
    return ""


def _bubble(page, text: str):
    return page.locator(".bubble.user", has_text=text).first


# ── the reported bug ─────────────────────────────────────────────────────────


def test_reload_keeps_the_second_agents_thread(page, dashboard, two_agents) -> None:
    page.goto(f"{_base(dashboard)}/chat/{SECOND}?thread={D_THREAD}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _bubble(page, "second-agent question").wait_for(state="visible", timeout=15_000)

    page.reload(wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _bubble(page, "second-agent question").wait_for(state="visible", timeout=15_000)
    assert _path(page) == f"/chat/{SECOND}"
    assert _thread(page) == D_THREAD
    # No "Couldn't open" toast: the thread was opened under its own agent.
    assert page.locator(".toast.error").count() == 0


def test_a_bare_chat_link_lands_on_an_agent_and_keeps_its_thread(
    page, dashboard, two_agents
) -> None:
    # The pre-path spelling every older link and the share target use.
    page.goto(
        f"{_base(dashboard)}/chat?agent={SECOND}&thread={D_THREAD}", wait_until="domcontentloaded"
    )
    wait_for_app_shell(page)
    _bubble(page, "second-agent question").wait_for(state="visible", timeout=15_000)
    page.wait_for_url(re.compile(rf"/chat/{SECOND}\?thread={D_THREAD}$"), timeout=10_000)


# ── Back / Forward ───────────────────────────────────────────────────────────


def test_back_and_forward_step_between_conversations(page, dashboard, two_agents) -> None:
    page.goto(f"{_base(dashboard)}/chat/{FIRST}?thread={M_OLD}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _bubble(page, "first-agent older question").wait_for(state="visible", timeout=15_000)

    # Pick the other conversation from the chat's own history drawer.
    page.locator("[data-testid='chat-history-toggle']").click()
    page.locator("[data-testid='thread-item']", has_text="newer question").first.click()
    _bubble(page, "first-agent newer question").wait_for(state="visible", timeout=15_000)
    page.wait_for_url(lambda url: M_NEW in unquote(url), timeout=10_000)

    page.go_back()
    page.wait_for_url(lambda url: M_OLD in unquote(url), timeout=10_000)
    _bubble(page, "first-agent older question").wait_for(state="visible", timeout=15_000)

    page.go_forward()
    page.wait_for_url(lambda url: M_NEW in unquote(url), timeout=10_000)
    _bubble(page, "first-agent newer question").wait_for(state="visible", timeout=15_000)


def test_back_returns_to_the_previous_agent(page, dashboard, two_agents) -> None:
    page.goto(f"{_base(dashboard)}/chat/{SECOND}?thread={D_THREAD}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _bubble(page, "second-agent question").wait_for(state="visible", timeout=15_000)

    page.goto(f"{_base(dashboard)}/chat/{FIRST}?thread={M_NEW}", wait_until="domcontentloaded")
    _bubble(page, "first-agent newer question").wait_for(state="visible", timeout=15_000)

    page.go_back()
    page.wait_for_url(lambda url: f"/chat/{SECOND}" in unquote(url), timeout=10_000)
    _bubble(page, "second-agent question").wait_for(state="visible", timeout=15_000)
    assert _thread(page) == D_THREAD


def test_agent_tabs_are_addresses_and_back_steps(page, dashboard) -> None:
    page.goto(f"{_base(dashboard)}/agents/{FIRST}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator("#agent-tab-overview[aria-selected='true']").wait_for(
        state="visible", timeout=15_000
    )

    page.locator("#agent-tab-tools").click()
    page.wait_for_url(re.compile(rf"/agents/{FIRST}/tools$"), timeout=10_000)
    page.locator("#agent-tab-tools[aria-selected='true']").wait_for(state="visible", timeout=10_000)

    page.reload(wait_until="domcontentloaded")
    page.locator("#agent-tab-tools[aria-selected='true']").wait_for(state="visible", timeout=15_000)

    page.go_back()
    page.wait_for_url(re.compile(rf"/agents/{FIRST}$"), timeout=10_000)
    page.locator("#agent-tab-overview[aria-selected='true']").wait_for(
        state="visible", timeout=10_000
    )

    page.go_forward()
    page.wait_for_url(re.compile(rf"/agents/{FIRST}/tools$"), timeout=10_000)
    page.locator("#agent-tab-tools[aria-selected='true']").wait_for(state="visible", timeout=10_000)


# ── links already in circulation ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        # Hrefs the runtime stores in notifications (server.py, gmail_watch.py,
        # cognition/worker.py): they must resolve forever.
        (f"/agents/{FIRST}?tab=mcp", rf"/agents/{FIRST}/mcp$"),
        (f"/agents/{FIRST}?tab=connectors", rf"/agents/{FIRST}/connectors$"),
        (f"/agents?agent={FIRST}", rf"/agents/{FIRST}$"),
        (f"/cortex?agent={FIRST}", rf"/cortex/{FIRST}$"),
        (f"/connections?agent={FIRST}", rf"/connections/{FIRST}$"),
        (f"/files?agent={FIRST}", rf"/files/{FIRST}$"),
    ],
)
def test_legacy_links_are_rewritten_to_their_address(page, dashboard, legacy, canonical) -> None:
    page.goto(f"{_base(dashboard)}{legacy}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_url(re.compile(canonical), timeout=15_000)


def test_an_agent_the_roster_does_not_have_falls_back_with_a_notice(page, dashboard) -> None:
    page.goto(f"{_base(dashboard)}/chat/no-such-agent?thread=x", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_url(re.compile(rf"/chat/{FIRST}"), timeout=15_000)
    page.get_by_text("is not in your roster").first.wait_for(state="visible", timeout=10_000)
    # The thread id belonged to the missing agent, so it is not carried over.
    assert _thread(page) != "x"


# ── an opened item is a Back step ────────────────────────────────────────────


def test_back_from_an_open_issue_returns_to_the_list(page, dashboard) -> None:
    base = _base(dashboard)
    title = f"E2E url routing {uuid.uuid4().hex[:8]}"
    resp = page.request.post(f"{base}/api/issues", data={"title": title, "type": "task"})
    assert resp.status == 200, resp.text()
    issue_id = int(resp.json()["issue"]["id"])

    page.goto(f"{base}/issues", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator(".issues-row", has_text=title).first.click()
    page.wait_for_url(lambda url: f"issue={issue_id}" in url, timeout=10_000)

    page.go_back()
    page.wait_for_url(lambda url: "issue=" not in url, timeout=10_000)
    page.locator(".issues-row", has_text=title).first.wait_for(state="visible", timeout=10_000)

    page.go_forward()
    page.wait_for_url(lambda url: f"issue={issue_id}" in url, timeout=10_000)
