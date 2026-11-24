"""E2E: the simple shell's rail grew three things the console already had.

**A notification bell.** Anything that needs a human — a channel that failed to
start, a connector asking to be signed in, an approval — announced itself only
in the dashboard, which is precisely the shell the people it is asking are not
looking at. Same component, same store, in the rail's head.

**A right-click menu on the list's background.** That gesture used to fall
through to the browser's own Back / Reload / Inspect Element menu, which is both
useless here and the one place a person reaches when they want to make something
new. It now offers exactly what the rail can create.

**A More section.** The five dashboard views this shell also offers, below a
divider because they are a second tier: everything above it is a conversation,
everything below is a page. They are the *same* views the dashboard routes to,
selected by the URL rather than by state — which is what these tests check, because
that is what makes Back work, a reload keep the panel, and the bell's own
"View all" link land somewhere real.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from tests.e2e.route_checks import SIMPLE_ROOT, open_simple_rail

pytestmark = pytest.mark.e2e

# The panels, in rail order, with a selector proving the view itself mounted
# rather than just the shell around it.
PANELS = [
    ("Overview", "/overview", "section:has-text('Notifications')"),
    ("Insights", "/insights", "button:has-text('1 day')"),
    ("Issues", "/issues", "button:has-text('New issue')"),
    ("Cortex", "/cortex", "button:has-text('Think now')"),
    ("Files", "/files", "text=Workspace files"),
]


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _open_simple(page, dashboard, path: str = "/"):
    _set_ui_mode("simple")
    page.goto(dashboard.base_url.rstrip("/") + path)
    _mount_and_open_rail(page)


def _reload(page):
    """Reload and get back to a usable rail.

    The wait is not optional and this is the bug it exists for: below the
    breakpoint `open_simple_rail` decides "wide viewport, nothing to open" from
    the *absence* of the drawer's own opener button, so calling it on a page
    that has not finished mounting returns having opened nothing. Every later
    click then lands on a drawer parked off-canvas — which Playwright reports as
    "visible, enabled and stable" and yet "outside of the viewport", retried for
    thirty seconds. It passed on a fast machine and failed on CI.
    """
    page.reload()
    _mount_and_open_rail(page)


def _mount_and_open_rail(page):
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)


def _open_more(page):
    """Expand More. It ships closed, like the dashboard's own More group."""
    open_simple_rail(page)
    more = page.get_by_role("button", name="More", exact=True)
    if more.get_attribute("aria-expanded") == "false":
        more.click()
    page.wait_for_selector(".simple-more-group", timeout=5_000)


def test_the_rail_carries_the_notification_bell(page, dashboard) -> None:
    _open_simple(page, dashboard)
    bell = page.locator(".simple-rail .notification-bell")
    assert bell.count() == 1, "the rail head should carry exactly one bell"
    bell.click()
    panel = page.locator("[role='dialog'][aria-label='Notifications']")
    panel.wait_for(state="visible", timeout=10_000)
    # Portaled and fixed-positioned, so it survives the rail's own clipping —
    # every pixel of it is on screen at either viewport.
    box = panel.bounding_box()
    viewport = page.viewport_size
    assert box is not None
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"] + 1


def test_right_clicking_the_rail_offers_a_new_group(page, dashboard) -> None:
    _open_simple(page, dashboard)
    scroll = page.locator(".simple-rail-scroll")
    box = scroll.bounding_box()
    assert box is not None
    # Well below the last row: the list's own background, not a row.
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] - 30, button="right")
    menu = page.locator(".simple-bot-menu")
    menu.wait_for(state="visible", timeout=5_000)
    assert menu.get_by_role("menuitem", name="New group").count() == 1
    # The rail's other creation action rides along — it is the same gesture in
    # the same empty space, and the alternative is a menu with one item.
    assert menu.get_by_role("menuitem", name="New agent").count() == 1


