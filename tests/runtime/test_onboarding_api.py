"""Integration tests for the first-run admin onboarding endpoints.

The wizard is deployment-wide: /api/auth/me reports `onboarding.required`
for admins of a multi-user instance until any admin completes (or skips)
the tour via POST /api/onboarding/complete.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api
from olano.users import onboarding


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {}
    runtime._blocked_agents = {}
    api = create_api(runtime)
    api.state.test_runtime = runtime
    return api


def _runtime_of(app):
    return app.state.test_runtime


def _bootstrap(client: TestClient, email="admin@x.com", password="pw", name="Admin"):
    return client.post(
        "/api/auth/bootstrap", json={"email": email, "password": password, "name": name}
    )


def test_me_reports_onboarding_required_for_fresh_admin(app):
    admin = TestClient(app)
    assert _bootstrap(admin).status_code == 200

    me = admin.get("/api/auth/me").json()
    assert me["onboarding"]["required"] is True

    state = admin.get("/api/onboarding")
    assert state.status_code == 200
    assert state.json()["required"] is True
    assert state.json()["state"] == {}


def test_complete_clears_required_and_persists_steps(app):
    admin = TestClient(app)
    _bootstrap(admin)

    done = admin.post(
        "/api/onboarding/complete",
        json={
            "steps": {
                "profile": "done",
                "provider": "skipped",
                "agentfather": "done",
                # An id missing from KNOWN_STEPS is silently dropped, so a new
                # step lands in the state file only once the allow-list knows
                # about it — and the retired ids an older bundle still posts
                # ("password", "vault", "account", …) fall out the same way.
                "password": "done",
                "bogus": "done",
            }
        },
    )
    assert done.status_code == 200
    state = done.json()["state"]
    assert state["completed"] is True
    assert state["skipped"] is False
    assert state["steps"] == {
        "profile": "done",
        "provider": "skipped",
        "agentfather": "done",
    }  # password + bogus dropped
    assert state["completed_by"]
    # No Agent Father on this box → nowhere to land but Overview.
    assert done.json()["greeting"] is None

    # Flag clears everywhere, including for a different admin session.
    assert admin.get("/api/auth/me").json()["onboarding"]["required"] is False
    assert admin.get("/api/onboarding").json()["required"] is False
    # Survives as a file (deployment-wide, not per-session).
    assert onboarding.is_completed()


def test_skip_all_marks_completed(app):
    admin = TestClient(app)
    _bootstrap(admin)

    done = admin.post("/api/onboarding/complete", json={"skipped": True})
    assert done.status_code == 200
    assert done.json()["state"]["skipped"] is True
    assert admin.get("/api/auth/me").json()["onboarding"]["required"] is False


def test_reset_makes_wizard_required_again(app):
    admin = TestClient(app)
    _bootstrap(admin)
    admin.post("/api/onboarding/complete", json={"skipped": True})
    assert admin.get("/api/auth/me").json()["onboarding"]["required"] is False

    assert admin.delete("/api/onboarding").status_code == 200
    assert admin.get("/api/auth/me").json()["onboarding"]["required"] is True


def test_member_never_sees_onboarding_and_cannot_touch_it(app):
    admin = TestClient(app)
    _bootstrap(admin)
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw"})

    member = TestClient(app)
    assert (
        member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
        == 200
    )
    # Members are not onboarded (nothing to configure)…
    assert member.get("/api/auth/me").json()["onboarding"]["required"] is False
    # …and the endpoints are admin-only (central gate → admin_required).
    assert member.get("/api/onboarding").status_code == 403
    assert member.post("/api/onboarding/complete", json={}).status_code == 403
    assert member.delete("/api/onboarding").status_code == 403


def test_unauthenticated_cannot_touch_onboarding(app):
    admin = TestClient(app)
    _bootstrap(admin)

    anon = TestClient(app)
    assert anon.get("/api/onboarding").status_code == 401
    assert anon.post("/api/onboarding/complete", json={}).status_code == 401


def test_single_user_admin_is_onboarded_too(app):
    """The wizard gates every install's admin — the single-user implicit owner
    included (the wizard's password step is what *creates* the admin sign-in
    there). Completing it via the implicit owner clears it as usual."""
    client = TestClient(app)
    me = client.get("/api/auth/me").json()
    assert me["multi_user"] is False
    assert me["onboarding"]["required"] is True

    assert client.post("/api/onboarding/complete", json={"skipped": True}).status_code == 200
    assert client.get("/api/auth/me").json()["onboarding"]["required"] is False


def test_managed_box_defaults_to_multi_user_gate(app, monkeypatch):
    """A managed deployment with no explicit ``multi_user_enabled`` runs
    gated: an anonymous visitor mid-provisioning (no accounts yet) gets
    ``setup_required``, never the implicit owner's admin powers (a
    just-provisioned box must not drop a visitor onto an open dashboard).
    After the admin account exists, sign-in works and the wizard is
    required."""
    from types import SimpleNamespace

    from olano.deployment import resolver

    monkeypatch.setattr(
        resolver, "deployment_status", lambda now=None: SimpleNamespace(posture="enforced")
    )
    anon = TestClient(app)
    resp = anon.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "setup_required"

    # First-boot provisioning (or the Bootstrap screen) creates the admin…
    admin = TestClient(app)
    assert _bootstrap(admin).status_code == 200
    me = admin.get("/api/auth/me").json()
    assert me["multi_user"] is True
    assert me["onboarding"]["required"] is True


def test_explicit_single_user_choice_wins_on_a_managed_box(app, monkeypatch):
    """An explicit ``multi_user_enabled: false`` always wins over the managed
    default — the operator's word beats the posture, in both directions."""
    from types import SimpleNamespace

    from olano.config.home import update_home_config
    from olano.deployment import resolver

    monkeypatch.setattr(
        resolver, "deployment_status", lambda now=None: SimpleNamespace(posture="enforced")
    )
    update_home_config({"multi_user_enabled": False})
    client = TestClient(app)
    me = client.get("/api/auth/me").json()
    assert me["multi_user"] is False
    # …and the wizard still gates the implicit owner (it gates every admin).
    assert me["onboarding"]["required"] is True


