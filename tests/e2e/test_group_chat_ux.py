"""E2E: the group-chat surfaces are actually reachable in a browser.

This suite exists because of how the feature failed. Every layer of group chat
worked and had unit tests — store, routes, tools, React components — and none
of it could be reached, because the capability that gates the UI defaulted off
with no control anywhere to turn it on. Passing tests either side of a switch
nobody can flip is exactly the shape of that bug, so what is driven here is the
part a browser can see and an in-process test cannot:

1. **Config → Group chats renders both switches.** The card is what makes the
   feature switchable at all, and the two switches have to appear together —
   "several bots share a conversation" and "bots answer each other unattended"
   are separate decisions with very different costs.
2. **The simple shell draws the group surfaces** — the rail's Group chats
   section with its New group button, and **Add an agent** in a 1:1 header — and
   draws none of them once an operator turns the feature off.
3. **A group conversation carries its own controls** — the quiet/normal/verbose
   cycle (starting on Normal, unlike a 1:1 chat) and a rename that works
   without a right-click, since a phone has no such gesture.

Both run at both breakpoints (the `page` fixture is parametrized), because the
chat header is where the phone runs out of width and a button that only exists
on desktop is not a shipped button.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import SIMPLE_BOT_RAIL, SIMPLE_ROOT, open_simple_rail

pytestmark = pytest.mark.e2e

# The card carries a phrase no other Config card does.
GROUPS_CARD = "section.card:has-text('Group chats')"
CASCADE_LABEL = "Let bots reply to each other"


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _set_groups(page, dashboard, enabled: bool) -> None:
    """Flip the feature the way an operator does — through the Config save.

    Deliberately not `set_group_thread_settings` directly: `/api/status` caches
    capabilities for a few seconds, and the PUT is what drops that cache. A test
    that writes the file behind the route reads a stale capability and then
    blames the UI — which is exactly what the first draft of this suite did.
    """
    resp = page.request.put(
        dashboard.base_url.rstrip("/") + "/api/system/config",
        data={"groups": {"enabled": enabled}},
    )
    assert resp.ok, f"config save failed: {resp.status} {resp.text()}"


def _seed_second_bot(name: str = "nova") -> None:
    """The harness boots exactly one agent (``merli``), and "add a bot to this
    chat" is correctly hidden with nobody to add — so a second one has to exist
    on disk before the action can be driven. The roster's disk pass picks it up.
    """
    import json
    import os
    from pathlib import Path

    home = Path(os.environ["LIONCLAW_HOME"])
    path = home / "agents" / name / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": name,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "description": "A second bot, so a group can have two.",
                "emoji": "🌟",
                "backend": {"type": "filesystem", "root_dir": str(home / "workspace")},
            }
        ),
        encoding="utf-8",
    )


def _open_simple(page, dashboard):
    page.goto(dashboard.base_url.rstrip("/") + "/", wait_until="domcontentloaded")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)


# ── Config → Group chats ─────────────────────────────────────────────────────


def test_the_config_card_renders_with_both_switches(page, dashboard) -> None:
    """The control that was missing. Both switches must appear on one card, and
    the cascade budgets must be visible (disabled, not hidden) so someone
    deciding whether to enable it can see what would bound it."""
    _set_ui_mode("advanced")
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    card = page.locator(GROUPS_CARD).first
    card.wait_for(state="visible", timeout=20_000)
    card.scroll_into_view_if_needed()

    assert card.get_by_text("Enable group chats").first.is_visible()
    assert card.get_by_text(CASCADE_LABEL).first.is_visible()
    assert card.get_by_text("Bot-to-bot turns per message").first.is_visible()
    # On by default (like the surface itself), so its two budgets are live —
    # they only go inert when an operator turns the cascade off.
    hops = card.locator("input[type='number']").nth(2)
    assert not hops.is_disabled()


def test_the_cascade_budgets_sleep_and_wake_with_their_switch(page, dashboard) -> None:
    """The budgets are disabled rather than hidden while the cascade is off:
    someone deciding whether to turn it back on needs to see what would
    bound it. Driven in both directions from the on-by-default state."""
    _set_ui_mode("advanced")
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    card = page.locator(GROUPS_CARD).first
    card.wait_for(state="visible", timeout=20_000)
    card.scroll_into_view_if_needed()

    toggle = card.locator("button[role='switch']").nth(1)  # the cascade toggle
    hops = card.locator("input[type='number']").nth(2)
    hops.wait_for(state="visible", timeout=5_000)
    assert not hops.is_disabled()
    toggle.click()
    page.wait_for_function("el => el.disabled", arg=hops.element_handle(), timeout=5_000)
    toggle.click()
    page.wait_for_function("el => !el.disabled", arg=hops.element_handle(), timeout=5_000)


# ── the simple shell ─────────────────────────────────────────────────────────


def test_the_rail_offers_group_chats(page, dashboard) -> None:
    _set_ui_mode("simple")
    _set_groups(page, dashboard, True)
    _open_simple(page, dashboard)

    open_simple_rail(page)
    page.wait_for_selector(f"{SIMPLE_BOT_RAIL} >> text=Group chats", timeout=10_000)
    assert page.locator(f"{SIMPLE_BOT_RAIL} .simple-newgroup").count() == 1


def test_add_a_bot_opens_the_promote_dialog(page, dashboard) -> None:
    """The 1:1 → group path in a real browser: the header action exists at both
    widths, and the dialog it opens offers the bots that can join.

    The carry-over half is asserted in the next test rather than here, because
    what the toggle does with a real transcript is an API property and is
    covered exhaustively in `tests/test_group_from_thread.py`. What only a
    browser can answer is whether the button survives the phone's header width.
    """
    _set_ui_mode("simple")
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    _open_simple(page, dashboard)

    add = page.get_by_role("button", name="Add an agent").first
    add.wait_for(state="visible", timeout=20_000)
    add.click()

    dialog = page.locator("[data-testid='simple-add-bot']")
    dialog.wait_for(state="visible", timeout=10_000)
    # The seeded bot is offered; the one being talked to is not — it is already
    # in the roster and leads it, so a pick for it would be a no-op.
    assert dialog.get_by_text("nova", exact=True).first.is_visible()
    assert dialog.locator(".simple-pick", has_text="merli").count() == 0


def test_a_conversation_with_nothing_in_it_is_not_offered_carry_over(page, dashboard) -> None:
    """Opening the shell and clicking Add an agent before saying anything: there is
    no transcript, so the dialog drops the question instead of showing a switch
    that would do nothing either way."""
    _set_ui_mode("simple")
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    _open_simple(page, dashboard)

    page.get_by_role("button", name="Add an agent").first.click()
    dialog = page.locator("[data-testid='simple-add-bot']")
    dialog.wait_for(state="visible", timeout=10_000)
    assert dialog.get_by_text("Bring the conversation along").count() == 0


def test_add_a_bot_is_hidden_with_nobody_to_add(page, dashboard) -> None:
    """The harness's default: one agent. An action whose only outcome would be
    a refusal is not drawn."""
    _set_ui_mode("simple")
    _set_groups(page, dashboard, True)
    _open_simple(page, dashboard)
    page.wait_for_timeout(1_000)  # let the roster settle before asserting absence
    assert page.get_by_role("button", name="Add an agent").count() == 0


def _make_group(page, dashboard, name: str = "War room") -> int:
    """Create a group through the API the browser is already authenticated to.

    Driving the New-group dialog would be a test of the dialog; what the tests
    below need is a group that EXISTS so its own surfaces can be driven.
    """
    resp = page.request.post(
        dashboard.base_url.rstrip("/") + "/api/groups",
        data={"name": name, "members": ["merli", "nova"]},
    )
    assert resp.ok, f"group create failed: {resp.status} {resp.text()}"
    return resp.json()["group"]["id"]


def _open_group(page, dashboard, name: str = "War room") -> None:
    _set_ui_mode("simple")
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    _open_simple(page, dashboard)
    _make_group(page, dashboard, name)
    open_simple_rail(page)
    row = page.locator(f"{SIMPLE_BOT_RAIL} .simple-row", has_text=name).first
    row.wait_for(state="visible", timeout=20_000)
    row.click()
    page.wait_for_selector("[data-testid='simple-group-thread']", timeout=20_000)


def test_a_group_carries_the_detail_switch_and_starts_on_normal(page, dashboard) -> None:
    """The control that was missing from the one conversation that needs it
    most: a group is opened to watch bots work together, so it ships the same
    quiet/normal/verbose cycle the 1:1 chat has — and starts on Normal, where
    each tool call gets a line, rather than the 1:1's Quiet."""
    _open_group(page, dashboard)

    btn = page.locator("[data-testid='simple-group-thread'] [data-testid='simple-detail-btn']")
    btn.wait_for(state="visible", timeout=10_000)
    assert btn.get_attribute("data-detail") == "normal"
    btn.click()
    # Cycles onward, and the label is the CURRENT level, not the next one.
    assert btn.get_attribute("data-detail") == "verbose"