def test_more_ships_closed_and_lists_the_five_panels(page, dashboard) -> None:
    _open_simple(page, dashboard)
    more = page.get_by_role("button", name="More", exact=True)
    assert more.get_attribute("aria-expanded") == "false", "More should ship collapsed"
    assert page.locator(".simple-more-group").count() == 0
    # The group above More exists but is empty until a panel is dragged out of
    # More, and an empty one takes no space at all.
    assert page.locator(".simple-more-primary .simple-more-item").count() == 0
    more.click()
    # The group, not the rows: five rows resolve ambiguously under strict mode.
    page.wait_for_selector(".simple-more-group", timeout=5_000)
    items = page.locator(".simple-more-item")
    assert [label.strip() for label in items.all_inner_texts()] == [name for name, _, _ in PANELS]
    # Every row is the dashboard route it names, so a middle-click or a copied
    # link behaves like every other link in the app.
    assert [a.get_attribute("href") for a in items.all()] == [path for _, path, _ in PANELS]


@pytest.mark.parametrize("label,path,marker", PANELS, ids=[p[1].strip("/") for p in PANELS])
def test_each_panel_mounts_its_dashboard_view(
    page, dashboard, label: str, path: str, marker: str
) -> None:
    _open_simple(page, dashboard)
    _open_more(page)
    page.get_by_role("link", name=label, exact=True).click()
    # The URL IS the selection — that is what makes Back and a reload work.
    # A panel can address deeper than its link (Cortex names its agent:
    # /cortex/<agent>), so match the panel's own segment and anything below it.
    page.wait_for_url(re.compile(rf"{re.escape(path)}(/[^?#]*)?([?#].*)?$"), timeout=10_000)
    page.wait_for_selector(f".simple-panel[data-simple-panel='{path}']", timeout=10_000)
    page.wait_for_selector(marker, timeout=20_000)
    # The shell is still the shell: the rail did not go anywhere, and the
    # conversation's composer is not mounted underneath the panel.
    assert page.locator(".simple-rail").count() == 1
    assert page.locator("[data-simple-composer]").count() == 0


def test_leaving_a_panel_goes_back_to_the_conversation(page, dashboard) -> None:
    _open_simple(page, dashboard)
    _open_more(page)
    page.get_by_role("link", name="Overview", exact=True).click()
    page.wait_for_url("**/overview", timeout=10_000)
    page.go_back()
    page.wait_for_selector("[data-simple-composer]", timeout=10_000)
    assert page.locator(".simple-panel").count() == 0


def test_picking_an_agent_while_a_panel_is_open_returns_to_the_conversation(
    page, dashboard
) -> None:
    """A panel is the pathname, so it stayed on screen when an agent was tapped
    underneath it: the selection changed, the page did not, and the tap looked
    like it did nothing — on a phone, where the drawer had also closed over
    nothing, it read as the chat being stuck behind Issues. Picking a
    conversation now sends the URL back to "/", which is the conversation, and
    Back still returns to the panel."""
    _open_simple(page, dashboard)
    _open_more(page)
    page.get_by_role("link", name="Issues", exact=True).click()
    page.wait_for_url("**/issues", timeout=10_000)
    page.wait_for_selector(".simple-panel[data-simple-panel='/issues']", timeout=10_000)

    open_simple_rail(page)
    page.locator(".simple-rail button.simple-row", has_text="merli").first.click()
    page.wait_for_selector("[data-simple-composer]", timeout=10_000)
    assert page.locator(".simple-panel").count() == 0
    assert urlparse(page.url).path == "/", page.url
    # On a phone the tap also closed the drawer, the way it does over a chat.
    assert page.locator(f"{SIMPLE_ROOT}.rail-open").count() == 0

    page.go_back()
    page.wait_for_selector(".simple-panel[data-simple-panel='/issues']", timeout=10_000)