def test_status_reports_setup_complete_for_the_console_gate(app):
    """``GET /api/status`` (public, the pre-login readiness probe) carries
    ``setup_complete``: False until the login gate is armed — multi-user on
    AND an admin account exists — and True after. The cloud console's
    deployment card gates its "Open dashboard" button on this, so a customer
    can never land on a half-provisioned box."""
    client = TestClient(app)
    assert client.get("/api/status").json()["setup_complete"] is False
    assert _bootstrap(client).status_code == 200
    assert client.get("/api/status").json()["setup_complete"] is True


def test_posture_resolution_failure_never_locks_out_a_manual_install(app, monkeypatch):
    """If the entitlements layer breaks outright, the multi-user default reads
    as the manual install (False): the implicit owner still reaches the
    dashboard (gated only by the wizard, which is skippable) instead of a
    login screen with no account behind it."""

    from olano.deployment import resolver

    def _boom(now=None):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(resolver, "deployment_status", _boom)
    client = TestClient(app)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["multi_user"] is False
    assert me.json()["onboarding"]["required"] is True


# ── where the wizard lands: the Agent Father greeting pointer ────────────────


def test_complete_points_at_the_greeting_thread_when_agent_father_is_up(app, monkeypatch):
    """The completion answer names the thread the runtime greets the owner on,
    and the greeting itself runs in the background — the POST never waits on
    a model call."""
    from olano.runtime import onboarding_greeting as og

    scheduled: list[tuple] = []

    async def fake_greet(runtime, user, thread_id, **kw):
        scheduled.append((user.email, thread_id))
        return None

    monkeypatch.setattr(og, "greet_new_owner", fake_greet)
    admin = TestClient(app)
    _bootstrap(admin)
    uid = admin.get("/api/auth/me").json()["user"]["id"]

    # Agent Father is booted on this box.
    runtime = _runtime_of(app)
    runtime._runners["agentfather"] = object()
    runtime._blocked_agents = {}
    done = admin.post("/api/onboarding/complete", json={"steps": {"agentfather": "done"}})
    assert done.status_code == 200
    assert done.json()["greeting"] == {"agent": "agentfather", "thread_id": f"welcome-{uid}"}


def test_complete_returns_no_greeting_when_the_plan_withholds_agent_father(app):
    admin = TestClient(app)
    _bootstrap(admin)
    runtime = _runtime_of(app)
    runtime._runners["agentfather"] = object()
    runtime._blocked_agents = {"agentfather": "blocked by deployment settings"}
    done = admin.post("/api/onboarding/complete", json={"skipped": True})
    assert done.status_code == 200
    assert done.json()["greeting"] is None
