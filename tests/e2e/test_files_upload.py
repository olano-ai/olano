"""Files view E2E: upload files from the device into a chosen folder.

Pick a folder in the tree, press the toolbar's Upload button, choose files in
the system picker: they land in that folder through ``POST /api/files/upload``
and show up in the tree. Uploading a name that already exists asks before
replacing it. Runs at both viewports, since on a phone the toolbar button is
the only way in (there is no right-click menu to fall back on).
"""

from __future__ import annotations

import uuid

import pytest

from tests.e2e.route_checks import open_agent_workspace, tree_row

pytestmark = pytest.mark.e2e

FOLDER = "e2e-upload-target"
UPLOAD_BTN = "button[aria-label*='Upload files']"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _raw(page, base_url: str, path: str) -> bytes:
    resp = page.request.get(f"{base_url.rstrip('/')}/api/files-raw/{path}")
    assert resp.status == 200, f"{path}: {resp.status}"
    return resp.body()


def _choose(page, files) -> None:
    with page.expect_file_chooser() as fc:
        page.locator(UPLOAD_BTN).first.click()
    fc.value.set_files(files)


def test_upload_into_the_selected_folder_and_replace(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    # The harness workspace outlives a run, and both viewports share it: unique
    # names keep the first upload a fresh one rather than a replace.
    tag = uuid.uuid4().hex[:8]
    xlsx, notes = f"PM Trade Blotter {tag}.xlsx", f"notes-{tag}.txt"
    resp = page.request.post(f"{base}/api/files/create", data={"path": FOLDER, "type": "directory"})
    assert resp.status in (200, 409)

    page.goto(f"{base}/files", wait_until="domcontentloaded")
    open_agent_workspace(page)
    folder = tree_row(page, FOLDER).first
    folder.wait_for(state="visible", timeout=15_000)
    folder.click()  # focusing the folder is what points Upload at it

    _choose(
        page,
        [
            {"name": xlsx, "mimeType": XLSX_MIME, "buffer": b"PK\x03\x04first"},
            {"name": notes, "mimeType": "text/plain", "buffer": b"hello"},
        ],
    )
    tree_row(page, notes).first.wait_for(state="visible", timeout=15_000)
    tree_row(page, xlsx).first.wait_for(state="visible", timeout=15_000)
    assert _raw(page, base, f"{FOLDER}/{xlsx}") == b"PK\x03\x04first"
    assert _raw(page, base, f"{FOLDER}/{notes}") == b"hello"

    # Same name again: the dialog asks, and Replace sends it with overwrite.
    tree_row(page, FOLDER).first.click()
    _choose(page, [{"name": notes, "mimeType": "text/plain", "buffer": b"second"}])
    dialog = page.get_by_role("alertdialog")
    dialog.wait_for(state="visible", timeout=10_000)
    assert notes in (dialog.text_content() or "")
    dialog.get_by_role("button", name="Replace").click()
    page.wait_for_function(
        "(url) => fetch(url).then(r => r.text()).then(t => t === 'second')",
        arg=f"{base}/api/files-raw/{FOLDER}/{notes}",
        timeout=15_000,
    )
