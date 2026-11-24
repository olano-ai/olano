"""E2E: the sidebar's per-agent chat list, the "open the latest conversation"
rule, the fleet-wide Cortex tabs and the profile menu's dropdown typography.

Four behaviours only a real browser can see, at both viewports:

* **Clicking an agent opens its latest HUMAN conversation.** The newest thread
  a person is in — typed on the dashboard or arrived over a messenger — wins
  over a newer scheduled run, and the choice lands in ``?thread=`` so a
  refresh keeps it. Before this every visit greeted the reader with an empty
  composer.
* **The selected agent unfolds into its chats** in the sidebar: New chat, the
  source filter chips, the rows with their provenance pills; right-click
  renames in place; the chevron folds without navigating (on a phone the
  drawer stays open, which is how an agent's chats are browsed from it).
* **Cortex lists every agent's items, each tagged with its owner**, and the
  agents filter narrows them client-side.
* **The profile menu's theme/language dropdowns are typeset like its rows** —
  the touch-device 16px floor for text controls (an iOS focus-zoom guard)
  used to catch them too, and a 16px value beside 13px rows read as a foreign
  control pasted into the panel.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIDEBAR, is_mobile_viewport, open_mobile_nav

pytestmark = pytest.mark.e2e

AGENT = "merli"
TG_ID = "agent:main:telegram:group:-1001"
DASH_ID = "3f2c9b6e-1111-4d0e-9a7b-000000000001"
SCHED_ID = "scheduled_merli_ab12cd34@20260906T0100"

ROW = "[data-testid='nav-thread-item']"


def _seed_transcripts(agent: str) -> None:
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def rows(ts, text, reply, channel, sender=None):
        user = {"ts": ts.isoformat(), "role": "user", "content": text, "channel": channel}
        if sender:
            user["sender"] = sender
        return [
            user,
            {"ts": (ts + timedelta(seconds=5)).isoformat(), "role": "assistant", "content": reply},
        ]

    # Oldest → newest: a dashboard chat, a Telegram chat, then a scheduled run
    # newer than both. The Telegram chat is the one that must open.
    seeds = {
        DASH_ID: rows(
            now - timedelta(days=2), "Plan the launch email", "Here is a draft.", "dashboard"
        ),
        TG_ID: rows(
            now - timedelta(hours=6),
            "can you list the top 3 tasks",
            "1. Deck 2. Vendor 3. Digest",
            "telegram",
            "Lin",
        ),
        SCHED_ID: rows(
            now - timedelta(hours=1),
            "[Autonomous scheduled task ab12cd34] Daily digest",
            "Digest posted.",
            "scheduler",
        ),
    }
    for tid, entries in seeds.items():
        (sessions / f"{tid}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in entries) + "\n", encoding="utf-8"
        )


def _thread_in_url(page) -> str:
    for part in unquote(urlsplit(page.url).query).split("&"):
        if part.startswith("thread="):
            return part[len("thread=") :]
    return ""


def _open_sidebar(page) -> None:
    if is_mobile_viewport(page):
        open_mobile_nav(page)


def test_clicking_an_agent_opens_its_latest_human_conversation(page, dashboard) -> None:
    _seed_transcripts(AGENT)
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _open_sidebar(page)
    page.locator(f"{SIDEBAR} .bot-row", has_text=AGENT).first.click()

    # The Telegram conversation — newer than the dashboard chat, older than
    # the scheduled run — is the one on screen, and the URL says so.
    page.wait_for_url(lambda url: TG_ID in unquote(urlsplit(url).query), timeout=15_000)
    page.locator(".bubble.user", has_text="top 3 tasks").first.wait_for(
        state="visible", timeout=15_000
    )
    assert _thread_in_url(page) == TG_ID

    # The sidebar unfolded the agent's chats: New chat, chips, the three rows.
    _open_sidebar(page)
    page.locator(ROW).first.wait_for(state="visible", timeout=10_000)
    assert page.locator(ROW).count() == 3
    assert page.locator("[data-testid='nav-thread-new']").is_visible()
    chips = page.locator(f"{SIDEBAR} .thread-filter-chip")
    assert chips.count() == 4, [
        c.inner_text() for c in chips.all()
    ]  # All · Chats · Telegram · Scheduled
    chips.filter(has_text="Scheduled").first.click()
    assert page.locator(ROW).count() == 1
    assert page.locator(ROW).first.locator(".thread-origin-scheduled").count() == 1
    chips.filter(has_text="All").first.click()

    # Right-click → Rename…, in place; the new name shows once the server agrees.
    page.locator(ROW, has_text="launch email").first.click(button="right")
    page.get_by_role("menuitem", name="Rename…").click()
    field = page.locator(f"{SIDEBAR} .thread-rename-input")
    field.wait_for(state="visible", timeout=5_000)
    field.fill("Launch checklist")
    field.press("Enter")
    page.locator(ROW, has_text="Launch checklist").first.wait_for(state="visible", timeout=10_000)

    # The chevron folds the list without navigating; on a phone the drawer
    # stays open for the reader to keep browsing.
    chev = (
        page.locator(f"{SIDEBAR} .bot-row-wrap", has_text=AGENT)
        .locator("[data-testid='nav-bot-chevron']")
        .first
    )
    chev.click()
    page.wait_for_timeout(300)
    assert page.locator(ROW).count() == 0
    if is_mobile_viewport(page):
        assert page.locator(f".app-shell.mobile-nav-open {SIDEBAR}").count() == 1
    chev.click()
    page.locator(ROW).first.wait_for(state="visible", timeout=5_000)
    assert _thread_in_url(page) == TG_ID  # still the same conversation

    # Clicking a row opens that conversation.
    page.locator(ROW, has_text="Launch checklist").first.click()
    page.wait_for_url(lambda url: DASH_ID in unquote(urlsplit(url).query), timeout=15_000)
    page.locator(".bubble.user", has_text="launch email").first.wait_for(
        state="visible", timeout=15_000
    )


def test_the_latest_conversation_opens_without_waiting_for_the_whole_list(page, dashboard) -> None:
    """Arrival opens from the newest page of threads, not the full list.

    The full list (`GET …/threads` with no parameters) reads every transcript
    the agent has, seconds on a long-lived agent, and the first open of an
    agent used to wait for all of it before asking for the one thread it
    shows. Here that request never answers usefully (it is aborted), and the
    agent must still open its latest human conversation; on the old code the
    same setup greeted the reader with an empty draft.
    """
    _seed_transcripts(AGENT)
    full_list = re.compile(rf"/api/agents/{AGENT}/threads$")
    page.route(full_list, lambda route: route.abort())
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _open_sidebar(page)
    page.locator(f"{SIDEBAR} .bot-row", has_text=AGENT).first.click()
    page.wait_for_url(lambda url: TG_ID in unquote(urlsplit(url).query), timeout=15_000)
    page.locator(".bubble.user", has_text="top 3 tasks").first.wait_for(
        state="visible", timeout=15_000
    )


def test_an_agent_with_no_chats_starts_a_draft_and_never_a_server_thread(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator(".composer textarea").first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(800)
    assert _thread_in_url(page) == "", page.url
    # Re-arriving cannot manufacture a thread: the list stays empty.
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_timeout(800)
    _open_sidebar(page)
    assert page.locator(ROW).count() == 0
    assert page.locator(f"{SIDEBAR} .bot-threads .bot-section-empty").count() == 1


IDEAS = {
    "agent": "",
    "agents": [AGENT, "ghost"],
    "items": [
        {
            "id": "idea-1",
            "agent": AGENT,
            "kind": "idea",
            "slug": "solar-kiln",
            "title": "Solar kiln",
            "status": "active",
            "summary": "Dry lumber with a passive solar kiln.",
            "tags": [],
            "path": "ideas/solar-kiln",
            "abs_path": "/tmp/ws/ideas/solar-kiln",
            "detail_file": "ideas/solar-kiln/idea.md",
            "source": "autonomous",
            "created_at": "2026-09-04T10:00:00Z",
            "updated_at": "2026-09-04T10:00:00Z",
            "links": {},
        },
        {
            "id": "idea-2",
            "agent": "ghost",
            "kind": "idea",
            "slug": "renewal-nudges",
            "title": "Renewal nudges",
            "status": "active",
            "summary": "A reminder 14 days before renewal.",
            "tags": [],
            "path": "ideas/renewal-nudges",
            "abs_path": "/tmp/ws2/ideas/renewal-nudges",
            "detail_file": "ideas/renewal-nudges/idea.md",
            "source": "autonomous",
            "created_at": "2026-09-05T10:00:00Z",
            "updated_at": "2026-09-05T10:00:00Z",
            "links": {},
        },
    ],
    "counts": {"idea": 2, "brainstorm": 0, "prototype": 0, "project": 0},
    "counts_by_agent": {
        AGENT: {"idea": 1, "brainstorm": 0, "prototype": 0, "project": 0},
        "ghost": {"idea": 1, "brainstorm": 0, "prototype": 0, "project": 0},
    },
}


def test_cortex_lists_every_agents_items_with_owner_tags_and_filters_them(page, dashboard) -> None:
    seen: list[str] = []

    def fulfil(route):
        seen.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(IDEAS))

    page.route(re.compile(r"/api/cortex/items(\?|$)"), fulfil)
    page.goto(dashboard.base_url.rstrip("/") + "/cortex", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator("[data-testid='agent-filter']").wait_for(state="visible", timeout=15_000)
    assert "all agents" in page.locator("[data-testid='cortex-scope-line']").inner_text()
    page.locator(".cortex-tabstrip button", has_text="Ideas").first.click()
    cards = page.locator(".cortex-card")
    cards.first.wait_for(state="visible", timeout=15_000)
    assert cards.count() == 2
    # The page asks for the FLEET, and every card names its owner.
    assert any("all_agents=true" in u for u in seen), seen
    owners = sorted(
        page.locator(".cortex-card .cortex-agent-tag").evaluate_all(
            "els => els.map(e => e.dataset.agent)"
        )
    )
    assert owners == ["ghost", AGENT]

    # Untick the one agent in the roster: its card goes, the other stays, and
    # the tab badge follows — no refetch needed.
    fetches_before = len(seen)
    page.locator("[data-testid='agent-filter'] .chat-chip").click()
    # The popover is portaled to <body> so it can be clamped into the viewport,
    # so its rows are not descendants of the trigger's wrapper.
    page.locator("[data-testid='agent-filter-menu'] .model-item", has_text=AGENT).click()
    page.keyboard.press("Escape")
    page.locator(".cortex-card", has_text="Renewal nudges").first.wait_for(
        state="visible", timeout=5_000
    )
    assert cards.count() == 1
    assert page.locator(".cortex-card .cortex-agent-tag[data-agent='ghost']").count() == 1
    badge = page.locator(".cortex-tabstrip button", has_text="Ideas").locator(".badge").inner_text()
    assert badge.strip() == "1"
    assert len(seen) == fetches_before, "toggling the filter must not refetch the items"


def test_profile_menu_dropdowns_are_typeset_like_its_rows(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    _open_sidebar(page)
    page.locator(f"{SIDEBAR} .profile-btn").first.click()
    menu = page.locator(".profile-menu-wrap .menu")
    menu.wait_for(state="visible", timeout=5_000)
    sizes = menu.locator("select.menu-select").evaluate_all(
        "els => els.map(e => getComputedStyle(e).fontSize)"
    )
    labels = menu.locator(".menu-select-label").evaluate_all(
        "els => els.map(e => getComputedStyle(e).fontSize)"
    )
    rows = menu.locator(".menu-item").evaluate_all(
        "els => els.map(e => getComputedStyle(e).fontSize)"
    )
    assert sizes == ["13px", "13px"], sizes
    assert set(labels) == {"13px"}, labels
    assert set(rows) == {"13px"}, rows
    if page.evaluate("matchMedia('(pointer: coarse)').matches"):
        # The 16px focus-zoom guard still holds for TEXT controls: only the
        # menu's dropdowns are exempt, because they open a picker, not a keyboard.
        page.keyboard.press("Escape")
        size = page.locator(".composer textarea").first.evaluate(
            "el => getComputedStyle(el).fontSize"
        )
        assert size == "16px", size
