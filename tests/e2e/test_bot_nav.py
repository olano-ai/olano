"""The sidebar's bot list (views/nav/BotNav.tsx) — the simple rail, ported.

Not in ``route_checks.ROUTE_CHECKS``: the list lives inside the sidebar, which is
a closed drawer on mobile, and those checks never open it. Here the render test
runs at both viewports and opens the drawer where it has to; the right-click
flows are desktop-only, because a touch viewport has no right-click (a phone's
long-press is a browser gesture Playwright's mouse cannot emulate faithfully).
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIDEBAR, is_mobile_viewport, open_mobile_nav

pytestmark = pytest.mark.e2e

BOTS = "[data-testid='nav-bot-sections']"
NEW_AGENT = "[data-testid='nav-new-agent']"


def _open_sidebar(page) -> None:
    wait_for_app_shell(page)
    if is_mobile_viewport(page):
        open_mobile_nav(page)
    page.locator(BOTS).wait_for(state="visible", timeout=10_000)


def _row(page, name: str):
    return page.locator(f"{BOTS} .bot-row", has_text=name)


def show_offline(page) -> None:
    """Open All agents' "N offline" fold. A seeded on-disk agent is never
    booted by the harness, so it reads as offline and waits behind that fold
    (views/agents/presence.ts); the fold is closed by default."""
    toggle = page.locator(f"{BOTS} [data-testid='nav-offline-toggle']")
    toggle.wait_for(state="visible", timeout=20_000)
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()


def _menu_item(page, label: str | re.Pattern[str]):
    item = page.get_by_role("menuitem", name=label)
    item.first.wait_for(state="visible", timeout=5_000)
    return item.first


def test_the_bot_list_leads_the_nav_and_more_follows_the_rule(page) -> None:
    """PINNED / ALL AGENTS render inside the sidebar with the roster's one bot,
    the New agent button is offered to the implicit owner directly under the
    search box (outside the list), and Chat has no tab of its own any more —
    the bot row is the door."""
    _open_sidebar(page)
    bots = page.locator(BOTS)
    assert bots.locator(".bot-section[data-section='pinned']").count() == 1
    assert bots.locator(".bot-section[data-section='all']").count() == 1
    _row(page, "merli").wait_for(state="visible", timeout=10_000)
    new_agent = page.locator(NEW_AGENT)
    assert new_agent.count() == 1
    # Under Search, above the nav — a sibling of both, not a child of the list.
    assert bots.locator(NEW_AGENT).count() == 0
    order = new_agent.evaluate(
        "el => { const p = el.parentElement; const kids = [...p.children];"
        " return [kids.indexOf(p.querySelector('.sidebar-search')), kids.indexOf(el),"
        " kids.indexOf(p.querySelector('nav.sidebar-nav'))]; }"
    )
    assert 0 <= order[0] < order[1] < order[2], order
    # The tab is gone; only the bot rows link to /chat.
    assert page.locator(f"{SIDEBAR} a.nav-link:not(.bot-row)[href='/chat']").count() == 0
    # No Primary group above the list: the bots lead the nav, then the rule,
    # then the More header.
    assert page.locator(f"{SIDEBAR} .nav-group-primary").count() == 0
    order = bots.evaluate(
        "el => { const nav = el.parentElement; const kids = [...nav.children];"
        " return [kids.indexOf(el), kids.indexOf(nav.querySelector('.nav-divider')),"
        " kids.indexOf(nav.querySelector('.nav-group-header:not(.bot-section-header)'))]; }"
    )
    assert order[0] == 0 and order[0] < order[1] < order[2], order


def test_the_new_agent_dialog_offers_only_usable_models(page, dashboard) -> None:
    """The New agent dialog carries a Model dropdown that lists only what this
    deployment can run right now — the deployment defaults, the Olano tiers and
    any provider holding a key — and pre-selects Default · Turbo whenever it is
    offered. Checked against the catalog the runtime itself serves, with the
    picker's own usability rule, so the two cannot drift apart."""
    _open_sidebar(page)
    page.locator(NEW_AGENT).click()
    select = page.locator("[data-testid='newbot-model']")
    select.wait_for(state="visible", timeout=10_000)
    # The catalog lands after the dialog opens: wait for the loading placeholder
    # to give way to the real list (or to the "nothing connected" note).
    page.wait_for_function(
        "sel => { const el = document.querySelector(sel);"
        " return !!el && ![...el.options].some(o => /Loading/i.test(o.text)); }",
        arg="[data-testid='newbot-model']",
        timeout=20_000,
    )

    providers = page.request.get(dashboard.base_url.rstrip("/") + "/api/catalog/providers").json()
    usable = {
        p["key"]
        for p in providers.get("providers", [])
        if p.get("available") is not False
        and (
            bool(p["auth"].get("connected"))
            if p.get("auth")
            else (p.get("env_set") is True if p.get("env_var") else True)
        )
    }
    alias_targets = {a["value"]: a.get("target") or "" for a in providers.get("model_aliases", [])}
    values = [v for v in select.evaluate("el => [...el.options].map(o => o.value)") if v]
    for value in values:
        provider = value.split(":", 1)[0]
        if provider == "default":
            target = alias_targets.get(value, "")
            assert target.split(":", 1)[0] in usable, (value, target)
        else:
            assert provider in usable, value
    if "default:turbo" in values:
        assert select.input_value() == "default:turbo"
    elif values:
        assert select.input_value() == values[0]
    else:
        # Nothing runnable on this box: the dropdown says so and is inert, and
        # the create will let the server pick.
        assert select.is_disabled()
    page.keyboard.press("Escape")