def test_a_bot_to_bot_answer_says_whom_it_answers(page, dashboard) -> None:
    """A cascade hop is drawn with two chips that mean different things:
    ``→ nova`` on the message that handed the thread over, and ``↩ merli`` on
    nova's answer, naming whom it answers. The second one exists so a reader
    never has to infer it from the prose — and so the answering bot never has
    to write ``@merli`` into its text, which would read as a hand-off.

    The rows are written straight into the store the runtime reads (same
    process, same ``LIONCLAW_HOME``): the harness's fake runners never mention
    anybody, so no real cascade can produce them here, and the chips render
    from the rows rather than from the loop that normally writes them."""
    from olano.groups.store import get_group_store

    _open_group(page, dashboard)
    groups = page.request.get(dashboard.base_url.rstrip("/") + "/api/groups").json()["groups"]
    gid = next(g["id"] for g in groups if g["name"] == "War room")
    store = get_group_store()
    store.add_message(
        gid,
        author="merli",
        author_kind="agent",
        body="@nova can you check the total?",
        addressee="nova",
    )
    store.add_message(
        gid,
        author="nova",
        author_kind="agent",
        body="Checked — the total is right.",
        reply_to="merli",
    )

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)
    row = page.locator(f"{SIMPLE_BOT_RAIL} .simple-row", has_text="War room").first
    row.wait_for(state="visible", timeout=20_000)
    row.click()
    thread = page.locator("[data-testid='simple-group-thread']")
    thread.wait_for(state="visible", timeout=20_000)

    handoff = thread.locator(".simple-to-chip:not(.simple-reply-chip)", has_text="nova").first
    handoff.wait_for(state="visible", timeout=10_000)
    answer = thread.locator(".simple-reply-chip").first
    answer.wait_for(state="visible", timeout=10_000)
    assert answer.inner_text().strip() == "↩ merli"
    assert "merli" in (answer.get_attribute("title") or "")
    # The hand-off message itself carries no answer chip: it started the
    # exchange rather than answering anybody.
    assert thread.locator(".simple-reply-chip").count() == 1


