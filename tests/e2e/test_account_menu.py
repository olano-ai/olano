"""E2E: the account/profile menu stays whole and short in both shells.

Two defects this pins, both of which only a real browser can see.

**The collapsed rail clipped its own menu.** ``.simple-rail.is-collapsed`` is
``overflow: hidden`` over a 78px strip. The account menu is ~240px wide, so
rendered inside that strip it was sliced off at the strip's edge: a column of
half-words, with the shell switch and Sign out unreachable. It is portaled to
``<body>`` and positioned from the button's rect now, so the assertion is
geometric — the menu's box must fit inside the viewport, not inside the rail.

**Theme and language were thirteen rows.** The themes plus ten languages, one
clickable row each, made the menu taller than a laptop viewport in both shells
— which is how Sign out ended up parked below an invisible scroll fold. They
are one ``<select>`` each now, so the check is that the rows are gone and the
two dropdowns carry every value.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import HAMBURGER, SIMPLE_ROOT, open_mobile_nav, open_simple_rail

pytestmark = pytest.mark.e2e

# Every language the picker offers, and every theme. If a dropdown is missing
# one of these it is not a replacement for the list it replaced.
LANGS = [
    "English",
    "中文",
    "Español",
    "हिन्दी",
    "العربية",
    "Português",
    "Русский",
    "日本語",
    "Français",
    "Deutsch",
]
# In picker order. The first two are the product's own themes and lead the
# list deliberately — they are what nearly everyone picks, and the dark
# variants used to push Olano Light to fifth.
THEMES = [
    "Olano Dark",
    "Olano Light",
    "Midnight",
    "Mocha",
    "Cool Black",
    "Volt",
    "Black & White",
    "Harbor",
    "Sepia",
    "Sakura",
    "Pastel Green",
    "Singapore",
    "High Contrast",
]


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _assert_pref_selects(page, menu) -> None:
    """The two dropdowns replaced the row lists, and nothing was dropped."""
    selects = menu.locator("select.menu-select")
    assert selects.count() == 2, "theme and language must each be one <select>"
    options = [o.strip() for o in selects.all_inner_texts()]
    joined = " ".join(options)
    for value in THEMES + LANGS:
        assert value in joined, f"{value!r} missing from the preference dropdowns"
    # Order, not just membership: the two house themes lead the dropdown.
    theme_options = [o.strip() for o in selects.first.locator("option").all_inner_texts()]
    assert theme_options[:2] == THEMES[:2], theme_options
    # The old shape — a .menu-item per value — must be gone, or the menu is
    # still thirteen rows tall with a dropdown bolted on top.
    for value in LANGS[1:]:
        assert menu.locator(f".menu-item:has-text('{value}')").count() == 0, value


def test_simple_collapsed_rail_menu_is_not_clipped(page) -> None:
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)

    collapse = page.get_by_role("button", name="Collapse the agent list")
    if collapse.count() == 0:
        pytest.skip("drawer-mode rail has no collapse toggle")
    collapse.first.click()
    page.wait_for_selector(f"{SIMPLE_ROOT}.rail-collapsed", timeout=10_000)

    page.get_by_role("button", name="Your account").click()
    menu = page.locator(".simple-account-menu")
    menu.wait_for(state="visible", timeout=10_000)

    box = menu.bounding_box()
    assert box is not None
    rail = page.locator(".simple-rail.is-collapsed").bounding_box()
    assert rail is not None
    # The menu is wider than the strip it hangs off — that is the whole point —
    # and every pixel of it is on screen anyway.
    assert box["width"] > rail["width"], "menu should not be squeezed into the strip"
    viewport = page.viewport_size
    assert box["x"] >= 0
    assert box["y"] >= 0
    assert box["x"] + box["width"] <= viewport["width"] + 1
    assert box["y"] + box["height"] <= viewport["height"] + 1

    # And the actions that clipping used to hide are clickable, not merely present.
    _assert_pref_selects(page, menu)


def test_simple_account_menu_is_on_top_at_both_viewports(page) -> None:
    """Not merely rendered — actually the thing under the pointer.

    Portaling the menu to ``<body>`` took it out of the rail's stacking context,
    and on a phone the rail *is* the drawer (``z-modal + 1``): a bare ``.menu``
    z-index of 50 left the menu painted underneath it, present in the DOM and
    invisible on screen. ``is_visible()`` cannot see that, so ask the document
    what is actually at the menu's own centre point.
    """
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)

    page.get_by_role("button", name="Your account").click()
    menu = page.locator(".simple-account-menu")
    menu.wait_for(state="visible", timeout=10_000)
    box = menu.bounding_box()
    assert box is not None
    hit = page.evaluate(
        "([x, y]) => document.elementFromPoint(x, y)?.closest('.simple-account-menu') !== null",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert hit, "the account menu is covered by something painted above it"
    _assert_pref_selects(page, menu)


def test_advanced_profile_menu_uses_dropdowns(page) -> None:
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)

    # Below the breakpoint the sidebar — and the profile button in its footer —
    # lives in the nav drawer, so it has to be opened before anything in it is
    # clickable. Both breakpoints mount the hamburger and CSS hides one, so the
    # question is whether it is *visible*, not whether it exists.
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    wrap = page.locator(".profile-menu-wrap")
    wrap.locator("button").first.click()
    menu = wrap.locator(".menu")
    menu.wait_for(state="visible", timeout=10_000)
    _assert_pref_selects(page, menu)


def _assert_quiet_controls(page, menu) -> None:
    """The menu's controls are minimal: borderless dropdowns and a real switch.

    The theme/language dropdowns used to be full bordered inputs, and the shell
    switch a text button naming its destination. Now the dropdowns draw no box
    of their own (the menu already has one) and the shell control is a
    ``role="switch"`` labelled with the state it controls — the one labelling a
    toggle can carry without ambiguity. Computed style, not class names: the
    border is what the eye sees.
    """
    selects = menu.locator("select.menu-select")
    for i in range(selects.count()):
        color = selects.nth(i).evaluate("el => getComputedStyle(el).borderTopColor")
        assert color in ("rgba(0, 0, 0, 0)", "transparent"), (
            f"dropdown {i} still draws a border: {color}"
        )
    switches = menu.locator("[role='switch']")
    assert switches.count() == 1, "the shell switch must be exactly one real switch (admin)"
    sw = switches.first
    assert sw.get_attribute("aria-checked") in ("true", "false")
    assert sw.evaluate("el => getComputedStyle(el).borderTopStyle") == "none"
    # State-named: the row says which view the switch controls.
    assert "Simple view" in menu.locator("[data-testid='uimode-switch']").inner_text()


def test_advanced_profile_menu_controls_are_quiet(page) -> None:
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    wrap = page.locator(".profile-menu-wrap")
    wrap.locator("button").first.click()
    menu = wrap.locator(".menu")
    menu.wait_for(state="visible", timeout=10_000)
    _assert_quiet_controls(page, menu)


def test_simple_account_menu_controls_are_quiet(page) -> None:
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)
    page.get_by_role("button", name="Your account").click()
    menu = page.locator(".simple-account-menu")
    menu.wait_for(state="visible", timeout=10_000)
    _assert_quiet_controls(page, menu)


def test_the_sidebar_theme_icon_follows_the_account_menu(page) -> None:
    """The sidebar's theme button wears the active theme's icon, and it used
    to keep its own copy of the theme: picking one from the account menu (or
    Config) left the button showing the theme that was active before."""
    _set_ui_mode("advanced")
    page.evaluate("() => localStorage.setItem('lc-theme', 'dark')")
    page.reload()
    wait_for_app_shell(page)
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    toggle = page.locator(".theme-toggle")
    before = toggle.inner_html()

    wrap = page.locator(".profile-menu-wrap")
    wrap.locator("button").first.click()
    menu = wrap.locator(".menu")
    menu.wait_for(state="visible", timeout=10_000)
    # Olano Dark wears the sun; Volt wears the bolt.
    menu.locator("select.menu-select").first.select_option("volt")
    assert page.evaluate("() => document.documentElement.dataset.theme") == "volt"
    page.wait_for_function(
        "(before) => document.querySelector('.theme-toggle')?.innerHTML !== before", arg=before
    )

    # And the button's own cycle continues from the theme the menu picked.
    if page.locator(HAMBURGER).is_visible():
        # On a phone Escape closes the nav drawer the button lives in as well
        # as the menu: let it close, then open it again.
        page.keyboard.press("Escape")
        page.locator(".app-shell.mobile-nav-open").wait_for(state="detached", timeout=5_000)
        open_mobile_nav(page)
    else:
        page.keyboard.press("Escape")
    toggle.click()
    assert page.evaluate("() => document.documentElement.dataset.theme") == "bw"
    page.evaluate("() => localStorage.setItem('lc-theme', 'dark')")
