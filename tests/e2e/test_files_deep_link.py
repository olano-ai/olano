"""Files-view deep links: /files?path=… must select AND preview the file.

Regression for the Cortex file lists (item drawers, pulse references, skill
panels, optimize reports) — every one of them links to
``/files?path=<workspace-relative>&agent=<name>``, and the Files view used to
ignore the query string entirely: clicking a file in a project's detail drawer
switched to the Files tab and then just showed whatever was previously
selected. The view now consumes the params once — expands the ancestor chain,
selects + previews a file (or expands a folder, e.g. a promoted item's
project directory). The address then names what is open, in its canonical form
``/files/<agent>?path=<path>``, so a reload or a copied link reopens it and
Back leaves it.

Seeded through the app's own files API (the only folder a fresh workspace
reliably has is whatever another test created — see
``test_files_tree_failed_dir.py`` for the precedent).
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.route_checks import TREE_ROW, is_mobile_viewport

pytestmark = pytest.mark.e2e

DIR_TOP = "e2e-deeplink"
DIR_NESTED = f"{DIR_TOP}/project"
FILE_PATH = f"{DIR_NESTED}/PLAN.md"

# The tree row is a WAI-ARIA tree item; the preview pane's header renders the
# bare file name in a <strong>, so the two are distinguishable.
FILE_ROW = f"{TREE_ROW}:has-text('PLAN.md')"
PREVIEW_HEAD = "strong:text-is('PLAN.md')"
BACK_BTN = "button.files-pane-back"


def _seed_workspace(page, base_url: str) -> None:
    """Create e2e-deeplink/project/PLAN.md through the app's own API."""
    base = base_url.rstrip("/")
    for path, kind in ((DIR_TOP, "directory"), (DIR_NESTED, "directory"), (FILE_PATH, "file")):
        resp = page.request.post(f"{base}/api/files/create", data={"path": path, "type": kind})
        assert resp.status in (200, 409), f"could not seed {path}: {resp.status} {resp.text()}"


def test_file_deep_link_selects_and_previews(page, dashboard) -> None:
    """Landing on /files?path=<file> opens the ancestor chain, shows the file
    selected in the tree, previews it, and the address names the file.

    Below the breakpoint the two panes are one column and the preview takes it
    (see ``test_files_mobile_preview.py``), so the tree row is proved there by
    going back to it rather than by looking past the preview.
    """
    base = dashboard.base_url.rstrip("/")
    _seed_workspace(page, base)

    page.goto(f"{base}/files?path={FILE_PATH}&agent=merli", wait_until="domcontentloaded")

    # The preview pane actually opened on the file (its header carries the name).
    page.locator(PREVIEW_HEAD).first.wait_for(state="visible", timeout=15_000)
    # The link's older spelling became the canonical address of that file.
    page.wait_for_url(re.compile(rf"/files/merli\?path={re.escape(FILE_PATH)}$"), timeout=10_000)

    # A reload reopens the same file: the address is the selection.
    page.reload(wait_until="domcontentloaded")
    page.locator(PREVIEW_HEAD).first.wait_for(state="visible", timeout=15_000)

    # …and the ancestor chain expanded, so the file's row is in the tree.
    if is_mobile_viewport(page):
        page.locator(BACK_BTN).first.click()
    page.locator(FILE_ROW).first.wait_for(state="visible", timeout=15_000)


def test_directory_deep_link_expands_the_folder(page, dashboard) -> None:
    """A folder target (e.g. a promoted item's project directory) expands down
    to that folder instead of erroring on a file preview."""
    base = dashboard.base_url.rstrip("/")
    _seed_workspace(page, base)

    page.goto(f"{base}/files?path={DIR_NESTED}&agent=merli", wait_until="domcontentloaded")

    # The folder itself was expanded, so its child file's row is visible.
    page.locator(FILE_ROW).first.wait_for(state="visible", timeout=15_000)
    # The address keeps naming the folder (it opened no file to replace it).
    page.wait_for_url(re.compile(rf"/files/merli\?path={re.escape(DIR_NESTED)}$"), timeout=10_000)