def test_a_group_can_be_renamed_from_its_own_panel(page, dashboard) -> None:
    """The rename route a phone can actually take. The rail's version is a
    right-click, which a touch viewport does not have — so the group's own info
    panel carries a pencil, and this runs at both widths."""
    _open_group(page, dashboard, "War room")

    panel = page.locator("[data-testid='simple-group-info']")
    if panel.count() == 0:
        # The third column remembers whether it was open, so the header button
        # is "About this group" or "Hide the group's details" depending on
        # where the shell was left. Only open it when it is actually closed.
        page.get_by_role("button", name="About this group").first.click()
    panel.wait_for(state="visible", timeout=10_000)
    panel.get_by_role("button", name="Rename this group").first.click()

    field = panel.locator(".simple-group-rename-input")
    field.wait_for(state="visible", timeout=5_000)
    field.fill("Launch room")
    field.press("Enter")

    # The header of the conversation itself is what proves it landed: the panel
    # renders from the same group object, but the header is the thing a person
    # was looking at when they renamed it.
    page.wait_for_selector(
        "[data-testid='simple-group-thread'] .simple-head-name:text('Launch room')",
        timeout=10_000,
    )


def test_turning_the_feature_off_removes_every_surface(page, dashboard) -> None:
    """An operator who turns it off gets no group pixels at all — not a
    disabled button, not an empty section."""
    _set_ui_mode("simple")
    _seed_second_bot()
    _set_groups(page, dashboard, False)
    _open_simple(page, dashboard)
    page.wait_for_timeout(1_000)

    assert page.get_by_role("button", name="Add an agent").count() == 0
    assert page.locator(".simple-newgroup").count() == 0


def test_a_group_reply_that_names_a_file_renders_it(page, dashboard) -> None:
    """The half of the 1:1 chat's files-in-chat that a group was missing.

    Media is re-derived per READ, against the row's own author (a group has one
    workspace root per member), so this survives the reload — which is what the
    row is asserted after. The file mentions in the prose become links for the
    same reason: the index comes from the same refs.

    The rows are written straight into the store the runtime reads: the
    harness's fake runners never name a file, so no real turn can produce them
    here, and what is under test is the rendering, not the loop that normally
    writes them.
    """
    from olano.groups.store import get_group_store
    from olano.runtime.api import _workspace_root

    png = "reports/group-chart.png"
    # A 1×1 opaque PNG — enough for <img> to decode and report a natural size.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63f8cfc0f01f0005000301b7b3f3e00000000049454e44ae426082"
    )
    root = _workspace_root("merli")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / png).write_bytes(png_bytes)
    (root / "reports" / "group-notes.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    _open_group(page, dashboard, "File room")
    groups = page.request.get(dashboard.base_url.rstrip("/") + "/api/groups").json()["groups"]
    gid = next(g["id"] for g in groups if g["name"] == "File room")
    get_group_store().add_message(
        gid,
        author="merli",
        author_kind="agent",
        body=(f"Saved: {root / png}\n\nThe numbers behind it are in reports/group-notes.csv."),
    )

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)
    row = page.locator(f"{SIMPLE_BOT_RAIL} .simple-row", has_text="File room").first
    row.wait_for(state="visible", timeout=20_000)
    row.click()
    thread = page.locator("[data-testid='simple-group-thread']")
    thread.wait_for(state="visible", timeout=20_000)

    img = thread.locator(".chat-media-img").first
    img.wait_for(state="visible", timeout=20_000)
    assert "/api/files-raw/reports/group-chart.png" in (img.get_attribute("src") or "")
    page.wait_for_function(
        "el => el.complete && el.naturalWidth > 0", arg=img.element_handle(), timeout=15_000
    )

    card = thread.locator(".chat-file-card").first
    card.wait_for(state="visible", timeout=10_000)
    assert "group-notes.csv" in (card.text_content() or "")

    # The mention in the prose is a link, and it is scoped to the MEMBER that
    # named it — a group resolves a workspace per author, never one per group.
    mention = thread.locator(".bubble.assistant a[data-internal]", has_text="group-notes.csv").first
    mention.wait_for(state="visible", timeout=10_000)
    assert "agent=merli" in (mention.get_attribute("href") or "")
