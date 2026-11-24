"""E2E: the Files preview is usable on a phone.

Two things were wrong once the panes stacked at 390px.

The pane header was one flex row — ``[name][size][spacer][Preview|Raw][edit]
[copy][download][delete][close]`` — and only the *name* could shrink, so
``notes-about-the-quarterly-review.md`` rendered as ``n`` and ``index.html`` as
``i.``: the preview no longer said which file it belonged to.

And both panes were on screen at once, which cost the phone twice: the tree
kept ``32vh`` plus its toolbar and footer *after* you had picked a file,
leaving a markdown page or an HTML frame about a third of the screen — and with
nothing picked that same tree sat above two thirds of a screen of "Select a
file to preview". Exactly one pane holds the column below the breakpoint now —
the same master/detail move the chat history rail makes — with an **All files**
button in the preview header to go back.

Both previews are exercised, because markdown renders into the pane while HTML
renders into a sandboxed iframe that has to be given a height to fill.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import is_mobile_viewport

pytestmark = pytest.mark.e2e

DIR = "e2e-preview-mobile"
MD_NAME = "notes-about-the-quarterly-review.md"
MD_PATH = f"{DIR}/{MD_NAME}"
HTML_PATH = f"{DIR}/index.html"

FILES = {
    MD_PATH: "# Quarterly review\n\nA paragraph long enough to wrap on a phone.\n",
    HTML_PATH: (
        "<!DOCTYPE html><html><body><h1 id='hello'>Voices of the waterfront</h1></body></html>"
    ),
}

TREE = ".files-tree-pane"
PANE = ".files-preview-pane"
BACK = "button.files-pane-back"
BODY = ".files-preview-body"
TITLE = ".files-pane-title strong"


def _box(locator):
    box = locator.bounding_box()
    assert box is not None, "element has no layout box"
    return box


def _open(page, base_url: str, path: str):
    """Seed the fixtures and deep-link onto one file's preview."""
    base = base_url.rstrip("/")
    for p, content in FILES.items():
        resp = page.request.put(f"{base}/api/files/{p}?agent=merli", data={"content": content})
        assert resp.ok, f"could not seed {p}: {resp.status} {resp.text()}"
    page.goto(f"{base}/files?path={path}&agent=merli", wait_until="domcontentloaded")
    page.locator(TITLE).first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(300)


def test_the_previewed_file_name_is_readable(page, dashboard) -> None:
    """The regression: a name column starved to a few pixels by the actions."""
    _open(page, dashboard.base_url, MD_PATH)
    title = page.locator(TITLE).first
    box = _box(title)
    assert box["width"] > 150, f"file name collapsed to {box['width']}px"
    assert title.inner_text().strip() == MD_NAME
    # It may still ellipsize on a narrow pane, but it must show most of itself.
    shown = page.evaluate("el => el.clientWidth / el.scrollWidth", title.element_handle())
    assert shown > 0.5, f"only {shown:.0%} of the file name is on screen"


def test_the_tree_owns_the_phone_screen_with_nothing_open(page, dashboard) -> None:
    """The other half of master/detail, and the state you arrive in: a 32vh tree
    above two thirds of a screen of "Select a file to preview" is the split
    upside down."""
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/files", wait_until="domcontentloaded")
    page.locator(TREE).first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(300)

    if not is_mobile_viewport(page):
        assert page.locator(PANE).first.is_visible(), "the desktop layout keeps both panes"
        return

    assert not page.locator(PANE).first.is_visible(), "the empty preview still takes the screen"
    assert _box(page.locator(TREE).first)["height"] > page.viewport_size["height"] * 0.6


def test_the_preview_owns_the_phone_screen(page, dashboard) -> None:
    """Master/detail: with a file open the tree is the screen you came from."""
    _open(page, dashboard.base_url, MD_PATH)
    tree = page.locator(TREE).first
    body = page.locator(BODY).first

    if not is_mobile_viewport(page):
        assert tree.is_visible(), "the desktop layout keeps both panes"
        assert page.locator(BACK).first.is_visible() is False
        return

    assert not tree.is_visible(), "the tree still holds a third of the phone screen"
    viewport = page.viewport_size["height"]
    assert _box(body)["height"] > viewport * 0.5, "the preview got a letterbox"
    # The markdown really rendered into it, not just the pane chrome.
    body.locator("h1:has-text('Quarterly review')").wait_for(state="visible", timeout=10_000)

    # Back returns to the tree, with the file still selected.
    page.locator(BACK).first.click()
    tree.wait_for(state="visible", timeout=10_000)
    page.locator(f"[role='treeitem']:has-text('{MD_NAME}')").first.wait_for(
        state="visible", timeout=10_000
    )


def test_the_html_frame_fills_the_pane(page, dashboard) -> None:
    """An HTML preview is an iframe sized against the pane; a `height: 100%`
    child of a plain `overflow: auto` box has no height to be a percentage of."""
    _open(page, dashboard.base_url, HTML_PATH)
    frame = page.locator(f"{BODY} iframe").first
    frame.wait_for(state="visible", timeout=15_000)
    page.frame_locator(f"{BODY} iframe").locator("#hello").wait_for(state="visible", timeout=15_000)
    body_h = _box(page.locator(BODY).first)["height"]
    assert _box(frame)["height"] >= body_h - 2, "the frame does not fill its pane"
    if is_mobile_viewport(page):
        assert body_h > page.viewport_size["height"] * 0.5, "the frame got a letterbox"


def test_preview_and_raw_stay_reachable(page, dashboard) -> None:
    """The toggle is the control the reader came for — it must not be the thing
    that gets pushed off a 390px header."""
    _open(page, dashboard.base_url, MD_PATH)
    for name in ("Raw", "Preview"):
        btn = page.get_by_role("button", name=name, exact=True).first
        assert btn.is_visible(), f"{name} is off screen"
        box = _box(btn)
        assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1, (
            f"{name} is clipped by the viewport ({box})"
        )
    page.get_by_role("button", name="Raw", exact=True).first.click()
    page.locator(f"{BODY} pre:has-text('# Quarterly review')").first.wait_for(
        state="visible", timeout=10_000
    )
    page.get_by_role("button", name="Preview", exact=True).first.click()
    page.locator(f"{BODY} h1:has-text('Quarterly review')").first.wait_for(
        state="visible", timeout=10_000
    )
