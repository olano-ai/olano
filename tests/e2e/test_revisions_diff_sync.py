"""E2E: the Revisions page shows what each commit changed, and resolves a pull.

Seeds real history into the test's home with the engine itself, then drives
the page at both viewports:

* selecting a commit lists its files with added / modified / deleted status
  and line counts, and a file opens a rendered diff, side by side on desktop
  and unified on a phone, switchable either way;
* the GitHub card offers the connect dialog, every field explained;
* with a stand-in remote (a local bare repository; the runtime is in-process,
  so the token lookup is patched to "none needed"), a pull that collides with
  a local edit parks a merge, and the page resolves it file by file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import is_mobile_viewport

pytestmark = pytest.mark.e2e

MEMORY = "agents/merli/workspace/MEMORY.md"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "someone",
    "GIT_AUTHOR_EMAIL": "someone@example.com",
    "GIT_COMMITTER_NAME": "someone",
    "GIT_COMMITTER_EMAIL": "someone@example.com",
}


def _home() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root


@pytest.fixture
def history():
    from olano.revisions.engine import RevisionsEngine

    home = _home()
    ws = home / "agents" / "merli" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "MEMORY.md").write_text("# memory\nline one\nline two\nline three\n")
    (ws / "SOUL.md").write_text("calm\n")
    engine = RevisionsEngine(home)
    engine.snapshot(reason="seed")
    (ws / "MEMORY.md").write_text("# memory\nline one\nline 2, edited\nline three\n")
    (ws / "IDEAS.md").write_text("a new idea\n")
    (ws / "charts").mkdir(exist_ok=True)
    (ws / "charts" / "sales.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\x01\x02\x03")
    engine.snapshot(author="merli", reason="turn: merli")
    return engine


def _open_revisions(page, dashboard) -> None:
    page.goto(f"{dashboard.base_url.rstrip('/')}/revisions", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator("button:has-text('Snapshot now')").wait_for(state="visible", timeout=15_000)


def _no_horizontal_page_scroll(page) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"page scrolls sideways by {overflow}px"


def test_a_commit_lists_its_files_and_each_file_diffs(page, dashboard, history) -> None:
    _open_revisions(page, dashboard)
    page.locator(".revisions-entry-main", has_text="turn: merli").click()

    summary = page.get_by_test_id("revisions-commit-summary")
    summary.wait_for(state="visible", timeout=15_000)
    text = summary.inner_text()
    assert "3 files changed" in text and "+2" in text and "2 added" in text and "1 modified" in text

    # A binary file (an image here; documents and media alike) has no lines to
    # compare: it says so and offers the file itself.
    png = page.locator(".revisions-file-row", has_text="sales.png")
    assert "binary" in png.inner_text()
    png.click()
    download = page.locator(".revisions-file-diff a", has_text="Download after").first
    download.wait_for(state="visible", timeout=10_000)
    assert "/api/revisions/raw?" in (download.get_attribute("href") or "")
    png.click()

    row = page.locator(".revisions-file-row", has_text="MEMORY.md")
    row.click()
    table = page.locator(".revisions-diff table.diff").first
    table.wait_for(state="visible", timeout=15_000)
    mobile = is_mobile_viewport(page)
    expected, other = ("unified", "split") if mobile else ("split", "unified")
    assert f"diff-{expected}" in (table.get_attribute("class") or "")
    assert "line 2, edited" in page.locator(".revisions-diff").first.inner_text()

    page.locator(
        ".revisions-seg button", has_text="Unified" if not mobile else "Side by side"
    ).first.click()
    page.locator(f".revisions-diff table.diff-{other}").first.wait_for(
        state="visible", timeout=10_000
    )

    page.locator(".revisions-file-diff button", has_text="Open file at this commit").first.click()
    page.locator(".revisions-viewer").wait_for(state="visible", timeout=10_000)
    assert "line 2, edited" in page.locator(".revisions-viewer").inner_text()
    _no_horizontal_page_scroll(page)


def test_the_connect_dialog_explains_every_field(page, dashboard, history) -> None:
    _open_revisions(page, dashboard)
    card = page.get_by_test_id("revisions-github")
    card.wait_for(state="visible", timeout=15_000)
    card.locator("button", has_text="Connect a GitHub repository").click()
    dialog = page.locator("[role=dialog]", has_text="Connect a GitHub repository")
    dialog.wait_for(state="visible", timeout=10_000)
    for label in ("Repository", "Sign in with", "Access token", "Branch", "Automatic push"):
        assert dialog.locator("label", has_text=label).count() >= 1, label
    help_text = dialog.inner_text()
    assert "must be empty" in help_text and "Contents: Read and write" in help_text
    assert "Manual (the default)" in help_text
    box = dialog.bounding_box()
    viewport = page.viewport_size
    assert box and viewport and box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1
    dialog.locator("button", has_text="Cancel").click()
    dialog.wait_for(state="detached", timeout=5_000)


@pytest.fixture
def conflicted_remote(history, tmp_path, monkeypatch):
    """Connected to a local bare repo, with GitHub and this box both having
    edited the same line of MEMORY.md since the last push."""
    from olano.config.system_settings import set_revisions_settings
    from olano.revisions import remote

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    set_revisions_settings({"remote": {"url": str(origin), "branch": "main", "sync": "manual"}})
    monkeypatch.setattr(remote, "resolve_token", lambda auth: "")
    history.snapshot()
    remote.RemoteSync(history, str(origin)).push()

    clone = tmp_path / "clone"
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=env)
    (clone / MEMORY).write_text("# memory\nline one\nGITHUB EDIT\nline three\n")
    for args in (
        ["add", "-A"],
        ["commit", "-qm", "edit on github"],
        ["push", "-q", "origin", "HEAD:main"],
    ):
        subprocess.run(["git", *args], cwd=clone, check=True, env=env)

    (_home() / MEMORY).write_text("# memory\nline one\nLOCAL EDIT\nline three\n")
    history.snapshot(author="merli", reason="turn: merli")
    return origin


def test_a_conflicting_pull_is_resolved_on_the_page(page, dashboard, conflicted_remote) -> None:
    _open_revisions(page, dashboard)
    card = page.get_by_test_id("revisions-github")
    card.wait_for(state="visible", timeout=15_000)
    card.locator("button", has_text="Pull").click()

    merge = page.get_by_test_id("revisions-merge")
    merge.wait_for(state="visible", timeout=15_000)
    assert "Nothing has been applied yet" in merge.inner_text()
    assert "LOCAL EDIT" in (_home() / MEMORY).read_text()  # untouched until resolved

    merge.locator(".revisions-file-row", has_text="MEMORY.md").click()
    merge.locator(".revisions-diff table.diff").first.wait_for(state="visible", timeout=15_000)
    complete = merge.locator("button", has_text="Complete merge")
    assert complete.is_disabled()

    merge.locator("button", has_text="Edit the merged text").click()
    editor = merge.locator("textarea.revisions-merge-editor")
    assert "<<<<<<< this box" in editor.input_value()
    assert complete.is_disabled()  # markers still in the text
    merge.locator("button", has_text="Take GitHub's version").click()
    complete.click()

    page.get_by_test_id("revisions-pull-summary").wait_for(state="visible", timeout=15_000)
    assert merge.count() == 0
    assert (_home() / MEMORY).read_text() == "# memory\nline one\nGITHUB EDIT\nline three\n"
    assert "commits to push" in page.get_by_test_id("revisions-github-state").inner_text()
    _no_horizontal_page_scroll(page)


# ── paging, the GitHub connection as default, a connection's progress ──────


@pytest.fixture
def long_history(history):
    """25 commits in all: three pages of ten."""
    ws = _home() / "agents" / "merli" / "workspace"
    for i in range(23):
        (ws / "IDEAS.md").write_text(f"idea {i}\n")
        history.snapshot(author="merli", reason=f"turn {i}")
    assert history.count() == 25
    return history


def test_the_history_pages_ten_commits_at_a_time(page, dashboard, long_history) -> None:
    _open_revisions(page, dashboard)
    rows = page.locator(".revisions-entry-main")
    page.locator(".revisions-pager-page").first.wait_for(state="visible", timeout=15_000)
    assert rows.count() == 10
    assert "Page 1 of 3" in page.locator(".revisions-pager-page").first.inner_text()
    assert "turn 22" in rows.first.inner_text()  # newest first

    page.get_by_role("button", name="Older commits").first.click()
    page.locator(".revisions-pager-page", has_text="Page 2 of 3").first.wait_for(timeout=10_000)
    rows.first.filter(has_text="turn 12").wait_for(timeout=10_000)

    page.get_by_role("button", name="Oldest commits").first.click()
    page.locator(".revisions-pager-page", has_text="Page 3 of 3").first.wait_for(timeout=10_000)
    page.wait_for_function("document.querySelectorAll('.revisions-entry-main').length === 5")
    rows.last.filter(has_text="seed").wait_for(timeout=10_000)
    assert page.get_by_role("button", name="Older commits").first.is_disabled()

    page.get_by_role("button", name="Newest commits").first.click()
    page.locator(".revisions-pager-page", has_text="Page 1 of 3").first.wait_for(timeout=10_000)
    rows.first.filter(has_text="turn 22").wait_for(timeout=10_000)
    assert "25 commits" in page.locator(".page").first.inner_text()
    _no_horizontal_page_scroll(page)


@pytest.fixture
def github_connected_account(monkeypatch):
    """This deployment already signed in to GitHub on the Connections page."""
    from olano.revisions import remote

    monkeypatch.setattr(
        remote, "github_connection", lambda: {"available": True, "account": "octo-admin"}
    )


def test_the_github_connection_is_the_default_sign_in(
    page, dashboard, history, github_connected_account
) -> None:
    _open_revisions(page, dashboard)
    card = page.get_by_test_id("revisions-github")
    card.wait_for(state="visible", timeout=15_000)
    assert "already connected as octo-admin" in card.inner_text()
    card.locator("button", has_text="Connect a GitHub repository").click()
    select = page.get_by_test_id("revisions-connect-auth")
    select.wait_for(state="visible", timeout=10_000)
    assert select.input_value() == "connection"
    first = select.locator("option").first
    assert first.get_attribute("value") == "connection" and "octo-admin" in first.inner_text()
    # The token field only appears for the token option.
    assert page.locator("input[type=password]").count() == 0
    select.select_option("token")
    page.locator("input[type=password]").wait_for(state="visible", timeout=5_000)


@pytest.fixture
def slow_connect(monkeypatch, github_connected_account):
    """A connection that takes a few seconds and reports each step, the way
    the real one does while it checks GitHub, snapshots and uploads."""
    import time

    from olano.config.system_settings import set_revisions_settings
    from olano.revisions import remote

    def fake_connect(*, repo, auth, token, branch, sync, allow_public):
        remote.report_progress("check")
        time.sleep(0.8)
        remote.report_progress("snapshot")
        time.sleep(0.8)
        for pct in (10, 40, 70, 97):
            remote.report_progress(
                "upload",
                percent=pct,
                phase="writing",
                done=pct * 10,
                total=1000,
                transfer="12.00 MiB | 3.00 MiB/s",
            )
            time.sleep(0.7)
        remote.report_progress("save")
        time.sleep(0.3)
        set_revisions_settings(
            {
                "remote": {
                    "url": "https://github.com/acme/olano-history.git",
                    "auth": auth,
                    "sync": sync,
                }
            }
        )
        return {"connected": True}

    monkeypatch.setattr(remote, "_connect", fake_connect)


def test_connecting_shows_each_step_and_the_upload(page, dashboard, history, slow_connect) -> None:
    _open_revisions(page, dashboard)
    card = page.get_by_test_id("revisions-github")
    card.wait_for(state="visible", timeout=15_000)
    card.locator("button", has_text="Connect a GitHub repository").click()
    page.get_by_test_id("revisions-connect-repo").fill("acme/olano-history")
    page.get_by_test_id("revisions-connect-submit").click()

    progress = page.get_by_test_id("revisions-connect-progress")
    progress.wait_for(state="visible", timeout=5_000)
    assert page.get_by_test_id("revisions-connect-repo").is_disabled()
    page.locator(".gh-step--now", has_text="Upload the history to GitHub").wait_for(timeout=10_000)
    page.locator(".gh-progress-foot", has_text="objects").wait_for(timeout=10_000)
    assert "3.00 MiB/s" in progress.inner_text()
    assert page.locator(".gh-step--done", has_text="Check the repository on GitHub").count() == 1

    # Closing the window does not stop it: the card keeps drawing the progress.
    page.locator("[role=dialog] button", has_text="Close (keeps running)").click()
    page.get_by_test_id("revisions-github-progress").wait_for(state="visible", timeout=5_000)

    card.locator(".gh-repo", has_text="acme/olano-history").wait_for(timeout=20_000)
    assert page.get_by_test_id("revisions-github-progress").count() == 0
    _no_horizontal_page_scroll(page)
