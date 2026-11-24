"""``POST /api/files/upload``: the Files view's Upload button.

The body is the file's raw bytes. These pin the write contract (where the file
lands, conflicts, the name and path jail, the size cap streamed rather than
buffered, no half-written file left behind) and that a cross-site form cannot
drive the raw-body endpoint.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

OCTET = {"Content-Type": "application/octet-stream"}
XLSX_BYTES = b"PK\x03\x04" + bytes(range(256)) * 4


@pytest.fixture
def client(tmp_workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    class _Runtime:
        _runners: dict = {}
        _channels: list = []

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    runtime = _Runtime()
    runtime._runners = {"test_agent": type("R", (), {"config": config})()}
    app = create_api(runtime)
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        yield TestClient(app)


def _upload(client, name, data=XLSX_BYTES, *, folder="", overwrite=False, headers=OCTET):
    params = {"name": name, "dir": folder}
    if overwrite:
        params["overwrite"] = "true"
    return client.post("/api/files/upload", params=params, content=data, headers=headers)


def test_upload_writes_the_bytes_into_the_chosen_folder(client, tmp_workspace) -> None:
    resp = _upload(client, "PM Trade Blotter.xlsx", folder="reports/q3")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "path": "reports/q3/PM Trade Blotter.xlsx",
        "size": len(XLSX_BYTES),
    }
    # A missing folder is created; the name is kept as the user wrote it.
    assert (tmp_workspace / "reports" / "q3" / "PM Trade Blotter.xlsx").read_bytes() == XLSX_BYTES
    # Nothing but the file itself is left behind.
    assert [p.name for p in (tmp_workspace / "reports" / "q3").iterdir()] == [
        "PM Trade Blotter.xlsx"
    ]


def test_upload_to_the_workspace_root(client, tmp_workspace) -> None:
    assert _upload(client, "notes.txt", b"hi").json()["path"] == "notes.txt"
    assert (tmp_workspace / "notes.txt").read_bytes() == b"hi"


def test_existing_file_conflicts_unless_overwrite(client, tmp_workspace) -> None:
    (tmp_workspace / "a.bin").write_bytes(b"old")

    resp = _upload(client, "a.bin", b"new")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "file_exists"
    assert (tmp_workspace / "a.bin").read_bytes() == b"old"

    assert _upload(client, "a.bin", b"new", overwrite=True).status_code == 200
    assert (tmp_workspace / "a.bin").read_bytes() == b"new"


def test_a_folder_is_never_overwritten(client, tmp_workspace) -> None:
    (tmp_workspace / "reports").mkdir()
    resp = _upload(client, "reports", overwrite=True)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "folder_exists"
    assert (tmp_workspace / "reports").is_dir()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b.txt", "..\\x.txt", "x\x00.txt", "a" * 256])
def test_name_must_be_one_path_component(client, tmp_workspace, name) -> None:
    assert _upload(client, name).status_code == 400


def test_folder_outside_the_workspace_is_refused(client, tmp_workspace) -> None:
    assert _upload(client, "evil.txt", folder="../..").status_code == 403
    assert not (tmp_workspace.parent.parent / "evil.txt").exists()


def test_cross_site_form_content_types_are_refused(client, tmp_workspace) -> None:
    for ct in (
        "multipart/form-data; boundary=x",
        "text/plain",
        "application/x-www-form-urlencoded",
    ):
        assert _upload(client, "f.txt", headers={"Content-Type": ct}).status_code == 415
    assert not (tmp_workspace / "f.txt").exists()


def test_oversize_upload_is_refused_and_leaves_nothing(client, tmp_workspace, monkeypatch) -> None:
    import olano.runtime.routes.files as files_routes

    monkeypatch.setattr(files_routes, "FILES_UPLOAD_MAX_BYTES", 10)

    # Declared length over the cap: refused before a byte is written.
    assert _upload(client, "big.bin", b"x" * 11).status_code == 413

    # A body that streams past the cap (no usable Content-Length) is cut off
    # mid-stream and its temp file removed.
    def body():
        yield b"x" * 8
        yield b"x" * 8

    resp = client.post(
        "/api/files/upload", params={"name": "big.bin"}, content=body(), headers=OCTET
    )
    assert resp.status_code == 413
    assert list(tmp_workspace.glob("*big.bin*")) == []
