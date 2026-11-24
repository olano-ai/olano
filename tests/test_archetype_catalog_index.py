"""Tests for the compiled archetype catalog (`olano.archetypes.catalog_index`).

Covers the three things the picker depends on: the catalog is correct, it is
cached hard enough that a request never rebuilds it, and the cache is keyed on
what is actually on disk so an added/edited/removed archetype shows up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.archetypes import catalog_index, installer


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """An external archetype pack laid out like the canonical `olano-agents`.

    Category subdirs plus `teams/`, a `.git` dir that must never be walked, and
    a `<home>/catalog/` to persist into.
    """
    home = tmp_path / "home"
    ext = home / "archetypes" / "olano-agents"
    (ext / "engineering").mkdir(parents=True)
    (ext / "teams").mkdir(parents=True)
    (ext / ".git" / "objects").mkdir(parents=True)

    (ext / "engineering" / "eng-backend.json5").write_text(
        json.dumps(
            {
                "name": "eng-backend",
                "label": "Backend Architect",
                "description": "Designs services.",
                "category": "engineering",
            }
        ),
        encoding="utf-8",
    )
    (ext / "teams" / "lionTest.json5").write_text(
        json.dumps(
            {
                "name": "lionTest",
                "label": "LionTest",
                "description": "A team.",
                "category": "engineering",
                "subagents": [{"from_archetype": "eng-backend"}],
            }
        ),
        encoding="utf-8",
    )
    # A file inside .git that would parse as an archetype if the walk descended.
    (ext / ".git" / "objects" / "sneaky.json5").write_text('{"name": "sneaky"}', encoding="utf-8")

    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    # Only the pack — not the bundled templates — so counts are exact.
    monkeypatch.setattr(installer, "_archetype_search_dirs", lambda: [ext])
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()
    yield ext
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()


def test_splits_teams_from_specialists(pack):
    cat = catalog_index.get_archetype_catalog()
    assert [e["name"] for e in cat["teams"]] == ["lionTest"]
    assert [e["name"] for e in cat["specialists"]] == ["eng-backend"]
    assert cat["teams"][0]["subagent_count"] == 1
    assert cat["teams"][0]["subagent_refs"] == ["eng-backend"]


def test_dot_dirs_are_never_walked(pack):
    """A `.git` inside the pack dir must contribute nothing.

    The canonical pack is a git clone living under `<home>/archetypes/`, so an
    unpruned recursive glob walks its object store on every read.
    """
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert "sneaky" not in names
    assert all(".git" not in str(p) for p in installer.discover_archetype_files())


def test_second_read_does_not_rebuild(pack, monkeypatch):
    catalog_index.get_archetype_catalog()

    def _explode():  # pragma: no cover - only runs if the cache is broken
        raise AssertionError("rebuilt when the cache should have served")

    monkeypatch.setattr(installer, "build_archetype_catalog", _explode)
    assert catalog_index.get_archetype_catalog()["teams"]


def test_survives_a_process_restart_via_disk(pack, monkeypatch):
    """A fresh process must read `<home>/catalog/archetypes.json`, not rebuild.

    This is what keeps a runtime that missed the boot warmer from paying a full
    rebuild on the first dashboard request after a restart.
    """
    catalog_index.get_archetype_catalog()
    catalog_index.invalidate()  # simulate a new process: memory empty, disk warm

    def _explode():  # pragma: no cover
        raise AssertionError("rebuilt instead of reading the persisted catalog")

    monkeypatch.setattr(installer, "build_archetype_catalog", _explode)
    cat = catalog_index.get_archetype_catalog()
    assert [e["name"] for e in cat["specialists"]] == ["eng-backend"]
    assert catalog_index.status()["source"] in {"memory", "disk"}


def test_added_archetype_invalidates_the_fingerprint(pack):
    assert len(catalog_index.get_archetype_catalog()["specialists"]) == 1
    (pack / "engineering" / "eng-frontend.json5").write_text(
        '{"name": "eng-frontend", "label": "Frontend"}', encoding="utf-8"
    )
    catalog_index.invalidate()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert names == {"eng-backend", "eng-frontend"}


def test_removed_archetype_invalidates_the_fingerprint(pack):
    catalog_index.get_archetype_catalog()
    (pack / "engineering" / "eng-backend.json5").unlink()
    catalog_index.invalidate()
    cat = catalog_index.get_archetype_catalog()
    assert [e["name"] for e in cat["specialists"]] == []


def test_malformed_archetype_is_skipped_not_fatal(pack):
    (pack / "engineering" / "broken.json5").write_text("{ not json at all ", encoding="utf-8")
    catalog_index.invalidate()
    cat = catalog_index.get_archetype_catalog()
    assert [e["name"] for e in cat["specialists"]] == ["eng-backend"]


def test_non_mapping_file_is_skipped(pack):
    """A stray YAML list in a pack dir must not become a catalog row."""
    (pack / "engineering" / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    catalog_index.invalidate()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert names == {"eng-backend"}


def test_archetype_with_unresolvable_subagent_refs_is_not_listed(pack):
    """A team the config layer would refuse to load must never reach the picker.

    `AgentConfig` raises on an unknown `from_archetype`, so listing such a team
    offers an install that succeeds and then fails to boot — which is exactly
    how three bundled team templates shipped broken on any box without the
    canonical pack.
    """
    (pack / "teams" / "lionGhost.json5").write_text(
        json.dumps(
            {
                "name": "lionGhost",
                "label": "LionGhost",
                "subagents": [{"from_archetype": "does-not-exist"}],
            }
        ),
        encoding="utf-8",
    )
    catalog_index.invalidate()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["teams"]}
    assert names == {"lionTest"}, "a team with a dangling ref was offered anyway"


def test_every_listed_team_can_actually_be_loaded(pack):
    """The listed→installable invariant, end to end against the real validator."""
    from olano.config.agent_config import AgentConfig

    for entry in catalog_index.get_archetype_catalog()["teams"]:
        path = installer.find_archetype_path(entry["name"])
        assert path is not None, f"{entry['name']} listed but unresolvable"
        data = dict(installer._load_archetype_file(path))
        data.setdefault("name", entry["name"])
        AgentConfig.model_validate(data)  # raises on a dangling from_archetype


def test_external_pack_overrides_bundled_templates(tmp_path, monkeypatch):
    """The canonical pack is the source of truth; bundled is only the floor.

    Bundled used to be searched first, so an in-package copy silently shadowed
    the pack's canonical version — a box with the pack installed still got the
    stale bundled archetype, with nothing reporting which one it had.
    """
    bundled = tmp_path / "bundled"
    ext = tmp_path / "home" / "archetypes"
    (bundled / "x").mkdir(parents=True)
    (ext / "teams").mkdir(parents=True)
    (bundled / "x" / "shared.json5").write_text(
        '{"name": "shared", "label": "from-bundled"}', encoding="utf-8"
    )
    (ext / "teams" / "shared.json5").write_text(
        '{"name": "shared", "label": "from-pack"}', encoding="utf-8"
    )

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(installer, "_BUNDLED_TEMPLATES_DIR", bundled)
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()

    dirs = installer._archetype_search_dirs()
    assert dirs[-1] == bundled, "bundled must be searched last"
    assert installer.find_archetype_path("shared") == ext / "teams" / "shared.json5"

    labels = {e["label"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert labels == {"from-pack"}
    catalog_index.invalidate()


def test_operator_configured_dirs_outrank_the_pack(tmp_path, monkeypatch):
    """An explicitly configured `archetype_dirs` entry beats everything.

    Naming a directory in config.yaml is a deliberate instruction, so a synced
    pack must not silently override it — the same "explicit wins" rule the rest
    of the config layer follows.
    """
    own = tmp_path / "mine"
    ext = tmp_path / "home" / "archetypes"
    own.mkdir(parents=True)
    ext.mkdir(parents=True)
    (own / "shared.json5").write_text('{"name": "shared", "label": "mine"}', encoding="utf-8")
    (ext / "shared.json5").write_text('{"name": "shared", "label": "pack"}', encoding="utf-8")

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "olano.config.home.load_home_config",
        lambda: type("C", (), {"archetype_dirs": [str(own)]})(),
    )
    installer._FILE_CACHE.clear()
    assert installer.find_archetype_path("shared") == own / "shared.json5"


def test_discovery_order_is_deterministic_and_prefers_shallow(tmp_path, monkeypatch):
    """Duplicate stems resolve the same way the installer resolves them.

    `find_archetype_path` prefers a direct child over a nested match, so the
    catalog must too — otherwise the picker can offer an entry whose install
    reads a different file.
    """
    root = tmp_path / "pack"
    (root / "nested").mkdir(parents=True)
    (root / "dup.json5").write_text('{"name": "dup", "label": "shallow"}', encoding="utf-8")
    (root / "nested" / "dup.json5").write_text('{"name": "dup", "label": "deep"}', encoding="utf-8")
    monkeypatch.setattr(installer, "_archetype_search_dirs", lambda: [root])
    installer._FILE_CACHE.clear()

    found = installer.discover_archetype_files()
    assert found == [root / "dup.json5"]
    assert installer.find_archetype_path("dup") == root / "dup.json5"


def test_status_reports_counts_and_pack_state(pack):
    catalog_index.get_archetype_catalog()
    st = catalog_index.status()
    assert st["teams"] == 1
    assert st["specialists"] == 1
    assert st["source"] == "memory"
    assert "pack_installed" in st
    assert st["search_dirs"] == [str(pack)]


def test_persisted_payload_is_readable_json(pack):
    catalog_index.get_archetype_catalog()
    from olano.catalog import store

    payload = store.read_payload(catalog_index.CATALOG_NAME)
    assert payload is not None
    assert payload["fingerprint"]
    assert isinstance(payload["teams"], list)
    assert isinstance(payload["specialists"], list)


def test_disk_payload_ignored_when_fingerprint_moves(pack):
    """A stale persisted catalog must lose to what is actually on disk."""
    catalog_index.get_archetype_catalog()
    from olano.catalog import store

    stale = store.read_payload(catalog_index.CATALOG_NAME)
    assert stale is not None
    stale["specialists"] = [{"name": "ghost", "label": "Ghost"}]
    stale["fingerprint"] = "0" * 64
    store.write_payload(catalog_index.CATALOG_NAME, stale, None)

    catalog_index.invalidate()
    names = {e["name"] for e in catalog_index.get_archetype_catalog()["specialists"]}
    assert names == {"eng-backend"}


def test_corrupt_disk_payload_falls_back_to_a_rebuild(pack):
    catalog_index.get_archetype_catalog()
    from olano.catalog import store

    (store.catalog_dir() / f"{catalog_index.CATALOG_NAME}.json").write_text(
        "{not json", encoding="utf-8"
    )
    catalog_index.invalidate()
    cat = catalog_index.get_archetype_catalog()
    assert [e["name"] for e in cat["specialists"]] == ["eng-backend"]


def test_bundled_templates_yield_a_usable_catalog(monkeypatch, tmp_path):
    """With no external pack, the in-package templates still populate both tabs.

    Guards the floor: a box that never fetched `olano-agents` must still show
    installable teams and experts rather than an empty picker.
    """
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr(
        installer, "_archetype_search_dirs", lambda: [Path(installer._BUNDLED_TEMPLATES_DIR)]
    )
    installer._FILE_CACHE.clear()
    catalog_index.invalidate()
    cat = catalog_index.get_archetype_catalog()
    assert len(cat["teams"]) >= 1
    assert len(cat["specialists"]) >= 1
    for entry in cat["teams"] + cat["specialists"]:
        assert entry["name"]
        assert entry["label"]
    catalog_index.invalidate()
