"""Files-tree E2E: dot-entries are listed only when the toggle asks for them.

`GET /api/files` omits names starting with `.` unless it is passed
`hidden=true`, so the Files view's eye button is a **listing** control, not a
client-side filter — the rows it reveals were never sent to the browser. That
is the half an API test cannot see: the button has to re-request every
directory the tree already has open (a filter could only ever hide), and the
choice has to survive a reload. The server half is pinned in
``tests/test_runtime_api.py``.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import open_agent_workspace, tree_row

pytestmark = pytest.mark.e2e

# Created by the tests themselves — the seeded workspace has no dotfile to lean
# on, and `.olano/` only exists once an agent has actually run there.
ROOT_DOTFILE = ".e2e-hidden.env"
ROOT_ANCHOR = "e2e-hidden-anchor.md"
SUBDIR = "e2e-hidden-sub"
DEEP_DOTFILE = ".e2e-deep.env"
DEEP_ANCHOR = "e2e-deep-anchor.md"

# The toolbar toggle, named by the state it will move to. Matched on a fragment
# of the (deliberately explanatory) label so re-wording the rest of the tooltip
# doesn't strand the selector.
SHOW_BTN = "button[aria-label*='Show hidden files']"
HIDE_BTN = "button[aria-label*='Hide hidden files']"


def _row(page, name: str):
    """The tree row for one entry (a WAI-ARIA tree item)."""
    return tree_row(page, name)


def _seed(page, base_url: str, entries: tuple[tuple[str, str], ...]) -> None:
    """Create fixture entries through the app's own API (409 = a prior run's)."""
    base = base_url.rstrip("/")
    for path, kind in entries:
        resp = page.request.post(f"{base}/api/files/create", data={"path": path, "type": kind})
        assert resp.status in (200, 409), f"could not seed {path}: {resp.status} {resp.text()}"


def _open_root(page, base_url: str) -> None:
    """Seed the root fixtures, open /files, and wait for the tree to render."""
    _seed(page, base_url, ((ROOT_DOTFILE, "file"), (ROOT_ANCHOR, "file")))
    page.goto(f"{base_url.rstrip('/')}/files", wait_until="domcontentloaded")
    open_agent_workspace(page)
    _row(page, ROOT_ANCHOR).first.wait_for(state="visible", timeout=15_000)


def test_dotfiles_are_listed_only_while_the_toggle_is_on(page, dashboard) -> None:
    """Off by default, on with one click, off again with the next — no reload."""
    _open_root(page, dashboard.base_url)

    # The anchor is in the same listing, so its arrival means the root has
    # rendered — the dotfile's absence is a real absence, not a slow load.
    assert _row(page, ROOT_DOTFILE).count() == 0, "dot-entries must be off by default"

    page.locator(SHOW_BTN).first.click()
    _row(page, ROOT_DOTFILE).first.wait_for(state="visible", timeout=10_000)

    page.locator(HIDE_BTN).first.click()
    page.wait_for_function(
        f"() => !document.body.innerText.includes({ROOT_DOTFILE!r})",
        timeout=10_000,
    )


def test_the_toggle_survives_a_reload(page, dashboard) -> None:
    """Someone working in `.env` shouldn't re-enable it on every page load."""
    _open_root(page, dashboard.base_url)
    page.locator(SHOW_BTN).first.click()
    _row(page, ROOT_DOTFILE).first.wait_for(state="visible", timeout=10_000)

    page.reload(wait_until="domcontentloaded")

    # A reload starts the in-session expansion over (agents collapsed again);
    # what must survive is the toggle, which is what the reopened root proves.
    open_agent_workspace(page)
    _row(page, ROOT_DOTFILE).first.wait_for(state="visible", timeout=15_000)
    assert page.locator(HIDE_BTN).count() == 1, "the button must come back lit/on"


def test_toggling_relists_directories_already_open(page, dashboard) -> None:
    """Flipping it re-requests open directories, not just the root.

    A folder expanded while the toggle was off holds a listing the server built
    without its dot-entries, so nothing client-side could reveal them.
    """
    _seed(
        page,
        dashboard.base_url,
        (
            (SUBDIR, "directory"),
            (f"{SUBDIR}/{DEEP_DOTFILE}", "file"),
            (f"{SUBDIR}/{DEEP_ANCHOR}", "file"),
        ),
    )
    page.goto(f"{dashboard.base_url.rstrip('/')}/files", wait_until="domcontentloaded")
    open_agent_workspace(page)
    _row(page, SUBDIR).first.wait_for(state="visible", timeout=15_000)
    _row(page, SUBDIR).first.click()

    _row(page, DEEP_ANCHOR).first.wait_for(state="visible", timeout=10_000)
    assert _row(page, DEEP_DOTFILE).count() == 0

    page.locator(SHOW_BTN).first.click()
    _row(page, DEEP_DOTFILE).first.wait_for(state="visible", timeout=10_000)
