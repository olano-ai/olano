"""Per-agent access control, member restrictions, and per-human thread isolation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths
from olano.runtime.api import create_api


def _runner(model="anthropic:claude-haiku-4-5-20251001", reply="ok"):
    r = MagicMock()
    r.config = MagicMock(model=model)
    r.ainvoke = AsyncMock(return_value=reply)
    return r


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {
        "merli": _runner(reply="hi from merli"),
        "scout": _runner(reply="hi from scout"),
    }
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


def test_member_chat_requires_access(app):
    admin = _admin(app)
    member, uid = _make_member(app, admin)

    # No grant yet → 403 on both agents.
    assert member.post("/api/chat", json={"agent": "merli", "message": "hi"}).status_code == 403
    assert member.post("/api/chat", json={"agent": "scout", "message": "hi"}).status_code == 403

    # Grant access to merli only.
    assert admin.post("/api/agents/merli/access", json={"user_id": uid}).status_code == 200

    ok = member.post("/api/chat", json={"agent": "merli", "message": "hi"})
    assert ok.status_code == 200
    assert ok.json()["response"] == "hi from merli"
    # Still no access to scout.
    assert member.post("/api/chat", json={"agent": "scout", "message": "hi"}).status_code == 403


def test_admin_can_chat_any_agent(app):
    admin = _admin(app)
    assert admin.post("/api/chat", json={"agent": "scout", "message": "hi"}).status_code == 200


def test_agents_list_is_filtered_for_members(app):
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})

    admin_names = {a["name"] for a in admin.get("/api/agents").json()}
    assert {"merli", "scout"} <= admin_names

    member_names = {a["name"] for a in member.get("/api/agents").json()}
    assert member_names == {"merli"}


def test_hidden_agent_excluded_from_dashboard_listing():
    """A `hidden: true` agent never appears in GET /api/agents — even to an admin.

    Booted runners and disk-only agents are both filtered. Internal/test agents
    set this so they stay out of the dashboard while remaining usable by name.
    """
    runtime = MagicMock()
    visible = _runner()
    visible.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001", hidden=False)
    hidden = _runner()
    hidden.config = MagicMock(model="openai:gpt-5.4-mini", hidden=True)
    runtime._runners = {"merli": visible, "test-agent-openai": hidden}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    app = create_api(runtime)

    admin = _admin(app)
    names = {a["name"] for a in admin.get("/api/agents").json()}
    assert "merli" in names
    assert "test-agent-openai" not in names


def test_member_blocked_from_admin_surfaces(app):
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})

    # Agent config mutation, vault, system, user admin → all forbidden for members.
    assert member.delete("/api/agents/merli").status_code == 403
    assert member.get("/api/vault/status").status_code == 403
    assert member.get("/api/admin/users").status_code == 403


def test_owner_can_manage_access_but_not_config(app):
    admin = _admin(app)
    # Make carol the OWNER of merli.
    carol, carol_id = _make_member(app, admin, email="carol@x.com")
    admin.post("/api/agents/merli/access", json={"user_id": carol_id, "role": "owner"})

    # Owner can grant access to another member…
    dave, dave_id = _make_member(app, admin, email="dave@x.com")
    assert carol.post("/api/agents/merli/access", json={"user_id": dave_id}).status_code == 200
    listing = carol.get("/api/agents/merli/access")
    assert listing.status_code == 200
    assert {m["id"] for m in listing.json()["members"]} >= {carol_id, dave_id}
    # …but still cannot reconfigure the agent (admin-only).
    assert carol.delete("/api/agents/merli").status_code == 403


def _overview(client: TestClient) -> dict:
    res = client.get("/api/admin/access-overview")
    assert res.status_code == 200
    return res.json()


def _agent(overview: dict, name: str) -> dict:
    return next(a for a in overview["agents"] if a["name"] == name)


def test_access_overview_lists_every_agent_split_by_role(app):
    admin = _admin(app)
    bob, bob_id = _make_member(app, admin)
    carol, carol_id = _make_member(app, admin, email="carol@x.com")
    admin.post("/api/agents/merli/access", json={"user_id": bob_id})
    admin.post("/api/agents/merli/access", json={"user_id": carol_id, "role": "owner"})

    data = _overview(admin)
    assert {a["name"] for a in data["agents"]} >= {"merli", "scout"}

    merli = _agent(data, "merli")
    assert merli["owners"] == [carol_id]
    assert merli["users"] == [bob_id]
    # An agent nobody was granted still gets a row (it is not simply absent).
    assert _agent(data, "scout") == {"name": "scout", "owners": [], "users": []}

    # Users are returned once in a flat roster, referenced by id above.
    assert {u["id"] for u in data["users"]} >= {bob_id, carol_id}


def test_access_overview_reports_implicit_admin_owners(app):
    """Admins own every agent by rule and hold no agent_access row.

    The per-agent endpoint can't see them, which is why the overview reports
    them separately instead of leaving them invisible.
    """
    admin = _admin(app)
    admin_id = admin.get("/api/auth/me").json()["user"]["id"]
    second = admin.post(
        "/api/admin/users",
        json={"email": "boss@x.com", "password": "pw", "name": "Boss", "is_admin": True},
    ).json()["user"]["id"]

    data = _overview(admin)
    assert set(data["admin_ids"]) == {admin_id, second}

    # The freshly-promoted admin owns scout without any explicit grant …
    assert second not in _agent(data, "scout")["owners"]
    # … and the per-agent endpoint indeed cannot see them — the gap the
    # overview's admin_ids exists to close.
    members = admin.get("/api/agents/scout/access").json()["members"]
    assert second not in {m["id"] for m in members}


def test_access_overview_drops_grants_for_unknown_users(app):
    """A dangling agent_access row must not render as a blank person."""
    admin = _admin(app)
    from olano.runtime.auth import get_user_store

    get_user_store().db.grant_access("ghost-user", "merli", role="member")
    assert _agent(_overview(admin), "merli")["users"] == []


def test_access_overview_is_admin_only(app):
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid, "role": "owner"})
    # Owner of merli, but not a platform admin → no fleet-wide view.
    assert member.get("/api/admin/access-overview").status_code == 403


def test_access_bulk_grants_users_across_agents(app):
    admin = _admin(app)
    _, bob_id = _make_member(app, admin)
    _, carol_id = _make_member(app, admin, email="carol@x.com")

    res = admin.post(
        "/api/admin/access/bulk",
        json={"user_ids": [bob_id, carol_id], "agent_names": ["merli", "scout"], "role": "member"},
    )
    assert res.status_code == 200
    assert res.json()["changed"] == 4

    data = _overview(admin)
    for name in ("merli", "scout"):
        assert set(_agent(data, name)["users"]) == {bob_id, carol_id}


def test_access_bulk_revokes_when_role_is_null(app):
    admin = _admin(app)
    _, bob_id = _make_member(app, admin)
    admin.post(
        "/api/admin/access/bulk",
        json={"user_ids": [bob_id], "agent_names": ["merli", "scout"], "role": "owner"},
    )
    assert set(_agent(_overview(admin), "merli")["owners"]) == {bob_id}

    res = admin.post(
        "/api/admin/access/bulk",
        json={"user_ids": [bob_id], "agent_names": ["merli", "scout"], "role": None},
    )
    assert res.json()["changed"] == 2
    data = _overview(admin)
    assert _agent(data, "merli")["owners"] == []
    assert _agent(data, "scout")["owners"] == []


def test_access_bulk_ignores_unknown_users_and_agents(app):
    admin = _admin(app)
    _, bob_id = _make_member(app, admin)
    res = admin.post(
        "/api/admin/access/bulk",
        json={
            "user_ids": [bob_id, "nope"],
            "agent_names": ["merli", "not-an-agent"],
            "role": "member",
        },
    )
    assert res.json()["changed"] == 1
    assert _agent(_overview(admin), "merli")["users"] == [bob_id]


def test_threads_are_shared_by_everyone_with_agent_access(app):
    """Every thread an agent has had — another human's dashboard chat, a
    scheduled run, a messaging-platform chat — is visible to anyone who may
    chat with that agent. The gate is the agent grant, never the thread id."""
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})
    admin_id = admin.get("/api/auth/me").json()["user"]["id"]

    sessions = get_olano_paths().sessions_dir / "merli"
    sessions.mkdir(parents=True, exist_ok=True)
    scheduled = "scheduled_merli_1a2b3c4d@20260101T000000-ab12"
    seeded = {f"u:{uid}:bobthread", f"u:{admin_id}:adminthread", scheduled, "5550001234"}
    for tid in seeded:
        (sessions / f"{tid}.jsonl").write_text(
            json.dumps({"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00+00:00"})
            + "\n",
            encoding="utf-8",
        )

    member_threads = {t["id"] for t in member.get("/api/agents/merli/threads").json()}
    assert seeded <= member_threads
    admin_threads = {t["id"] for t in admin.get("/api/agents/merli/threads").json()}
    assert seeded <= admin_threads

    # A member reads (and may continue) another human's thread and a run's.
    assert member.get(f"/api/agents/merli/threads/u:{admin_id}:adminthread").status_code == 200
    assert member.get(f"/api/agents/merli/threads/{scheduled}").status_code == 200

    # …only for agents they were granted: the gate moved, it did not vanish.
    stranger, _ = _make_member(app, admin, email="eve@x.com")
    assert stranger.get("/api/agents/merli/threads").status_code == 403
    assert stranger.get(f"/api/agents/merli/threads/u:{uid}:bobthread").status_code == 403
    # An anonymous caller sees nothing.
    assert TestClient(app).get("/api/agents/merli/threads").status_code == 401


def test_scheduler_tasks_filtered_for_members(app):
    from olano.scheduler.db import SchedulerDB

    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})

    db = SchedulerDB()
    db.create_task(name="m-task", prompt="p", agent_name="merli")
    db.create_task(name="s-task", prompt="p", agent_name="scout")

    admin_names = {t["name"] for t in admin.get("/api/scheduler/tasks").json()}
    assert {"m-task", "s-task"} <= admin_names
    member_names = {t["name"] for t in member.get("/api/scheduler/tasks").json()}
    assert member_names == {"m-task"}  # only tasks for agents the member can access