def test_more_remembers_whether_it_was_opened(page, dashboard) -> None:
    """Closed on a first visit, and then whatever the human left it as.

    The collapsed default is the point: this shell is a chat app, and five
    operator pages unfurled above the account button on a first load would say
    otherwise. But a reload must not undo a deliberate open, so the state is
    persisted per browser rather than re-defaulted on every mount.
    """
    _open_simple(page, dashboard)
    more = page.get_by_role("button", name="More", exact=True)
    assert more.get_attribute("aria-expanded") == "false", "More must ship collapsed"

    _reload(page)
    more = page.get_by_role("button", name="More", exact=True)
    assert more.get_attribute("aria-expanded") == "false", "still closed — nobody opened it"

    more.click()
    page.wait_for_selector(".simple-more-group .simple-more-item", timeout=5_000)
    _reload(page)
    assert (
        page.get_by_role("button", name="More", exact=True).get_attribute("aria-expanded") == "true"
    ), "an opened More must survive a reload"


def test_a_panel_can_be_moved_out_of_more_and_stays_there(page, dashboard) -> None:
    """The console's own nav split, over this shell's five entries.

    Driven through the context menu rather than a synthetic HTML5 drag: it is
    the same code path (`placeItem` + the same persisted split), it is the only
    path a phone has, and a scripted dragstart/drop sequence tests Playwright's
    event synthesis more than it tests this.
    """
    _open_simple(page, dashboard)
    _open_more(page)
    page.get_by_role("link", name="Overview", exact=True).click(button="right")
    menu = page.locator(".simple-bot-menu")
    menu.wait_for(state="visible", timeout=5_000)
    menu.get_by_role("menuitem").first.click()

    # Out of More and into the group above it, which is a sibling list.
    promoted = page.locator(".simple-more-primary .simple-more-item")
    promoted.first.wait_for(state="visible", timeout=5_000)
    assert [label.strip() for label in promoted.all_inner_texts()] == ["Overview"]

    # It stays out across a reload, and it is no longer inside More.
    _reload(page)
    _open_more(page)
    assert [
        label.strip()
        for label in page.locator(".simple-more-primary .simple-more-item").all_inner_texts()
    ] == ["Overview"]
    inside = page.locator(".simple-more-group .simple-more-item")
    assert "Overview" not in [label.strip() for label in inside.all_inner_texts()]

    # And it survives being sent back — the move is reversible from the same menu.
    page.locator(".simple-more-primary").get_by_role("link", name="Overview", exact=True).click(
        button="right"
    )
    menu = page.locator(".simple-bot-menu")
    menu.wait_for(state="visible", timeout=5_000)
    menu.get_by_role("menuitem").first.click()
    page.wait_for_selector(
        ".simple-more-primary .simple-more-item", state="detached", timeout=5_000
    )


def test_a_promoted_panel_is_visible_with_more_collapsed(page, dashboard) -> None:
    """That is the whole reason to drag one out: More can stay shut and the page
    you actually use stays one click away."""
    _open_simple(page, dashboard)
    _open_more(page)
    page.get_by_role("link", name="Files", exact=True).click(button="right")
    page.locator(".simple-bot-menu").get_by_role("menuitem").first.click()
    page.wait_for_selector(".simple-more-primary .simple-more-item", timeout=5_000)

    page.get_by_role("button", name="More", exact=True).click()  # collapse it
    page.wait_for_selector(".simple-more-group", state="detached", timeout=5_000)
    assert page.locator(".simple-more-primary").get_by_role("link", name="Files").count() == 1


def test_a_route_this_shell_does_not_offer_lands_on_the_conversation(page, dashboard) -> None:
    """Overview's own "Manage agents" button points at /agents, which is not one
    of the five. The shell must not leave the address bar claiming a page it is
    not showing — it sends the URL back to "/" instead."""
    _set_ui_mode("simple")
    page.goto(dashboard.base_url.rstrip("/") + "/agents")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    page.wait_for_selector("[data-simple-composer]", timeout=20_000)
    assert urlparse(page.url).path == "/", page.url
