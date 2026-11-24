"""Integration tests for the /pages/<slug>/ viewer surface + /api/pages management."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.pages.registry import get_page, set_page_auth, start_page
from olano.runtime import pages_routes
from olano.runtime.api import create_api

PASSWORD = "s3cret-only-alice"
USERNAME = "owner"


def _basic(username: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    pages_routes._AUTH_CACHE.clear()
    yield
    pages_routes._AUTH_CACHE.clear()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "alice-ws"
    site = ws / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>report</h1>")
    (site / "style.css").write_text("body { color: red }")
    (ws / "SECRET.md").write_text("workspace file outside the page dir")
    # Tests have no real agent.json5 on disk — resolve every agent to this ws.
    monkeypatch.setattr(pages_routes, "_agent_workspace_root", lambda agent: ws)
    return ws


@pytest.fixture
def client(workspace):
    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


@pytest.fixture
def published(workspace):
    # Page owned by agent "alice".
    return start_page("alice", "report", "site", workspace)


def test_unknown_slug_404(client):
    assert client.get("/pages/nope/").status_code == 404


def test_locked_until_owner_sets_password(client, published):
    r = client.get("/pages/report/")
    assert r.status_code == 503
    assert "set-password" in r.text
    assert "alice" in r.text  # names the owning agent


def test_401_challenge_without_and_with_wrong_credentials(client, published):
    set_page_auth("alice", USERNAME, PASSWORD)
    r = client.get("/pages/report/")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/pages/report/", headers=_basic(USERNAME, "wrong")).status_code == 401
    assert client.get("/pages/report/", headers=_basic("intruder", PASSWORD)).status_code == 401


def test_serves_index_and_assets_with_correct_credentials(client, published):
    set_page_auth("alice", USERNAME, PASSWORD)
    headers = _basic(USERNAME, PASSWORD)

    index = client.get("/pages/report/", headers=headers)
    assert index.status_code == 200
    assert index.text == "<h1>report</h1>"
    assert index.headers["content-type"].startswith("text/html")

    css = client.get("/pages/report/style.css", headers=headers)
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    assert client.get("/pages/report/missing.js", headers=headers).status_code == 404


def test_credentials_are_per_agent(client, workspace):
    """Agent alice's password guards only alice's pages; bob's stay locked."""
    start_page("alice", "report", "site", workspace)
    start_page("bob", "dash", "site", workspace)
    set_page_auth("alice", USERNAME, PASSWORD)

    # Alice's credential opens alice's page...
    assert client.get("/pages/report/", headers=_basic(USERNAME, PASSWORD)).status_code == 200
    # ...but bob has no credential, so bob's page is still 503 (not unlocked by
    # alice's password).
    assert client.get("/pages/dash/").status_code == 503
    assert client.get("/pages/dash/", headers=_basic(USERNAME, PASSWORD)).status_code == 503

    # Give bob his own, different password: alice's must not open it.
    set_page_auth("bob", USERNAME, "bob-only-password")
    assert client.get("/pages/dash/", headers=_basic(USERNAME, PASSWORD)).status_code == 401
    assert (
        client.get("/pages/dash/", headers=_basic(USERNAME, "bob-only-password")).status_code == 200
    )


def test_bare_slug_redirects_to_trailing_slash(client, published):
    r = client.get("/pages/report", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/pages/report/"


def test_traversal_out_of_page_dir_blocked(client, published):
    set_page_auth("alice", USERNAME, PASSWORD)
    headers = _basic(USERNAME, PASSWORD)
    r = client.get("/pages/report/%2e%2e/SECRET.md", headers=headers)
    assert r.status_code in (403, 404)
    assert "workspace file" not in r.text


def test_stopped_page_is_404(client, workspace, published):
    from olano.pages.registry import stop_page

    set_page_auth("alice", USERNAME, PASSWORD)
    stop_page("alice", "report")
    r = client.get("/pages/report/", headers=_basic(USERNAME, PASSWORD))
    assert r.status_code == 404


def test_bearer_gate_skips_pages_but_guards_management(client, published, monkeypatch):
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "tok")
    set_page_auth("alice", USERNAME, PASSWORD)
    # Viewer surface: reachable with Basic Auth alone (no bearer/session).
    r = client.get("/pages/report/", headers=_basic(USERNAME, PASSWORD))
    assert r.status_code == 200
    # Management surface: still behind the dashboard bearer gate.
    assert client.get("/api/pages").status_code == 401
    assert client.get("/api/pages/alice/auth").status_code == 401


def test_management_endpoints(client, published):
    listing = client.get("/api/pages").json()
    assert listing["auth_by_agent"] == {}
    assert [p["slug"] for p in listing["pages"]] == ["report"]
    assert client.get("/api/pages/alice/auth").json() == {
        "agent": "alice",
        "configured": False,
        "username": "owner",
    }

    r = client.post("/api/pages/alice/auth", json={"username": USERNAME, "password": PASSWORD})
    assert r.status_code == 200
    assert client.get("/api/pages").json()["auth_by_agent"] == {
        "alice": {"configured": True, "username": USERNAME}
    }
    assert client.get("/api/pages/alice/auth").json()["configured"] is True
    # Credential set through the API immediately guards the page.
    assert client.get("/pages/report/", headers=_basic(USERNAME, PASSWORD)).status_code == 200

    # Invalid username is rejected.
    assert (
        client.post(
            "/api/pages/alice/auth", json={"username": "a:b", "password": PASSWORD}
        ).status_code
        == 400
    )

    # Removing the credential re-locks the page (503).
    assert client.delete("/api/pages/alice/auth").json()["removed"] is True
    assert client.get("/pages/report/", headers=_basic(USERNAME, PASSWORD)).status_code == 503

    # Slug deletion is a distinct endpoint (two-segment /{agent}/auth vs one).
    assert client.delete("/api/pages/report").status_code == 200
    assert get_page("report") is None
    assert client.delete("/api/pages/report").status_code == 404
