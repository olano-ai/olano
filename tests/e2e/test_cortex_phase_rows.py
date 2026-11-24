"""E2E: the Cortex "Phases & schedule" rows are readable at both viewports.

The rows were one flat flex line — ``[dot][name][cadence][last run][All-agents
On/Off][this-agent Inherit/On/Off][Log][Run now]``. Everything but the name is
fixed-width, so on a phone the name was the only thing that could give, and it
gave all of it: ``Brainstorming`` rendered as ``B.`` and ``Skill Librarian`` as
``S.`` — eight rows of identical unlabelled controls with no way to tell which
phase you were about to run, or which of the two segmented controls was the
fleet-wide one.

The row is four blocks now, and below the tier breakpoint they restack: name,
then when it runs, then each tier behind the same inline label the settings
table above uses, then the actions. These pin the properties that make that
readable — and that the desktop row stays a single line.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.cortex_ui import open_cortex_settings, open_subsection
from tests.e2e.route_checks import is_mobile_viewport

pytestmark = pytest.mark.e2e

ROW = ".cortex-phase-row"
TAP_TARGET_PX = 36  # segmented chips; the Log/Run now buttons clear 40


def _box(locator):
    box = locator.bounding_box()
    assert box is not None, "element has no layout box"
    return box


def _open_phases(page):
    """Open /cortex and expand the (collapsed-by-default) phase list."""
    base = page.url.split("/overview")[0].rstrip("/")
    page.goto(base + "/cortex", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)
    # Everything configurable lives in one card that is collapsed by default,
    # so opening it is now part of reaching any of these controls.
    open_cortex_settings(page)
    open_subsection(page, "Phases & schedule")
    page.locator(ROW).first.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(300)
    return page.locator(ROW)


def test_every_phase_name_survives_the_layout(page) -> None:
    """The regression itself: a name column squeezed to a letter and an ellipsis.

    Asserted as shape, not CSS — a future layout may get the name its width some
    other way, but the name must be on screen. "All of it, or at least 120px of
    it" rather than "all of it": a settings table may legitimately ellipsize a
    very long label one day; it may never render one as ``B.``.
    """
    rows = _open_phases(page)
    assert rows.count() >= 3, "expected the phase list to have rows to check"
    for i in range(rows.count()):
        name = rows.nth(i).locator(".cortex-phase-name")
        text = name.inner_text().strip()
        shown, needed = page.evaluate(
            "el => [el.clientWidth, el.scrollWidth]", name.element_handle()
        )
        assert shown >= min(needed, 120), f"phase {text!r} shows {shown}px of a {needed}px name"


def test_a_long_phase_name_wraps_instead_of_truncating(page) -> None:
    """The phone layout must show the whole name, not a prefix of it.

    The shipped labels are short enough that "not clipped" says little, so this
    substitutes names long enough to force the question — including one with no
    spaces in it, which wraps only because the rule says `anywhere`. Nothing may
    be cut off and nothing may push the page sideways.
    """
    rows = _open_phases(page)
    if not is_mobile_viewport(page):
        pytest.skip("desktop is a single line and may legitimately ellipsize")
    labels = [
        "Skill Librarian & Workspace Consolidation Review",
        "Supercalifragilisticexpialidociousphasename",
    ]
    page.evaluate(
        "labels => document.querySelectorAll('.cortex-phase-name').forEach((el, i) => {"
        "  if (i < labels.length) el.childNodes[0].nodeValue = labels[i];"
        "})",
        labels,
    )
    page.wait_for_timeout(200)
    for i, label in enumerate(labels):
        name = rows.nth(i).locator(".cortex-phase-name")
        shown, needed = page.evaluate(
            "el => [el.clientWidth, el.scrollWidth]", name.element_handle()
        )
        assert shown >= needed, f"{label!r} is clipped ({shown}px of {needed}px)"
    assert not page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    ), "a long phase name pushed the page wider than the viewport"


def test_each_phase_tier_says_which_scope_it_sets(page) -> None:
    """Stacked, the two segmented controls sit above each other with nothing to
    tell them apart — the column headers that name them are desktop-only."""
    rows = _open_phases(page)
    row = rows.first
    tiers = row.locator(".cortex-phase-tier")
    assert tiers.count() == 2, "a phase row carries an All-agents and an agent control"
    for i in range(2):
        label = tiers.nth(i).locator(".cortex-tier-inline-label")
        assert label.count() == 1
        # Desktop has the headers, so the inline labels stay hidden there.
        assert label.first.is_visible() is is_mobile_viewport(page)


def test_phase_controls_are_thumb_sized_on_a_phone(page) -> None:
    """The desktop row's 3px/9px chips are ~24px tall — half the touch floor."""
    rows = _open_phases(page)
    if not is_mobile_viewport(page):
        pytest.skip("tap-target floor applies to the touch viewport")
    row = rows.first
    chips = row.locator(".cortex-phase-tier .seg-btn")
    assert chips.count() == 5, "On/Off plus Inherit/On/Off"
    for i in range(chips.count()):
        box = _box(chips.nth(i))
        assert box["height"] >= TAP_TARGET_PX, (
            f"chip {i} is {box['width']}×{box['height']} — under the {TAP_TARGET_PX}px floor"
        )
    for name in ("Log", "Run now"):
        box = _box(row.get_by_role("button", name=name, exact=True))
        assert box["height"] >= 40, f"{name} is {box['height']}px tall"


def test_the_desktop_row_stays_one_line(page) -> None:
    """The stacked phone layout must not leak upward: on desktop a phase is a
    single table-like line, which is what makes eleven of them scannable."""
    rows = _open_phases(page)
    if is_mobile_viewport(page):
        pytest.skip("the stacked layout is the point below the breakpoint")
    row = rows.first
    box = _box(row)
    assert box["height"] < 46, f"desktop phase row is {box['height']}px — it wrapped"
    # Name and actions share a baseline rather than stacking.
    name_y = _box(row.locator(".cortex-phase-name"))["y"]
    actions_y = _box(row.locator(".cortex-phase-actions"))["y"]
    assert abs(name_y - actions_y) < 20, "the actions dropped below the name"
