"""Read-only environment for non-admin members.

Non-admin members may browse and read — including a file's raw bytes for
the workspaces their grants cover, served with hardening headers — but the
runtime must block workspace writes and memory edits (HTTP layer), and the
security pipeline must deny mutating agent tools (tool layer). Chat — including image
attachments for inference — stays open because images are passed inline to the
model and never written to disk.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api


def _runner(model="anthropic:claude-haiku-4-5-20251001", reply="ok"):
    r = MagicMock()
    r.config = MagicMock(model=model)
    r.ainvoke = AsyncMock(return_value=reply)
    return r


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {"merli": _runner(reply="hi from merli")}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


def _admin(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"})
    return c


def _make_member(app, admin: TestClient, email="bob@x.com") -> tuple[TestClient, str]:
    uid = admin.post(
        "/api/admin/users", json={"email": email, "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": email, "password": "pw"})
    return member, uid


# ── HTTP layer ───────────────────────────────────────────────────────────────


def _seed_merli_workspace_file(rel: str, data: bytes) -> str:
    """Write a file into merli's (isolated-home) workspace; returns its URL path."""
    from olano.config.home import get_olano_paths

    d = get_olano_paths().agents_dir / "merli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json5").write_text(json.dumps({"name": "merli"}), encoding="utf-8")
    target = d / "workspace" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"/api/files-raw/{rel}?agent=merli"


def test_member_with_a_grant_reads_raw_bytes_with_hardening_headers(app):
    """Reads are open to members for the workspaces their grants cover: the
    chart an agent just made renders inline and downloads from its card."""
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    assert admin.post("/api/agents/merli/access", json={"user_id": uid}).status_code == 200
    url = _seed_merli_workspace_file("reports/chart.png", b"\x89PNG")

    resp = member.get(url)
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in resp.headers  # an image never runs

    # The Download action on a chat card: same bytes, saved instead of shown.
    dl = member.get(url + "&download=1")
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    assert "chart.png" in dl.headers["content-disposition"]


def test_member_without_a_grant_still_cannot_read_raw_bytes(app):
    admin = _admin(app)
    member, _uid = _make_member(app, admin)
    url = _seed_merli_workspace_file("reports/secret-chart.png", b"\x89PNG")
    assert member.get(url).status_code == 403


