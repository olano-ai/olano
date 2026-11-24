"""Tests for the cloud agent-archetype catalog (`olano.catalog.agents_registry`).

This catalog is unusual among the synced ones: it **materializes to disk** as
`<home>/catalog/archetypes/<slug>.json5` rather than being served from memory,
so that every existing archetype consumer (find_archetype_path, the catalog
builder, install, `from_archetype` resolution) works unchanged. These tests pin
the properties that makes safe — the filenames it writes, what it deletes, what
it refuses — plus the search-dir precedence that decides which copy of an
archetype a box actually runs.
"""

from __future__ import annotations

import json

import pytest

from olano.archetypes import catalog_index, installer
from olano.catalog import agents_registry


def _entry(slug: str, **body):
    return {"slug": slug, "body": {"name": slug, "label": slug.title(), **body}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()
    yield tmp_path
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()


def _stub_fetch(monkeypatch, agents, revision=7):
    monkeypatch.setattr(agents_registry, "_fetch_all", lambda: (agents, revision))


# ── Materializing ───────────────────────────────────────────────────────────


def test_writes_one_file_per_archetype_named_by_slug(home, monkeypatch):
    """Filename is the SLUG, not the `name` field.

    `from_archetype:` resolves by filename stem, and the two genuinely differ
    (`knowledge-base-agent.json5` declares `"name": "KnowledgeBaseAgent"`), so
    naming files after `name` would break every team's specialist references.
    """
    _stub_fetch(monkeypatch, [_entry("knowledge-base-agent", name="KnowledgeBaseAgent")])
    assert agents_registry.refresh_from_cloud() == {"count": 1, "revision": 7}

    written = sorted(p.name for p in agents_registry.materialized_dir().glob("*.json5"))
    assert written == ["knowledge-base-agent.json5"]
    body = json.loads(
        (agents_registry.materialized_dir() / "knowledge-base-agent.json5").read_text()
    )
    assert body["name"] == "KnowledgeBaseAgent"


def test_withdrawn_archetypes_are_removed(home, monkeypatch):
    _stub_fetch(monkeypatch, [_entry("keeper"), _entry("goner")])
    agents_registry.refresh_from_cloud()
    assert len(list(agents_registry.materialized_dir().glob("*.json5"))) == 2

    _stub_fetch(monkeypatch, [_entry("keeper")])
    agents_registry.refresh_from_cloud()
    assert [p.stem for p in agents_registry.materialized_dir().glob("*.json5")] == ["keeper"]


def test_empty_payload_is_refused(home, monkeypatch):
    """An empty catalog is far likelier to be a cloud-side accident than a
    deliberate withdrawal of everything — it must not wipe the box."""
    _stub_fetch(monkeypatch, [_entry("keeper")])
    agents_registry.refresh_from_cloud()

    _stub_fetch(monkeypatch, [])
    assert agents_registry.refresh_from_cloud() is None
    assert [p.stem for p in agents_registry.materialized_dir().glob("*.json5")] == ["keeper"]


def test_fetch_failure_leaves_the_existing_copy_alone(home, monkeypatch):
    _stub_fetch(monkeypatch, [_entry("keeper")])
    agents_registry.refresh_from_cloud()

    monkeypatch.setattr(agents_registry, "_fetch_all", lambda: None)
    assert agents_registry.refresh_from_cloud() is None
    assert [p.stem for p in agents_registry.materialized_dir().glob("*.json5")] == ["keeper"]


@pytest.mark.parametrize("slug", ["../escape", "nested/path", ".hidden", ""])
def test_slugs_that_could_escape_the_directory_are_skipped(home, monkeypatch, slug):
    """The slug becomes a filename, so a traversal in it would write anywhere."""
    _stub_fetch(monkeypatch, [{"slug": slug, "body": {"name": "x"}}, _entry("ok")])
    agents_registry.refresh_from_cloud()
    assert [p.stem for p in agents_registry.materialized_dir().glob("*.json5")] == ["ok"]
    assert not (home / "catalog" / "escape.json5").exists()


def test_entry_without_a_body_is_skipped(home, monkeypatch):
    _stub_fetch(monkeypatch, [{"slug": "bodyless"}, _entry("ok")])
    agents_registry.refresh_from_cloud()
    assert [p.stem for p in agents_registry.materialized_dir().glob("*.json5")] == ["ok"]


# ── Integration with the archetype layer ────────────────────────────────────


def test_materialized_archetypes_reach_the_picker_and_install(home, monkeypatch):
    """The point of writing files: every downstream consumer works unchanged."""
    _stub_fetch(
        monkeypatch,
        [
            _entry("eng-backend", description="Designs services."),
            {
                "slug": "lionTeam",
                "body": {
                    "name": "lionTeam",
                    "label": "LionTeam",
                    "subagents": [{"from_archetype": "eng-backend"}],
                },
            },
        ],
    )
    agents_registry.refresh_from_cloud()

    # Membership, not equality: the in-package templates are legitimately in the
    # catalog too — the cloud copy adds to them rather than replacing them.
    cat = catalog_index.get_archetype_catalog()
    assert "lionTeam" in {e["name"] for e in cat["teams"]}
    assert "eng-backend" in {e["name"] for e in cat["specialists"]}
    # And the team's specialist reference resolves, so the config validates —
    # a team whose refs dangle would have been skipped by the catalog builder.
    assert installer.find_archetype_path("eng-backend") is not None


def test_refresh_invalidates_the_compiled_catalog(home, monkeypatch):
    _stub_fetch(monkeypatch, [_entry("first")])
    agents_registry.refresh_from_cloud()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert "first" in names and "second" not in names

    # Without the invalidate() inside refresh_from_cloud, the 30s fingerprint
    # throttle would keep serving the previous compile.
    _stub_fetch(monkeypatch, [_entry("first"), _entry("second")])
    agents_registry.refresh_from_cloud()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert {"first", "second"} <= names


def test_cloud_copy_outranks_a_git_clone_but_not_operator_dirs(home, monkeypatch):
    """Precedence: archetype_dirs > cloud > git clone > bundled.

    The synced copy beats the clone because a one-shot `archetypes update` goes
    stale and the sync cannot; an explicitly configured dir beats both because
    naming it is a deliberate instruction.
    """
    own = home / "mine"
    own.mkdir()
    (own / "shared.json5").write_text('{"name": "shared", "label": "mine"}', encoding="utf-8")
    clone = home / "archetypes"
    clone.mkdir()
    (clone / "shared.json5").write_text('{"name": "shared", "label": "clone"}', encoding="utf-8")
    _stub_fetch(monkeypatch, [_entry("shared", label="cloud")])
    agents_registry.refresh_from_cloud()

    # Cloud beats the clone…
    installer._FILE_CACHE.clear()
    assert installer.find_archetype_path("shared") == (
        agents_registry.materialized_dir() / "shared.json5"
    )

    # …and an operator-configured dir beats the cloud.
    monkeypatch.setattr(
        "olano.config.home.load_home_config",
        lambda: type("C", (), {"archetype_dirs": [str(own)]})(),
    )
    installer._FILE_CACHE.clear()
    assert installer.find_archetype_path("shared") == own / "shared.json5"


def test_status_reports_the_materialized_count(home, monkeypatch):
    assert agents_registry.catalog_status()["source"] == "none"
    _stub_fetch(monkeypatch, [_entry("a"), _entry("b")], revision=12)
    agents_registry.refresh_from_cloud()
    st = agents_registry.catalog_status()
    assert st["source"] == "cloud"
    assert st["archetypes"] == 2
    assert st["revision"] == 12


def test_sync_service_knows_about_the_catalog():
    """A handler the manifest advertises but no handler consumes is a no-op."""
    from olano.catalog.sync import _HANDLERS

    assert "agents" in _HANDLERS
