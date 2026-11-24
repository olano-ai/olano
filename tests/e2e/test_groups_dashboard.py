"""Group chats in the full dashboard: the sidebar's GROUP CHATS section, the
``/groups/:id`` page, and the chat header's "Add a bot".

The simple shell's counterparts are in ``test_group_chat_ux.py``; the helpers
that flip the feature and seed a second bot are borrowed from there, because
the two suites must set the deployment up the same way — a group needs two
bots and the harness boots one.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import is_mobile_viewport, open_mobile_nav
from tests.e2e.test_bot_nav import show_offline
from tests.e2e.test_group_chat_ux import _make_group, _seed_second_bot, _set_groups

pytestmark = pytest.mark.e2e

CHATS = "[data-testid='nav-bot-sections'] [data-section='chats']"
THREAD = "[data-testid='simple-group-thread']"
CHIP = "[data-testid='chat-add-bot']"


def _goto(page, dashboard, suffix: str) -> None:
    page.goto(dashboard.base_url.rstrip("/") + suffix, wait_until="domcontentloaded")
    wait_for_app_shell(page)


def _open_sidebar(page) -> None:
    if is_mobile_viewport(page):
        open_mobile_nav(page)


def test_the_sidebar_lists_group_chats_and_opens_one(page, dashboard) -> None:
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    gid = _make_group(page, dashboard, "War room")

    _goto(page, dashboard, "/overview")
    _open_sidebar(page)
    row = page.locator(f"{CHATS} .bot-row", has_text="War room")
    row.wait_for(state="visible", timeout=20_000)
    row.click()
    page.wait_for_url(f"**/groups/{gid}", timeout=20_000)
    page.locator(f"{THREAD} .simple-head-name", has_text="War room").wait_for(
        state="visible", timeout=20_000
    )
    # The page is the transcript AND its details: the roster panel opens from
    # the header, with the leading bot marked as the one that answers by
    # default — the same panel the simple shell has, so a phone gets it as a
    # full-screen overlay.
    page.get_by_role("button", name="About this group").first.click()
    panel = page.locator("[data-testid='simple-group-info']")
    panel.wait_for(state="visible", timeout=10_000)
    assert panel.get_by_text("Answers by default").count() == 1


def test_add_a_bot_from_the_chat_header_starts_a_group(page, dashboard) -> None:
    """The 1:1 chat's header promotes the conversation into a group, the way
    the simple shell's header does — and lands on the new group's page, named
    from its roster since nobody was asked for a name."""
    _set_groups(page, dashboard, True)
    _seed_second_bot()

    _goto(page, dashboard, "/chat")
    chip = page.locator(CHIP)
    chip.wait_for(state="visible", timeout=20_000)
    chip.click()
    dialog = page.locator("[data-testid='simple-add-bot']")
    dialog.wait_for(state="visible", timeout=10_000)
    # Nothing has been sent in this chat, so there is no transcript to carry
    # and the switch for it is not offered.
    assert dialog.get_by_text("Bring the conversation along").count() == 0
    dialog.locator(".simple-pick", has_text="nova").click()
    dialog.get_by_role("button", name="Start group chat").click()

    page.wait_for_url(re.compile(r".*/groups/\d+$"), timeout=20_000)
    page.locator(f"{THREAD} .simple-head-name", has_text="merli & nova").wait_for(
        state="visible", timeout=20_000
    )


# Presence/absence checks need no touch interaction, so one viewport is enough.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_the_chip_is_hidden_with_nobody_to_add(page, dashboard) -> None:
    """The harness's default: one agent. An action whose only outcome would be
    a refusal is not drawn."""
    _set_groups(page, dashboard, True)
    _goto(page, dashboard, "/chat")
    page.locator("[data-testid='chat-new']").wait_for(state="visible", timeout=20_000)
    page.wait_for_timeout(1_000)  # let the roster settle before asserting absence
    assert page.locator(CHIP).count() == 0


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_turning_the_feature_off_removes_every_dashboard_surface(page, dashboard) -> None:
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    gid = _make_group(page, dashboard, "War room")
    _set_groups(page, dashboard, False)

    _goto(page, dashboard, "/overview")
    show_offline(page)  # nova is on disk, never booted: it waits behind the fold
    page.locator("[data-testid='nav-bot-sections'] .bot-row", has_text="nova").wait_for(
        state="visible", timeout=20_000
    )
    assert page.locator(CHATS).count() == 0

    _goto(page, dashboard, "/chat")
    page.locator("[data-testid='chat-new']").wait_for(state="visible", timeout=20_000)
    page.wait_for_timeout(1_000)
    assert page.locator(CHIP).count() == 0

    # The page redirects itself rather than rendering a group the deployment
    # says does not exist.
    page.goto(dashboard.base_url.rstrip("/") + f"/groups/{gid}", wait_until="domcontentloaded")
    page.wait_for_url("**/overview", timeout=20_000)


# Right-click is desktop-only: a touch viewport has no right-click, and the
# group's own details panel carries the same rename for phones.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_a_group_chat_renames_in_place_from_its_row(page, dashboard) -> None:
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    _make_group(page, dashboard, "War room")

    _goto(page, dashboard, "/overview")
    row = page.locator(f"{CHATS} .bot-row", has_text="War room")
    row.wait_for(state="visible", timeout=20_000)
    row.click(button="right")
    item = page.get_by_role("menuitem", name=re.compile(r"^Rename"))
    item.first.wait_for(state="visible", timeout=5_000)
    item.first.click()
    field = page.locator(f"{CHATS} .bot-group-rename")
    field.wait_for(state="visible", timeout=5_000)
    field.fill("Launch room")
    field.press("Enter")
    # The server owns the name; the row reflects the reply it sends back.
    page.locator(f"{CHATS} .bot-row", has_text="Launch room").wait_for(
        state="visible", timeout=20_000
    )


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_the_details_panel_adds_and_removes_a_member(page, dashboard) -> None:
    """Participants are edited from the group's own panel: a third bot joins at
    the end of the speaking order, then leaves through its row's button and the
    confirmation that says what is kept. The server owns the roster; the panel
    reflects what it sends back."""
    _set_groups(page, dashboard, True)
    _seed_second_bot()
    _seed_second_bot("orion")
    gid = _make_group(page, dashboard, "War room")

    _goto(page, dashboard, f"/groups/{gid}")
    page.locator(f"{THREAD} .simple-head-name", has_text="War room").wait_for(
        state="visible", timeout=20_000
    )
    page.get_by_role("button", name="About this group").first.click()
    panel = page.locator("[data-testid='simple-group-info']")
    panel.wait_for(state="visible", timeout=10_000)

    panel.locator("select").select_option("orion")
    panel.get_by_role("button", name="Add", exact=True).click()
    panel.locator(".simple-member", has_text="orion").wait_for(state="visible", timeout=20_000)
    # The transcript header counts the roster too.
    page.locator(f"{THREAD} .simple-head-sub", has_text="3 agents").wait_for(
        state="visible", timeout=20_000
    )

    panel.get_by_role("button", name="Remove orion").click()
    page.locator("[role='alertdialog'] button.btn.danger").click()
    panel.locator(".simple-member", has_text="orion").wait_for(state="detached", timeout=20_000)
    page.locator(f"{THREAD} .simple-head-sub", has_text="2 agents").wait_for(
        state="visible", timeout=20_000
    )