def test_clicking_a_bot_opens_the_chat_with_it(page) -> None:
    _open_sidebar(page)
    _row(page, "merli").click()
    page.wait_for_url("**/chat/merli**", timeout=15_000)
    page.locator("[data-testid='chat-history-toggle']").wait_for(state="visible", timeout=15_000)


# The right-click flows below are desktop-only: a touch viewport has no
# right-click, and the menu's actions are all reachable on a phone through the
# agent's own page and the chat view.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_the_right_click_menu_pins_and_unpins(page) -> None:
    _open_sidebar(page)
    bots = page.locator(BOTS)
    pinned = bots.locator(".bot-section[data-section='pinned']")
    _row(page, "merli").click(button="right")
    _menu_item(page, "Pin").click()
    pinned.locator(".bot-row", has_text="merli").wait_for(state="visible", timeout=5_000)
    assert bots.locator(".bot-section[data-section='all'] .bot-row", has_text="merli").count() == 0

    _row(page, "merli").click(button="right")
    _menu_item(page, "Unpin").click()
    bots.locator(".bot-section[data-section='all'] .bot-row", has_text="merli").wait_for(
        state="visible", timeout=5_000
    )


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_the_hover_pin_button_pins_and_unpins(page) -> None:
    """The simple rail's pin button, on every row: revealed on hover, it pins
    without the menu and reads its state back as aria-pressed. Desktop-only
    because the reveal is a hover; on a touch viewport the button is simply
    always shown."""
    _open_sidebar(page)
    bots = page.locator(BOTS)
    pinned = bots.locator(".bot-section[data-section='pinned']")

    wrap = bots.locator(".bot-row-wrap", has=page.locator(".bot-row", has_text="merli"))
    wrap.hover()
    pin = wrap.locator(".bot-pin")
    pin.wait_for(state="visible", timeout=5_000)
    assert pin.get_attribute("aria-pressed") == "false"
    pin.click()
    pinned.locator(".bot-row", has_text="merli").wait_for(state="visible", timeout=5_000)

    wrap = bots.locator(".bot-row-wrap", has=page.locator(".bot-row", has_text="merli"))
    wrap.hover()
    pin = wrap.locator(".bot-pin")
    pin.wait_for(state="visible", timeout=5_000)
    assert pin.get_attribute("aria-pressed") == "true"
    pin.click()
    bots.locator(".bot-section[data-section='all'] .bot-row", has_text="merli").wait_for(
        state="visible", timeout=5_000
    )


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_right_click_anywhere_on_the_sidebar_offers_a_new_group(page) -> None:
    """The gesture is not confined to the list: the brand row at the very top
    of the sidebar — well outside the bot list — opens the same New group… menu,
    and so does the More header at the bottom."""
    _open_sidebar(page)
    bots = page.locator(BOTS)

    page.locator(f"{SIDEBAR} .brand").click(button="right", position={"x": 4, "y": 4})
    _menu_item(page, re.compile(r"^New group")).click()
    field = bots.locator(".bot-group-rename")
    field.wait_for(state="visible", timeout=5_000)
    field.press("Escape")
    field.wait_for(state="detached", timeout=5_000)

    page.locator(f"{SIDEBAR} button.nav-group-header:not(.bot-section-header)").click(
        button="right"
    )
    _menu_item(page, re.compile(r"^New group")).click()
    bots.locator(".bot-group-rename").wait_for(state="visible", timeout=5_000)
    page.keyboard.press("Escape")


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_see_all_chats_opens_the_chat_view_with_its_rail(page) -> None:
    _open_sidebar(page)
    _row(page, "merli").click(button="right")
    _menu_item(page, "See all chats").click()
    page.wait_for_url("**/chat**", timeout=15_000)
    page.locator("aside.chat-rail").wait_for(state="visible", timeout=15_000)
    # The one-shot flag is consumed: a copied link must not reopen the rail.
    page.wait_for_function("() => !location.search.includes('history')", timeout=10_000)


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_groups_are_made_renamed_filled_and_only_deleted_when_empty(page) -> None:
    _open_sidebar(page)
    bots = page.locator(BOTS)

    # New group from the list's empty space (the container's own menu).
    bots.click(button="right", position={"x": 20, "y": 6})
    _menu_item(page, re.compile(r"^New group")).click()
    field = bots.locator(".bot-group-rename")
    field.wait_for(state="visible", timeout=5_000)
    field.fill("Sales")
    field.press("Enter")
    sales = bots.locator(".bot-section", has=page.locator(".bot-section-header", has_text="Sales"))
    sales.wait_for(state="visible", timeout=5_000)

    # A bot moves in through its own menu, and lands only there.
    _row(page, "merli").click(button="right")
    _menu_item(page, "Move to “Sales”").click()
    sales.locator(".bot-row", has_text="merli").wait_for(state="visible", timeout=5_000)
    assert bots.locator(".bot-section[data-section='all'] .bot-row", has_text="merli").count() == 0

    # Delete is offered but greyed with the reason while the group holds a bot.
    sales.locator(".bot-section-header").click(button="right")
    delete = _menu_item(page, "Delete group")
    assert delete.get_attribute("aria-disabled") == "true"
    assert "empty" in (delete.get_attribute("title") or "")
    page.keyboard.press("Escape")

    # Rename in place.
    sales.locator(".bot-section-header").click(button="right")
    _menu_item(page, re.compile(r"^Rename")).click()
    field = bots.locator(".bot-group-rename")
    field.wait_for(state="visible", timeout=5_000)
    field.fill("Customer success")
    field.press("Enter")
    renamed = bots.locator(
        ".bot-section", has=page.locator(".bot-section-header", has_text="Customer success")
    )
    renamed.wait_for(state="visible", timeout=5_000)

    # Move the bot back out, and now the group deletes.
    _row(page, "merli").click(button="right")
    _menu_item(page, "Move to All agents").click()
    bots.locator(".bot-section[data-section='all'] .bot-row", has_text="merli").wait_for(
        state="visible", timeout=5_000
    )
    renamed.locator(".bot-section-header").click(button="right")
    _menu_item(page, "Delete group").click()
    renamed.wait_for(state="detached", timeout=5_000)

    # The arrangement survives a reload — it is this browser's own.
    bots.click(button="right", position={"x": 20, "y": 6})
    _menu_item(page, re.compile(r"^New group")).click()
    field = bots.locator(".bot-group-rename")
    field.fill("Ops")
    field.press("Enter")
    page.reload(wait_until="domcontentloaded")
    _open_sidebar(page)
    page.locator(BOTS).locator(".bot-section-header", has_text="Ops").wait_for(
        state="visible", timeout=10_000
    )


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_the_arrow_keys_walk_the_rows(page) -> None:
    """The simple rail's keyboard rule, kept: Up/Down move between rows, End and
    Home jump the ends, and a section header is never a stop. Walked over the
    rows that can take focus — a greyed "New group chat…" row is skipped, the
    way a disabled button is skipped by Tab."""
    from tests.e2e.test_group_chat_ux import _seed_second_bot

    _seed_second_bot()  # two bots, so there is somewhere to walk to
    page.reload(wait_until="domcontentloaded")
    _open_sidebar(page)
    rows = page.locator(f"{BOTS} .bot-row:not(:disabled)")
    show_offline(page)
    _row(page, "nova").wait_for(state="visible", timeout=20_000)
    n = rows.count()
    assert n >= 2
    rows.first.focus()
    page.keyboard.press("End")
    assert rows.nth(n - 1).evaluate("el => el === document.activeElement")
    page.keyboard.press("Home")
    assert rows.first.evaluate("el => el === document.activeElement")
    page.keyboard.press("ArrowDown")
    assert rows.nth(1).evaluate("el => el === document.activeElement")
    page.keyboard.press("ArrowUp")
    assert rows.first.evaluate("el => el === document.activeElement")
