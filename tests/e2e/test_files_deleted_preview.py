"""E2E: deleting the file open in the preview.

When the file shown in the preview is deleted (by an agent, another tab or
the API), the preview used to keep a loading spinner forever. It now says the
file is gone and offers a way to close it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

DIR = "e2e-deleted-preview"
FILE_PATH = f"{DIR}/GONE.md"
PREVIEW_HEAD = "strong:text-is('GONE.md')"


def _open_then_delete(page, base: str, path: str, head: str) -> None:
    resp = page.request.post(f"{base}/api/files/create", data={"path": DIR, "type": "directory"})
    assert resp.status in (200, 409), resp.text()
    resp = page.request.put(
        f"{base}/api/files/{path}?agent=merli", data={"content": "# Soon gone\n"}
    )
    assert resp.status == 200, resp.text()
    page.goto(f"{base}/files?path={path}&agent=merli", wait_until="domcontentloaded")
    page.locator(head).first.wait_for(state="visible", timeout=15_000)
    # The deletion arrives the way an agent's or another tab's would: as a
    # live `file_updated` frame, not as a click in this view.
    resp = page.request.delete(f"{base}/api/files/{path}?agent=merli")
    assert resp.status == 200, resp.text()


def test_a_deleted_file_says_it_is_gone(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    _open_then_delete(page, base, FILE_PATH, PREVIEW_HEAD)

    gone = page.get_by_test_id("preview-gone")
    gone.wait_for(state="visible", timeout=15_000)
    assert page.locator(".files-preview-body .spinner").count() == 0
    gone.get_by_role("button", name="Close").click()
    page.locator(PREVIEW_HEAD).wait_for(state="detached", timeout=5_000)


def test_a_deleted_image_says_it_is_gone(page, dashboard) -> None:
    """Media render straight from the raw endpoint with no read of their own,
    so the live change has to check the file is still there."""
    base = dashboard.base_url.rstrip("/")
    _open_then_delete(page, base, f"{DIR}/gone.png", "strong:text-is('gone.png')")
    page.get_by_test_id("preview-gone").wait_for(state="visible", timeout=15_000)
