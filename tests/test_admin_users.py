"""Tests for the human-user admin service layer (``olano/admin/users.py``).

The autouse ``lionclaw_home`` fixture gives every test an isolated home (and
therefore an isolated ``users.db`` — the store singleton is keyed by path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano import admin
from olano.users.store import get_user_store


@pytest.fixture()
def seeded_admin():
    """The first admin, created the way onboarding does (directly, password set)."""
    return get_user_store().create_user(
        "root@example.com", "hunter2secret", name="Root Admin", is_admin=True
    )


def _write_home_agent(name: str) -> None:
    from olano.config.home import get_olano_paths

    path = get_olano_paths().root / "agents" / name / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name}), encoding="utf-8")


def _invite_token(payload: dict) -> str:
    return payload["invite"]["path"].rsplit("/", 1)[-1]


# ── create / resolve ─────────────────────────────────────────────────────────


def test_create_refuses_the_first_account() -> None:
    with pytest.raises(ValueError, match="first admin"):
        admin.create_user("first@example.com")


def test_resolver_accepts_email_username_and_id(seeded_admin) -> None:
    out = admin.create_user("dana@example.com", name="Dana Ray")
    uid = out["user"]["id"]
    username = out["user"]["username"]
    assert admin.get_user("dana@example.com")["id"] == uid
    assert admin.get_user(f"@{username}")["id"] == uid
    assert admin.get_user(username)["id"] == uid
    assert admin.get_user(uid)["id"] == uid


def test_resolver_names_the_accepted_forms(seeded_admin) -> None:
    with pytest.raises(ValueError, match="@username"):
        admin.get_user("nobody-here")


def test_create_is_invite_only(seeded_admin, monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    from olano.core import mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)

    out = admin.create_user("kai@example.com", name="Kai")
    assert out["status"] == "pending"
    assert out["user"]["is_active"] is False
    invite = out["invite"]
    assert invite["path"].startswith("/invite/")
    assert invite["single_use"] is True
    assert invite["expires_at"]
    # No public origin → no absolute URL, no QR, and a remediation note.
    assert invite["url"] is None
    assert invite["qr_png"] is None
    assert any("LIONCLAW_PUBLIC_URL" in n for n in invite["notes"])


def test_invite_builds_absolute_url_and_qr(seeded_admin, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://box.test")
    out = admin.create_user("mia@example.com", qr_agent="agentfather")
    invite = out["invite"]
    assert invite["url"] == "https://box.test" + invite["path"]
    qr = invite["qr_png"]
    assert qr and Path(qr).exists() and Path(qr).suffix == ".png"
    from olano.config.home import get_olano_paths

    uploads = get_olano_paths().agent_workspace_dir("agentfather") / "uploads"
    assert Path(qr).parent == uploads


def test_create_enforces_the_deployment_quota(seeded_admin, monkeypatch) -> None:
    from olano.deployment import enforcement

    denied = enforcement.Decision(
        allowed=False,
        code="deployment_limit_max_users",
        message="User limit reached (3).",
        remediation="Upgrade the plan at cloud.olano.ai.",
    )
    monkeypatch.setattr(enforcement, "can_create_user", lambda count: denied)
    with pytest.raises(PermissionError) as err:
        admin.create_user("late@example.com")
    assert "User limit reached" in str(err.value)
    assert "cloud.olano.ai" in str(err.value)


def test_duplicate_email_and_taken_username_surface(seeded_admin) -> None:
    from olano.users.db import DuplicateEmailError, DuplicateUsernameError

    admin.create_user("kim@example.com", username="kim")
    with pytest.raises(DuplicateEmailError):
        admin.create_user("kim@example.com")
    with pytest.raises(DuplicateUsernameError):
        admin.create_user("kim2@example.com", username="kim")


# ── reset (the password flow) ────────────────────────────────────────────────


def test_reset_round_trips_through_accept_invite(seeded_admin) -> None:
    admin.create_user("ana@example.com")
    reset = admin.reset_user_password("ana@example.com")
    store = get_user_store()
    accepted = store.accept_invite(_invite_token(reset), "brand-new-pass")
    assert accepted is not None and accepted.is_active
    assert store.authenticate("ana@example.com", "brand-new-pass") is not None


def test_reset_works_for_active_users_and_voids_the_old_link(seeded_admin) -> None:
    first = admin.reset_user_password("root@example.com")
    second = admin.reset_user_password("root@example.com")
    store = get_user_store()
    assert store.get_invite(_invite_token(first)) is None  # replaced
    assert store.get_invite(_invite_token(second)) is not None


# ── roles / update / delete ──────────────────────────────────────────────────


def test_set_user_role_and_last_admin_guard(seeded_admin) -> None:
    out = admin.create_user("lee@example.com")
    promoted = admin.set_user_role("lee@example.com", "admin")
    assert promoted["user"]["is_admin"] is True
    get_user_store().accept_invite(_invite_token(out), "a-strong-pass")

    demoted = admin.set_user_role("root@example.com", "member")
    assert demoted["user"]["is_admin"] is False
    with pytest.raises(ValueError, match="last active admin"):
        admin.set_user_role("lee@example.com", "member")
    with pytest.raises(ValueError, match="role must be"):
        admin.set_user_role("lee@example.com", "owner")


def test_update_user_allowlist_and_steering_refusals(seeded_admin) -> None:
    for field, hint in (
        ("username", "permanent"),
        ("password", "reset_user_password"),
        ("is_admin", "set_user_role"),
    ):
        with pytest.raises(ValueError, match=hint):
            admin.update_user("root@example.com", {field: "x"})
    with pytest.raises(ValueError, match="unknown field"):
        admin.update_user("root@example.com", {"favourite_color": "red"})
    with pytest.raises(ValueError, match="blank"):
        admin.update_user("root@example.com", {"name": "  "})
    with pytest.raises(ValueError, match="ui_mode"):
        admin.update_user("root@example.com", {"ui_mode": "fancy"})

    out = admin.update_user("root@example.com", {"name": "Root A.", "timezone": "Europe/Madrid"})
    assert out["user"]["name"] == "Root A."
    assert out["user"]["timezone"] == "Europe/Madrid"


def test_title_is_editable_and_clearable(seeded_admin) -> None:
    """A person's position, shown under their name. Unlike `name` it may be
    emptied — "no position" is a real answer, not a blank slipping through."""
    out = admin.update_user("root@example.com", {"title": "  CTO  "})
    assert out["user"]["title"] == "CTO"
    out = admin.update_user("root@example.com", {"title": ""})
    assert out["user"]["title"] == ""


def test_create_user_records_the_title(seeded_admin) -> None:
    out = admin.create_user("kai@example.com", name="Kai", title="Head of Sales")
    assert out["user"]["title"] == "Head of Sales"
    assert admin.get_user("kai@example.com")["title"] == "Head of Sales"


def test_disabling_a_user_kills_their_sessions(seeded_admin) -> None:
    out = admin.create_user("sam@example.com")
    store = get_user_store()
    user = store.accept_invite(_invite_token(out), "sam-password")
    token = store.issue_session(user)
    assert store.resolve_session(token) is not None

    admin.update_user("sam@example.com", {"is_active": False})
    assert store.resolve_session(token) is None  # inactive users never resolve

    admin.update_user("sam@example.com", {"is_active": True})
    assert store.resolve_session(token) is not None


def test_disable_last_admin_is_refused(seeded_admin) -> None:
    with pytest.raises(ValueError, match="last active admin"):
        admin.update_user("root@example.com", {"is_active": False})


def test_delete_user_and_last_admin_guard(seeded_admin) -> None:
    admin.create_user("tmp@example.com")
    deleted = admin.delete_user("tmp@example.com")
    assert deleted["deleted"]["email"] == "tmp@example.com"
    with pytest.raises(ValueError, match="@username"):
        admin.get_user("tmp@example.com")
    with pytest.raises(ValueError, match="last active admin"):
        admin.delete_user("root@example.com")


# ── per-agent access ─────────────────────────────────────────────────────────


def test_grant_and_revoke_agent_access(seeded_admin) -> None:
    _write_home_agent("helper")
    admin.create_user("zoe@example.com")

    with pytest.raises(ValueError, match="refusing to guess"):
        admin.grant_agent_access("zoe@example.com", "helper", role="admin")
    with pytest.raises(ValueError, match="unknown agent"):
        admin.grant_agent_access("zoe@example.com", "no-such-agent-xyz")

    granted = admin.grant_agent_access("zoe@example.com", "helper", role="member")
    assert granted["granted"]["role"] == "member"
    assert {"agent_name": "helper", "role": "member"} in granted["user"]["agents"]

    promoted = admin.grant_agent_access("zoe@example.com", "helper", role="owner")
    assert promoted["granted"]["role"] == "owner"

    revoked = admin.revoke_agent_access("zoe@example.com", "helper")
    assert revoked["ok"] is True
    again = admin.revoke_agent_access("zoe@example.com", "helper")
    assert again["ok"] is False
    assert any("nothing changed" in n for n in again["notes"])


def test_access_overview_reports_grants_and_implicit_admins(seeded_admin) -> None:
    _write_home_agent("helper")
    out = admin.create_user("pat@example.com")
    admin.grant_agent_access("pat@example.com", "helper", role="owner")

    overview = admin.access_overview()
    assert seeded_admin.id in overview["admin_ids"]
    helper = next(a for a in overview["agents"] if a["name"] == "helper")
    assert out["user"]["id"] in helper["owners"]
    ids = {u["id"] for u in overview["users"]}
    assert {seeded_admin.id, out["user"]["id"]} <= ids


def test_list_users_carries_status_and_agents(seeded_admin) -> None:
    _write_home_agent("helper")
    admin.create_user("wes@example.com")
    admin.grant_agent_access("wes@example.com", "helper")
    rows = {u["email"]: u for u in admin.list_users()}
    assert rows["root@example.com"]["status"] == "active"
    assert rows["wes@example.com"]["status"] == "pending"
    assert rows["wes@example.com"]["agents"] == [{"agent_name": "helper", "role": "member"}]
    assert "password_hash" not in rows["root@example.com"]


# ── tool wrappers ────────────────────────────────────────────────────────────


def test_tool_wrappers_return_json(seeded_admin) -> None:
    from olano.tools import olano_admin

    listed = json.loads(olano_admin.get_tool("olano_list_users").invoke({}))
    assert any(u["email"] == "root@example.com" for u in listed)
    err = json.loads(olano_admin.get_tool("olano_get_user").invoke({"user": "ghost"}))
    assert "error" in err
