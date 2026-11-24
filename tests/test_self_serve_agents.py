"""Self-serve agent creation: a member may make their own bot.

The feature is one narrow exemption to "agent configuration is admin-only"
(``runtime/auth.py:_member_forbidden``), so what these tests are really
guarding is the *shape* of that exemption:

* it is **on by default** (each member gets their own bot out of the box) and
  an explicit ``enabled: false`` closes it everywhere at once;
* it opens exactly ``POST /api/agents`` — not a sub-path, not another method;
* the middleware is never the only guard: the handler refuses on its own;
* the limits (quota, archetype allow-list) bind members and nobody else.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.requests import Request

from olano.config.home import get_olano_paths, write_home_config_text
from olano.config.system_settings import (
    SelfServeAgentSettings,
    load_self_serve_agent_settings,
    self_serve_agents_enabled,
    self_serve_archetype_allowed,
)
from olano.runtime.api import create_api
from olano.runtime.auth import _member_forbidden


def write_config_block(key: str, block: dict | None) -> None:
    """Merge one top-level block into the tmp home's ``config.yaml``.

    Deliberately goes through the real file rather than monkeypatching the
    loader: the point of most of these tests is that the *loader* reads the
    switch the way an operator would write it.
    """
    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    if block is None:
        raw.pop(key, None)
    else:
        raw[key] = block
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


def enable_self_serve(**overrides: object) -> None:
    write_config_block("self_serve_agents", {"enabled": True, **overrides})


def disable_self_serve() -> None:
    write_config_block("self_serve_agents", {"enabled": False})


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [],
            "query_string": b"",
        }
    )


# ── settings ─────────────────────────────────────────────────────────────────


class TestSettings:
    def test_absent_block_is_on(self):
        """An install that never writes the block hands members the New-bot
        button — bounded by the default quota and the slug validation."""
        settings = load_self_serve_agent_settings()
        assert settings.enabled is True
        assert settings.max_per_user == 3
        assert settings.allowed_archetypes == []
        assert self_serve_agents_enabled() is True

    def test_explicit_disable_wins(self):
        disable_self_serve()
        assert self_serve_agents_enabled() is False

    def test_block_is_read_from_config_yaml(self):
        enable_self_serve(max_per_user=1, allowed_archetypes=["merli"])
        settings = load_self_serve_agent_settings()
        assert settings.enabled is True
        assert settings.max_per_user == 1
        assert settings.allowed_archetypes == ["merli"]

    def test_quota_is_clamped(self):
        assert SelfServeAgentSettings(max_per_user=0).max_per_user == 1
        assert SelfServeAgentSettings(max_per_user=10_000).max_per_user == 100

    def test_hand_edited_scalar_archetype_normalizes(self):
        """``allowed_archetypes: merli`` must narrow, not raise.

        A validation error here would fall back to the defaults — an *empty*
        allow-list, i.e. "any archetype" — turning a typo into a wider
        permission than the operator wrote.
        """
        assert SelfServeAgentSettings(allowed_archetypes="merli").allowed_archetypes == ["merli"]
        assert SelfServeAgentSettings(allowed_archetypes=None).allowed_archetypes == []

    def test_empty_allow_list_means_any_archetype(self):
        assert self_serve_archetype_allowed("anything") is True
        assert self_serve_archetype_allowed("") is True

    def test_non_empty_allow_list_is_strict(self):
        enable_self_serve(allowed_archetypes=["merli"])
        assert self_serve_archetype_allowed("merli") is True
        assert self_serve_archetype_allowed("agentfather") is False
        # A from-scratch create names no archetype, and "none of them" is not
        # on the operator's menu either.
        assert self_serve_archetype_allowed("") is False

    def test_unreadable_config_reads_as_the_defaults(self):
        """A broken config.yaml yields the all-default settings — enabled, with
        the default quota — the same posture-default shape as
        ``multi_user_enabled``. The load must not raise either way."""
        paths = get_olano_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.config_path.write_text("{{{ not yaml")
        settings = load_self_serve_agent_settings()
        assert settings.enabled is True
        assert settings.max_per_user == 3


# ── the middleware exemption ─────────────────────────────────────────────────


class TestMemberForbidden:
    def test_agent_creation_is_open_by_default(self):
        assert _member_forbidden(_request("POST", "/api/agents")) is False

    def test_agent_creation_blocks_when_disabled(self):
        disable_self_serve()
        assert _member_forbidden(_request("POST", "/api/agents")) is True

    def test_agent_creation_opens_when_enabled(self):
        enable_self_serve()
        assert _member_forbidden(_request("POST", "/api/agents")) is False

    def test_exemption_does_not_reach_sub_paths(self):
        """Editing, deleting or reloading an existing agent stays admin-only."""
        enable_self_serve()
        for method, path in (
            ("PUT", "/api/agents/merli"),
            ("PATCH", "/api/agents/merli"),
            ("DELETE", "/api/agents/merli"),
            ("POST", "/api/agents/merli/reload"),
            ("POST", "/api/agents/merli/reload-vault"),
        ):
            assert _member_forbidden(_request(method, path)) is True, f"{method} {path}"

    def test_exemption_does_not_reach_other_methods_on_the_collection(self):
        enable_self_serve()
        for method in ("PUT", "PATCH", "DELETE"):
            assert _member_forbidden(_request(method, "/api/agents")) is True, method

    def test_nothing_else_moves(self):
        """The exemption must not widen any other member gate."""
        enable_self_serve()
        for method, path in (
            ("GET", "/api/admin/users"),
            ("POST", "/api/vault/secrets"),
            ("PUT", "/api/files/notes.txt"),
            ("POST", "/api/memory"),
        ):
            assert _member_forbidden(_request(method, path)) is True, f"{method} {path}"
        # …and the surfaces members already had stay open. Raw file reads are
        # open at this layer too: the handler scopes them to the member's
        # granted workspaces (tests/runtime/test_readonly_member.py).
        assert _member_forbidden(_request("GET", "/api/agents")) is False
        assert _member_forbidden(_request("POST", "/api/agents/merli/threads")) is False
        assert _member_forbidden(_request("GET", "/api/files-raw/x.bin")) is False


# ── HTTP end to end ──────────────────────────────────────────────────────────


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


def _admin(app) -> TestClient:
    c = TestClient(app)
    r = c.post(
        "/api/auth/bootstrap",
        json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return c


def _member(app, admin: TestClient, email: str = "bob@x.com") -> tuple[TestClient, str]:
    uid = admin.post(
        "/api/admin/users", json={"email": email, "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": email, "password": "pw"}).status_code == 200
    return c, uid


def _owned(uid: str) -> list[str]:
    from olano.users.store import get_user_store

    return [
        row["agent_name"]
        for row in get_user_store().db.list_agents_for_user(uid)
        if row["role"] == "owner"
    ]


class TestCapability:
    def test_status_advertises_the_default_on(self, app):
        client = TestClient(app)
        assert client.get("/api/status").json()["capabilities"]["self_serve_agents"] is True

    def test_status_reflects_a_disabled_deployment(self):
        # A fresh app, because /api/status caches its capability payload for a
        # few seconds and the switch is read while that cache is cold.
        disable_self_serve()
        runtime = MagicMock()
        runtime._runners = {}
        client = TestClient(create_api(runtime))
        assert client.get("/api/status").json()["capabilities"]["self_serve_agents"] is False


class TestSelfServeCreate:
    def test_member_is_refused_while_the_switch_is_off(self, app):
        admin = _admin(app)
        member, uid = _member(app, admin)
        disable_self_serve()
        resp = member.post("/api/agents", json={"name": "bobbot"})
        assert resp.status_code == 403
        assert _owned(uid) == []

    def test_member_creates_and_owns_the_agent(self, app):
        admin = _admin(app)
        member, uid = _member(app, admin)
        enable_self_serve()

        resp = member.post("/api/agents", json={"name": "bobbot"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "bobbot"
        # The creator — not the admin who happened to bootstrap the box —
        # must come out of this owning the agent.
        assert _owned(uid) == ["bobbot"]

    def test_quota_returns_429(self, app):
        admin = _admin(app)
        member, uid = _member(app, admin)
        enable_self_serve(max_per_user=2)

        assert member.post("/api/agents", json={"name": "one"}).status_code == 200
        assert member.post("/api/agents", json={"name": "two"}).status_code == 200
        third = member.post("/api/agents", json={"name": "three"})
        assert third.status_code == 429
        assert "self_serve_quota_exceeded" in third.json()["detail"]
        assert sorted(_owned(uid)) == ["one", "two"]

    def test_granted_access_does_not_consume_quota(self, app):
        """Being invited to someone else's agent is not spending your own
        allowance — the quota counts ownership, not access."""
        admin = _admin(app)
        member, uid = _member(app, admin)
        enable_self_serve(max_per_user=1)

        # The admin grants the member access to an agent they do not own.
        from olano.users.store import get_user_store

        get_user_store().db.grant_access(uid, "someone_elses_bot", role="member")

        assert member.post("/api/agents", json={"name": "mine"}).status_code == 200
        assert member.post("/api/agents", json={"name": "second"}).status_code == 429

    def test_archetype_allow_list_refuses_others(self, app):
        admin = _admin(app)
        member, _ = _member(app, admin)
        enable_self_serve(allowed_archetypes=["merli"])

        blocked = member.post("/api/agents", json={"name": "af", "archetype": "agentfather"})
        assert blocked.status_code == 403
        assert "archetype_not_allowed" in blocked.json()["detail"]
        # A from-scratch create is not on the menu either.
        assert member.post("/api/agents", json={"name": "scratch"}).status_code == 403

    def test_allowed_archetype_installs(self, app):
        admin = _admin(app)
        member, uid = _member(app, admin)
        enable_self_serve(allowed_archetypes=["merli"])

        resp = member.post("/api/agents", json={"name": "bobsmerli", "archetype": "merli"})
        assert resp.status_code == 200, resp.text
        assert _owned(uid) == ["bobsmerli"]

    def test_archetype_create_refuses_fields_it_cannot_honour(self, app):
        admin = _admin(app)
        enable_self_serve()
        resp = admin.post(
            "/api/agents",
            json={"name": "clashy", "archetype": "merli", "system_prompt": "be different"},
        )
        assert resp.status_code == 422
        assert "system_prompt" in resp.json()["detail"]

    def test_member_cannot_name_admin_only_tools(self, app):
        """Editing an agent's tools is admin-only, so this create is the one
        member route that could name the tools an administrator alone grants:
        the fleet-scoped admin toolkit and the Twilio template admin toolkit."""
        admin = _admin(app)
        member, uid = _member(app, admin)
        enable_self_serve()

        for tool in ("whatsapp_template_create", "whatsapp_template_delete", "olano_tail_log"):
            resp = member.post("/api/agents", json={"name": "bobbot", "tools": [tool]})
            assert resp.status_code == 403, tool
            assert "admin_only_tools" in resp.json()["detail"]
            assert tool in resp.json()["detail"]
        assert _owned(uid) == []

        # Sending an approved template is not an administrator's grant: it comes
        # with the WhatsApp (Twilio) connector on any agent.
        resp = member.post(
            "/api/agents", json={"name": "bobbot", "tools": ["whatsapp_template_send"]}
        )
        assert resp.status_code == 200, resp.text
        assert _owned(uid) == ["bobbot"]

    def test_handler_refuses_even_if_the_middleware_lets_it_through(self, app, monkeypatch):
        """Defence in depth: the exemption must not be the only guard.

        With the switch off, a member reaching the handler (a future refactor
        of the gate, a mounted sub-app, a direct call) still gets a 403.
        """
        admin = _admin(app)
        member, uid = _member(app, admin)
        disable_self_serve()
        monkeypatch.setattr("olano.runtime.auth._member_forbidden", lambda request: False)

        resp = member.post("/api/agents", json={"name": "sneaky"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "self_serve_agents_disabled"
        assert _owned(uid) == []


class TestAdminsAreUnaffected:
    def test_admin_creates_with_the_switch_off(self, app):
        admin = _admin(app)
        disable_self_serve()
        assert admin.post("/api/agents", json={"name": "adminbot"}).status_code == 200

    def test_admin_ignores_the_quota(self, app):
        admin = _admin(app)
        enable_self_serve(max_per_user=1)
        assert admin.post("/api/agents", json={"name": "a1"}).status_code == 200
        assert admin.post("/api/agents", json={"name": "a2"}).status_code == 200
        assert admin.post("/api/agents", json={"name": "a3"}).status_code == 200

    def test_admin_ignores_the_archetype_allow_list(self, app):
        admin = _admin(app)
        enable_self_serve(allowed_archetypes=["nothing-real"])
        assert admin.post("/api/agents", json={"name": "a1"}).status_code == 200

    def test_admin_may_grant_admin_only_tools(self, app):
        admin = _admin(app)
        resp = admin.post(
            "/api/agents", json={"name": "twilioadmin", "tools": ["whatsapp_template_create"]}
        )
        assert resp.status_code == 200, resp.text

    def test_single_user_install_is_untouched(self, app):
        """No users, no multi-user mode: the implicit owner is an admin and the
        create path is byte-identical to what it was before self-serve."""
        client = TestClient(app)
        assert client.post("/api/agents", json={"name": "solo"}).status_code == 200


# ── review regression: the name is a filesystem path, so it is a slug ────────


class TestAgentNameValidation:
    """``agents_dir / body.name`` plus a recursive mkdir makes an unvalidated
    name a path-traversal primitive — and self-serve opened the endpoint to
    non-admin members whose only check was a client-side regex."""

    @pytest.mark.parametrize(
        "name",
        [
            "../../../../tmp/evil",
            "x/y",
            "..",
            ".hidden",
            "UPPER",
            "spa ce",
            "a" * 65,
            "",
        ],
    )
    def test_traversal_and_junk_names_are_refused(self, app, name):
        enable_self_serve()
        admin = _admin(app)
        member, uid = _member(app, admin)
        resp = member.post("/api/agents", json={"name": name, "description": "d"})
        assert resp.status_code == 422, (name, resp.text)
        # An empty handle is no longer junk in itself — it means "mint one from
        # the display name" — but with no display name either there is nothing
        # to mint from, and the refusal names that instead.
        assert ("display_name_required" if name == "" else "invalid_agent_name") in resp.text
        assert _owned(uid) == []

    def test_admins_are_held_to_the_same_rule(self, app):
        admin = _admin(app)
        resp = admin.post("/api/agents", json={"name": "../oops", "description": "d"})
        assert resp.status_code == 422
        assert "invalid_agent_name" in resp.text

    def test_a_plain_slug_is_not_what_gets_refused(self, app):
        enable_self_serve()
        admin = _admin(app)
        member, _uid = _member(app, admin)
        resp = member.post("/api/agents", json={"name": "sales-helper_2", "description": "d"})
        assert "invalid_agent_name" not in resp.text
