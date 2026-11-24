"""Unit tests for the agent-published page registry + viewer credential."""

from __future__ import annotations

import json

import pytest

from olano.config.home import get_olano_paths
from olano.pages import registry as pages_registry
from olano.pages.registry import (
    PagesError,
    admin_delete_page,
    clear_page_auth,
    delete_page,
    get_page,
    get_page_auth,
    list_page_auth,
    list_pages,
    normalize_slug,
    page_auth_configured,
    restart_page,
    set_page_auth,
    start_page,
    stop_page,
    verify_page_auth,
)


@pytest.fixture
def site_dir(tmp_path):
    ws = tmp_path / "ws"
    site = ws / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>hi</h1>")
    return ws


# ── slug validation ──────────────────────────────────────────────────────────


def test_normalize_slug_lowers_and_trims():
    assert normalize_slug("  My-Report ") == "my-report"


@pytest.mark.parametrize(
    "bad",
    ["", "a", "-abc", "abc-", "a_b", "a b", "über", "x" * 64, "api", "pages", "admin"],
)
def test_invalid_or_reserved_slugs_rejected(bad):
    with pytest.raises(PagesError):
        normalize_slug(bad)


# ── lifecycle + collisions ───────────────────────────────────────────────────


def test_start_claims_slug_and_lists(site_dir):
    record = start_page("alice", "report", "site", site_dir)
    assert record["slug"] == "report"
    assert record["enabled"] is True
    assert record["directory"] == "site"
    assert get_page("report")["agent"] == "alice"
    assert [p["slug"] for p in list_pages("alice")] == ["report"]
    assert list_pages("bob") == []


def test_slug_collision_across_agents_rejected(site_dir):
    start_page("alice", "report", "site", site_dir)
    with pytest.raises(PagesError, match="already taken by agent 'alice'"):
        start_page("bob", "report", "site", site_dir)
    # Alice's record must be untouched by the failed claim.
    assert get_page("report")["agent"] == "alice"


def test_restart_own_slug_updates_directory(site_dir):
    start_page("alice", "report", "site", site_dir)
    other = site_dir / "v2"
    other.mkdir()
    record = start_page("alice", "report", "v2", site_dir)
    assert record["directory"] == "v2"


def test_stop_keeps_claim_restart_resumes(site_dir):
    start_page("alice", "report", "site", site_dir)
    stopped = stop_page("alice", "report")
    assert stopped["enabled"] is False
    with pytest.raises(PagesError, match="already taken"):
        start_page("bob", "report", "site", site_dir)
    resumed = restart_page("alice", "report", site_dir)
    assert resumed["enabled"] is True


def test_stop_foreign_slug_rejected(site_dir):
    start_page("alice", "report", "site", site_dir)
    with pytest.raises(PagesError, match="belongs to agent 'alice'"):
        stop_page("bob", "report")


def test_delete_releases_slug(site_dir):
    start_page("alice", "report", "site", site_dir)
    delete_page("alice", "report")
    assert get_page("report") is None
    # Now claimable by another agent.
    assert start_page("bob", "report", "site", site_dir)["agent"] == "bob"


def test_admin_delete_releases_any_slug(site_dir):
    start_page("alice", "report", "site", site_dir)
    admin_delete_page("report")
    assert get_page("report") is None
    with pytest.raises(PagesError):
        admin_delete_page("report")


def test_missing_directory_rejected(site_dir):
    with pytest.raises(PagesError, match="does not exist"):
        start_page("alice", "report", "nope", site_dir)


@pytest.mark.parametrize("bad_dir", ["../outside", "/etc", "a/../../b"])
def test_traversal_directory_rejected(site_dir, bad_dir):
    with pytest.raises(PagesError):
        start_page("alice", "report", bad_dir, site_dir)


def test_corrupt_registry_file_recovers(site_dir):
    path = get_olano_paths().pages_registry_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert list_pages() == []
    start_page("alice", "report", "site", site_dir)
    assert json.loads(path.read_text())["pages"]["report"]["agent"] == "alice"


# ── viewer credential (per agent) ────────────────────────────────────────────


def test_auth_roundtrip_and_hash_only_storage():
    assert not page_auth_configured("alice")
    set_page_auth("alice", "owner", "s3cret-only-alice")
    assert page_auth_configured("alice")
    assert verify_page_auth("alice", "owner", "s3cret-only-alice")
    assert not verify_page_auth("alice", "owner", "wrong")
    assert not verify_page_auth("alice", "someone", "s3cret-only-alice")
    stored = get_page_auth("alice")
    assert stored["password_hash"].startswith("pbkdf2_sha256$")
    assert stored["username"] == "owner"
    raw = get_olano_paths().pages_auth_path.read_text()
    assert "s3cret-only-alice" not in raw


def test_auth_is_isolated_per_agent():
    set_page_auth("alice", "owner", "alice-password")
    set_page_auth("bob", "owner", "bob-password")
    # Each agent's credential verifies only its own password.
    assert verify_page_auth("alice", "owner", "alice-password")
    assert verify_page_auth("bob", "owner", "bob-password")
    assert not verify_page_auth("alice", "owner", "bob-password")
    assert not verify_page_auth("bob", "owner", "alice-password")
    # An agent with no credential set is never authenticated.
    assert not page_auth_configured("carol")
    assert not verify_page_auth("carol", "owner", "alice-password")
    assert set(list_page_auth()) == {"alice", "bob"}


def test_clear_page_auth():
    set_page_auth("alice", "owner", "alice-password")
    assert page_auth_configured("alice")
    assert clear_page_auth("alice") is True
    assert not page_auth_configured("alice")
    # Clearing an agent that has no credential is a no-op returning False.
    assert clear_page_auth("alice") is False


def test_auth_validation():
    with pytest.raises(PagesError, match="agent"):
        set_page_auth("", "owner", "long-enough-pw")
    with pytest.raises(PagesError, match="username"):
        set_page_auth("alice", "", "long-enough-pw")
    with pytest.raises(PagesError, match="':'"):
        set_page_auth("alice", "a:b", "long-enough-pw")
    with pytest.raises(PagesError, match="at least"):
        set_page_auth("alice", "owner", "short")


def test_verify_without_configured_auth_is_false():
    assert pages_registry.verify_page_auth("anyone", "owner", "anything") is False


def test_legacy_v1_credential_migrates_to_page_owning_agents(site_dir):
    """A flat v1 auth.json is assigned to every agent that currently owns a page."""
    # Two agents own pages; a third does not.
    start_page("alice", "report", "site", site_dir)
    start_page("bob", "dash", "site", site_dir)
    # Write a legacy v1 global credential (pre-per-agent schema).
    from olano.users.passwords import hash_password

    path = get_olano_paths().pages_auth_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"username": "owner", "password_hash": hash_password("legacy-global-pw")})
    )
    # First read migrates: both page-owning agents inherit the credential.
    assert verify_page_auth("alice", "owner", "legacy-global-pw")
    assert verify_page_auth("bob", "owner", "legacy-global-pw")
    assert not page_auth_configured("carol")  # no page → not migrated
    # The file is rewritten in v2 form and no longer flat.
    migrated = json.loads(path.read_text())
    assert migrated["version"] == 2
    assert set(migrated["agents"]) == {"alice", "bob"}
    assert "password_hash" not in migrated  # not a flat v1 record anymore