def test_script_capable_files_are_served_sandboxed(app):
    """A workspace HTML or SVG opened directly can never run as the dashboard."""
    admin = _admin(app)
    for rel, body in (
        ("reports/page.html", b"<script>alert(1)</script>"),
        ("reports/chart.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
    ):
        resp = admin.get(_seed_merli_workspace_file(rel, body))
        assert resp.status_code == 200, rel
        assert resp.headers["content-security-policy"] == "sandbox", rel
        assert resp.headers["x-content-type-options"] == "nosniff", rel


def test_member_blocked_from_file_writes_and_downloads(app):
    admin = _admin(app)
    member, _ = _make_member(app, admin)

    # Workspace mutations — every write/move/duplicate/delete is denied (403).
    assert member.put("/api/files/notes.txt", json={"content": "x"}).status_code == 403
    assert member.delete("/api/files/notes.txt").status_code == 403
    assert (
        member.post("/api/files/create", json={"path": "f.txt", "type": "file"}).status_code == 403
    )
    assert member.post("/api/files/move", json={"source": "a", "dest": "b"}).status_code == 403
    assert member.post("/api/files/duplicate", json={"path": "a"}).status_code == 403
    assert (
        member.post(
            "/api/files/upload?name=f.bin",
            content=b"x",
            headers={"Content-Type": "application/octet-stream"},
        ).status_code
        == 403
    )
    # Raw download (the "save a file to my machine" surface) is denied even as GET.
    # Raw reads are no longer a blanket 403 (see the tests above); an
    # ungranted member is refused by the handler's workspace scoping instead.
    assert member.get("/api/files-raw/anything.bin?agent=merli").status_code == 403


def test_member_blocked_from_memory_writes(app):
    admin = _admin(app)
    member, _ = _make_member(app, admin)

    assert member.post("/api/memory", json={"content": "fact", "section": "X"}).status_code == 403
    assert member.put("/api/memory", json={"content": "whole doc"}).status_code == 403


def test_member_can_still_read_files_and_memory(app):
    admin = _admin(app)
    member, uid = _make_member(app, admin)

    # Workspace reads are agent-scoped: with no grant, even a GET of the
    # default agent's workspace is denied (the workspace belongs to an agent
    # the member cannot access)…
    assert member.get("/api/files").status_code == 403
    assert member.get("/api/memory").status_code == 403

    # …but once the agent is granted, the read-only gate lets GETs through
    # (handler success is incidental; what matters is reads aren't 403'd).
    admin.post("/api/agents/merli/access", json={"user_id": uid})
    assert member.get("/api/files").status_code != 403  # default agent = merli
    assert member.get("/api/files?agent=merli").status_code != 403
    assert member.get("/api/memory?agent=merli").status_code != 403


def test_admin_is_not_read_only(app):
    """The same surfaces are reachable for an admin (gate returns no 403)."""
    admin = _admin(app)
    assert admin.put("/api/memory", json={"content": "doc"}).status_code != 403
    assert admin.get("/api/files-raw/x.bin").status_code != 403


def test_member_chat_with_image_attachment_is_allowed(app):
    """Image attachments stay usable for inference — read-only does not block
    chat, and the image is passed inline (never written to the workspace)."""
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    resp = member.post(
        "/api/chat",
        json={
            "agent": "merli",
            "message": "what is this?",
            "attachments": [
                {"kind": "image", "name": "shot.png", "mime": "image/png", "data": png}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["response"] == "hi from merli"


# ── tool layer (security pipeline) ─────────────────────────────────────────────


def test_pipeline_denies_mutating_tools_for_members(base_config):
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import ToolMetadata

    mutating = ToolMetadata(capability="mutating", risk="medium")
    read_only = ToolMetadata(capability="read_only", risk="low")

    # Non-admin member context → mutating tools blocked at layer 6, reads allowed.
    with push_request_context(authenticated=True, is_owner=False, is_admin=False):
        denied = evaluate_tool_access("write_file", mutating, base_config)
        assert not denied.allowed
        assert denied.layer == 6
        assert evaluate_tool_access("read_file", read_only, base_config).allowed

    # Owner / single-user (is_owner=True) → mutating tools allowed.
    with push_request_context(authenticated=True, is_owner=True, is_admin=True):
        assert evaluate_tool_access("write_file", mutating, base_config).allowed

    # System / unauthenticated context (scheduler, self-learning) → not a member.
    with push_request_context(authenticated=False, is_owner=False, is_admin=False):
        assert evaluate_tool_access("write_file", mutating, base_config).allowed


def test_pipeline_peer_hops_are_not_members(base_config):
    """A2A (talk_to_agent) callees get full tool service: the deployment admin
    already governs which agents may reach which via the org talk-to policy, so
    an admitted peer is not subject to the member read-only gate."""
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import ToolMetadata

    mutating = ToolMetadata(capability="mutating", risk="medium")
    with push_request_context(
        source="peer",
        channel="talk_to_agent",
        user_id="marcus",
        authenticated=True,
        is_owner=False,
        is_admin=False,
    ):
        assert evaluate_tool_access("zohomail_sendemail", mutating, base_config).allowed


def test_pipeline_peer_mutating_from_manager_only(base_config):
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import ToolMetadata

    mutating = ToolMetadata(capability="mutating", risk="medium")
    read_only = ToolMetadata(capability="read_only", risk="low")
    base_config.org.mutating_from_manager_only = True
    base_config.org.manager = "Marcus"

    def peer(sender: str):
        return push_request_context(
            source="peer",
            channel="talk_to_agent",
            user_id=sender,
            authenticated=True,
            is_owner=False,
            is_admin=False,
        )

    # The manager matches case-insensitively → mutating allowed.
    with peer("marcus"):
        assert evaluate_tool_access("gmail_send", mutating, base_config).allowed

    # Any other admitted peer → mutating blocked at layer 6, reads still fine.
    with peer("cindy"):
        denied = evaluate_tool_access("gmail_send", mutating, base_config)
        assert not denied.allowed
        assert denied.layer == 6
        assert "manager" in denied.reason
        assert evaluate_tool_access("gmail_list", read_only, base_config).allowed

    # Flag on but no manager declared → every peer gets read-only service.
    base_config.org.manager = None
    with peer("marcus"):
        assert not evaluate_tool_access("gmail_send", mutating, base_config).allowed
