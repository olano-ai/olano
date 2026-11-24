"""The demo deployment's passwordless sign-in (``olano/runtime/demo_access.py``).

The mobile app's "Try the demo" entry signs a visitor in to the Olano-run demo
box with ``POST /api/auth/demo``. The route must be inert on every other box,
must never hand out an administrator's session, and must be bounded.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime import auth as auth_layer
from olano.runtime import demo_access
from olano.runtime.api import create_api
from olano.users.store import get_user_store, reset_user_store

DEMO_EMAIL = "demo@olano.run"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in (*demo_access.DEMO_ACCOUNT_ENV, *demo_access.DEMO_SHARED_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_SECURE_COOKIES", raising=False)
    demo_access.reset_for_tests()
    reset_user_store()
    yield
    demo_access.reset_for_tests()


def _client(*, multi_user: bool = True) -> TestClient:
    store = get_user_store()
    store.create_user(email="owner@x.com", password="pw", name="Owner", is_admin=True)
    auth_layer.set_multi_user_enabled(multi_user)
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return TestClient(create_api(runtime))


def test_off_unless_the_environment_names_a_demo_account():
    client = _client()
    response = client.post("/api/auth/demo")
    assert response.status_code == 404
    assert response.json()["detail"] == "demo_disabled"
    assert "set-cookie" not in response.headers


def _template():
    store = get_user_store()
    template = store.create_user(email=DEMO_EMAIL, password="unused-by-the-app", name="Demo")
    store.db.grant_access(template.id, "nova")
    store.db.grant_access(template.id, "aria", role="owner")
    return template


def test_each_visitor_gets_an_account_of_their_own(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    _template()

    response = client.post("/api/auth/demo")
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["email"].endswith("@demo.invalid")
    assert user["name"] == demo_access.VISITOR_NAME
    assert user["is_admin"] is False
    cookie = response.headers["set-cookie"]
    assert "lc_session=" in cookie
    assert f"Max-Age={demo_access.DEMO_SESSION_DAYS * 86400}" in cookie

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == user["email"]

    # The template's agents, never more than member rights.
    store = get_user_store()
    visitor = store.db.get_user_by_email(user["email"])
    assert store.db.list_agents_for_user(visitor.id) == [
        {"agent_name": "aria", "role": "member"},
        {"agent_name": "nova", "role": "member"},
    ]

    # A second visitor is someone else: conversations never mix.
    other = TestClient(client.app).post("/api/auth/demo").json()["user"]
    assert other["email"] != user["email"]


def test_shared_mode_signs_everyone_in_as_the_template(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    monkeypatch.setenv("OLANO_DEMO_SHARED", "1")
    client = _client()
    _template()
    response = client.post("/api/auth/demo")
    assert response.status_code == 200
    assert response.json()["user"]["email"] == DEMO_EMAIL
    assert not any(demo_access.is_visitor_email(u.email) for u in get_user_store().db.list_users())


def test_visitor_accounts_are_capped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    monkeypatch.setattr(demo_access, "MAX_VISITOR_ACCOUNTS", 1)
    client = _client()
    _template()
    assert client.post("/api/auth/demo").status_code == 200
    busy = client.post("/api/auth/demo")
    assert busy.status_code == 429
    assert busy.json()["detail"] == "demo_busy"


def test_the_legacy_variable_spelling_works(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    get_user_store().create_user(email=DEMO_EMAIL, password="x", name="Demo")
    assert client.post("/api/auth/demo").status_code == 200


def test_never_hands_out_an_administrator(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", "owner@x.com")
    client = _client()
    response = client.post("/api/auth/demo")
    assert response.status_code == 403
    assert response.json()["detail"] == "demo_account_is_admin"
    assert "set-cookie" not in response.headers
    assert client.get("/api/auth/me").status_code == 401


def test_a_missing_or_disabled_account_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    assert client.post("/api/auth/demo").json()["detail"] == "demo_unavailable"

    user = get_user_store().create_user(email=DEMO_EMAIL, password="x", name="Demo")
    get_user_store().db.update_user(user.id, is_active=False)
    response = client.post("/api/auth/demo")
    assert response.status_code == 404
    assert response.json()["detail"] == "demo_unavailable"


def test_a_single_user_box_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client(multi_user=False)
    get_user_store().create_user(email=DEMO_EMAIL, password="x", name="Demo")
    response = client.post("/api/auth/demo")
    assert response.status_code == 404
    assert response.json()["detail"] == "demo_unavailable"


def test_sign_ins_are_capped_per_minute(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    monkeypatch.setattr(demo_access, "_limiter", demo_access._MinuteLimiter(2))
    client = _client()
    get_user_store().create_user(email=DEMO_EMAIL, password="x", name="Demo")
    assert client.post("/api/auth/demo").status_code == 200
    assert client.post("/api/auth/demo").status_code == 200
    busy = client.post("/api/auth/demo")
    assert busy.status_code == 429
    assert busy.json()["detail"] == "demo_busy"
    assert busy.headers["retry-after"] == "60"


def test_the_limiter_forgets_after_a_minute():
    limiter = demo_access._MinuteLimiter(1)
    assert limiter.allow(now=100.0)
    assert not limiter.allow(now=159.9)
    assert limiter.allow(now=160.0)


def test_the_route_is_public():
    assert demo_access.DEMO_PATH in auth_layer.PUBLIC_API_PATHS


def _write_transcript(agent: str, thread_id: str, text: str) -> None:
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-11-13T08:00:00+00:00", "role": "user", "content": text},
        {"ts": "2026-11-13T08:00:01+00:00", "role": "assistant", "content": f"About {text}."},
    ]
    (sessions / f"{thread_id}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _visitor(client: TestClient) -> tuple[TestClient, str]:
    """A signed-in visitor on its own cookie jar, and its account id."""
    own = TestClient(client.app)
    email = own.post("/api/auth/demo").json()["user"]["email"]
    return own, get_user_store().db.get_user_by_email(email).id


def test_a_visitor_sees_only_the_conversations_it_started(monkeypatch: pytest.MonkeyPatch):
    """Strangers share the demo's agents, so a visitor's threads are its own:
    the list, a page, search, a direct read and the run endpoints all agree."""
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    _template()
    alice, alice_id = _visitor(client)
    bob, bob_id = _visitor(client)
    mine = f"u:{alice_id}:11111111-1111-4111-8111-111111111111"
    theirs = f"u:{bob_id}:22222222-2222-4222-8222-222222222222"
    _write_transcript("nova", mine, "tulips")
    _write_transcript("nova", theirs, "secret plans")
    _write_transcript("nova", "team-thread", "the team's own chat")

    listed = alice.get("/api/agents/nova/threads")
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()] == [mine]
    page = alice.get("/api/agents/nova/threads?limit=20").json()["threads"]
    assert [t["id"] for t in page] == [mine]
    found = alice.get("/api/agents/nova/threads/search?q=plans").json()["threads"]
    assert found == []
    assert [
        t["id"] for t in alice.get("/api/agents/nova/threads/search?q=tulips").json()["threads"]
    ] == [mine]

    assert alice.get(f"/api/agents/nova/threads/{mine}").status_code == 200
    for other in (theirs, "team-thread"):
        assert alice.get(f"/api/agents/nova/threads/{other}").status_code == 404
        renamed = alice.patch(f"/api/agents/nova/threads/{other}", json={"name": "mine now"})
        assert renamed.status_code == 404
    assert [t["id"] for t in bob.get("/api/agents/nova/threads").json()] == [theirs]

    # An administrator still sees everything.
    admin = TestClient(client.app)
    assert (
        admin.post("/api/auth/login", json={"email": "owner@x.com", "password": "pw"}).status_code
        == 200
    )
    assert {t["id"] for t in admin.get("/api/agents/nova/threads").json()} == {
        mine,
        theirs,
        "team-thread",
    }


def test_a_visitors_new_conversation_is_minted_under_its_prefix(monkeypatch: pytest.MonkeyPatch):
    from olano.security.request_context import push_request_context

    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    _template()
    _, visitor_id = _visitor(client)
    visitor = get_user_store().db.get_user_by_id(visitor_id)
    assert visitor is not None
    with push_request_context(user_id=visitor.id, authenticated=True, is_owner=False):
        minted = auth_layer.namespace_thread_id(None)
        assert minted.startswith(f"u:{visitor_id}:")
        assert auth_layer.namespace_thread_id(minted) == minted
        with pytest.raises(Exception) as refused:
            auth_layer.namespace_thread_id("someone-elses-thread")
        assert getattr(refused.value, "status_code", None) == 404


def test_shared_mode_and_ordinary_members_are_not_scoped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    _client()
    template = _template()
    member = get_user_store().create_user(email="teammate@x.com", password="pw", name="T")
    assert demo_access.scopes_threads(template) is False
    assert demo_access.scopes_threads(member) is False
    visitor = MagicMock(email="visitor-abc@demo.invalid", is_admin=False)
    assert demo_access.scopes_threads(visitor) is True
    monkeypatch.delenv("OLANO_DEMO_ACCOUNT")
    # Off the demo box an address that looks like a visitor's means nothing.
    assert demo_access.scopes_threads(visitor) is False


async def test_the_sweep_deletes_day_old_visitors_and_their_conversations(
    monkeypatch: pytest.MonkeyPatch,
):
    from datetime import datetime, timedelta, timezone

    from olano.config.home import get_olano_paths
    from olano.core import checkpoints

    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", DEMO_EMAIL)
    client = _client()
    _template()
    _, old_id = _visitor(client)
    _, new_id = _visitor(client)
    store = get_user_store()
    now = datetime.now(tz=timezone.utc)
    with store.db._conn() as conn:
        conn.execute(
            "UPDATE users SET created_at = ? WHERE id = ?",
            ((now - timedelta(hours=25)).isoformat(), old_id),
        )
        conn.commit()
    old_thread = f"u:{old_id}:33333333-3333-4333-8333-333333333333"
    new_thread = f"u:{new_id}:44444444-4444-4444-8444-444444444444"
    _write_transcript("nova", old_thread, "yesterday")
    _write_transcript("aria", old_thread, "yesterday, elsewhere")
    sessions = get_olano_paths().sessions_dir
    (sessions / "nova" / f"{old_thread}.feedback.ndjson").write_text("{}\n")
    _write_transcript("nova", new_thread, "today")
    _write_transcript("nova", "team-thread", "the team's own chat")
    purged: list[tuple[str, str]] = []

    async def _purge(agent: str, thread_id: str, home=None) -> None:
        purged.append((agent, thread_id))

    monkeypatch.setattr(checkpoints, "adelete_thread", _purge)

    result = await demo_access.asweep_visitors(now=now)

    assert result == {"accounts": 1, "threads": 2}
    assert sorted(purged) == [("aria", old_thread), ("nova", old_thread)]
    assert store.db.get_user_by_id(old_id) is None
    assert store.db.get_user_by_id(new_id) is not None
    assert store.db.get_user_by_email(DEMO_EMAIL) is not None
    left = sorted(p.name for p in sessions.rglob("*") if p.is_file())
    assert left == sorted([f"{new_thread}.jsonl", "team-thread.jsonl"])


async def test_the_sweep_does_nothing_off_a_demo_box():
    assert await demo_access.asweep_visitors() is None
