"""The brain an archetype installs with, on a managed box and everywhere else.

A managed deployment bills inference in credits through the Olano gateway and
holds no provider key, so an archetype that pins ``anthropic:claude-sonnet-4-6``
installs an agent that fails on its first turn. Every archetype — AgentFather,
the conductor, included — gets the fast tier (the conductor used to get the
deep one; migration 0020 moves the ones already on it). Unmanaged installs keep
the archetype file's own literal, because there is no gateway to bill through
and an ``olano:*`` tier there can only 401.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.archetypes import installer


@pytest.fixture
def managed(monkeypatch):
    """Force the managed posture the resolver keys on."""
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: True)


@pytest.fixture
def unmanaged(monkeypatch):
    from olano.config import system_settings as ss

    monkeypatch.setattr(ss, "managed_olano_default", lambda: False)


# ── the resolver ─────────────────────────────────────────────────────────────


def test_conductor_gets_the_fast_tier_too(managed) -> None:
    """AgentFather's turns are conversational and tool-driven; the deep tier
    only made every one of them wait, and that wait is the first thing a new
    owner meets. The role is still resolved separately so the split can come
    back with one line."""
    assert installer.MANAGED_CONDUCTOR_MODEL == "default:turbo"
    assert installer.default_archetype_model("agentfather") == "default:turbo"
    # An archetype name is not case- or whitespace-sensitive here.
    assert installer.default_archetype_model("  AgentFather ") == "default:turbo"


def test_every_other_archetype_gets_the_fast_tier(managed) -> None:
    for name in ("merli", "it-admin", "customer-service", "lionLife", "developer", ""):
        assert installer.default_archetype_model(name) == "default:turbo"
    assert installer.default_archetype_model(None) == "default:turbo"


def test_unmanaged_keeps_the_archetype_file_model(unmanaged) -> None:
    """None ⇒ "whatever the file declares". An olano tier on a box with no
    gateway is a model that can only 401 on the first turn."""
    assert installer.default_archetype_model("agentfather") is None
    assert installer.default_archetype_model("merli") is None


# ── install paths ────────────────────────────────────────────────────────────


def _installed(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_managed_install_puts_agentfather_on_turbo(managed, tmp_path) -> None:
    path = installer.install_archetype("agentfather", agents_dir=tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_managed_install_puts_other_archetypes_on_turbo(managed, tmp_path) -> None:
    path = installer.install_archetype("it-admin", agents_dir=tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_unmanaged_install_keeps_the_shipped_model(unmanaged, tmp_path) -> None:
    shipped = installer.load_archetype_template("agentfather")["model"]
    assert not shipped.startswith("olano:")
    path = installer.install_archetype("agentfather", agents_dir=tmp_path)
    assert _installed(path)["model"] == shipped


def test_installing_does_not_rewrite_the_archetype_for_the_next_caller(managed, tmp_path) -> None:
    """`install_archetype` edits the template it is handed. The parse cache must
    hand out a copy, or one install's name/model leaks into every later read of
    the same archetype for the life of the process."""
    before = installer.load_archetype_template("merli")
    installer.install_archetype(
        "merli", agent_name="bob", agents_dir=tmp_path, model="openai:gpt-5.5"
    )
    assert installer.load_archetype_template("merli") == before


def test_an_explicit_model_always_wins(managed, tmp_path) -> None:
    """The resolver is a default, not a policy — a caller who names a model
    (the CLI's `--model`, a preferred_model in the home config) gets it."""
    path = installer.install_archetype(
        "agentfather", agents_dir=tmp_path, model="anthropic:claude-sonnet-4-6"
    )
    assert _installed(path)["model"] == "anthropic:claude-sonnet-4-6"


def test_agent_creation_templates_follow_the_same_rule(managed, tmp_path) -> None:
    """`agents create --from-template` templates conduct nothing, so they all
    land on the fast tier."""
    path = installer.install_agent_template("developer", agents_dir=tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_installed_model_survives_agent_config_validation(managed, tmp_path) -> None:
    """`default:turbo` must parse as a brain — the AgentConfig validator rejects
    the Olano *utility* groups, and a default that trips it would break every
    boot."""
    from olano.config.agent_config import AgentConfig

    path = installer.install_archetype("agentfather", agents_dir=tmp_path)
    assert AgentConfig(**_installed(path)).model == "default:turbo"


# ── migration 0011: boxes provisioned before the resolver existed ─────────────


def _write_agent(agents_dir: Path, name: str, data: dict) -> Path:
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _apply_migration(home: Path) -> None:
    from olano.migrations import _load_migrations

    for mod in _load_migrations():
        if mod.ID == "0011_managed_archetype_model_defaults":
            mod.apply(home)
            return
    raise AssertionError("migration 0011 not discovered")


def test_migration_moves_an_untouched_agentfather_onto_the_managed_tier(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )
    _apply_migration(tmp_path)
    assert _installed(path)["model"] == installer.MANAGED_CONDUCTOR_MODEL


def test_migration_moves_other_archetype_agents_to_turbo(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    shipped = installer.load_archetype_template("it-admin")["model"]
    path = _write_agent(
        agents, "helpdesk", {"name": "helpdesk", "archetype": "it-admin", "model": shipped}
    )
    _apply_migration(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_migration_leaves_a_deliberately_chosen_model_alone(managed, tmp_path) -> None:
    """The whole test for "the owner never chose this" is that the model still
    matches what the archetype shipped. Anything else is somebody's decision."""
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "openai:gpt-5.5"},
    )
    before = path.read_text(encoding="utf-8")
    _apply_migration(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_migration_moves_an_integration_agent_off_the_implicit_default(managed, tmp_path) -> None:
    """The toolkit-agent installer wrote no `model` key at all, so those agents
    ran on `AgentConfig`'s Anthropic default. Absent is not chosen."""
    agents = tmp_path / "agents"
    path = _write_agent(
        agents, "gmail", {"name": "gmail", "toolkit_agent": "gmail", "tools": ["gmail_send"]}
    )
    _apply_migration(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_migration_leaves_hand_written_agents_alone(managed, tmp_path) -> None:
    """No `archetype`/`template` provenance ⇒ not this migration's business."""
    agents = tmp_path / "agents"
    path = _write_agent(agents, "goku", {"name": "goku", "model": "anthropic:claude-sonnet-4-6"})
    before = path.read_text(encoding="utf-8")
    _apply_migration(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_migration_is_a_noop_on_an_unmanaged_box(unmanaged, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )
    before = path.read_text(encoding="utf-8")
    _apply_migration(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_migration_is_idempotent(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )
    _apply_migration(tmp_path)
    after_first = path.read_text(encoding="utf-8")
    _apply_migration(tmp_path)
    assert path.read_text(encoding="utf-8") == after_first


# ── the runner must not burn 0011 on an unmanaged boot ────────────────────────
#
# apply() returning early on an unmanaged box used to be recorded as applied
# all the same, so a first boot that raced the deployment manifest (posture
# still "open") permanently stranded every archetype agent on the BYOK
# literal. 0011 now exposes applicable() so the runner defers it instead.


def _only_0011():
    from olano.migrations import _load_migrations

    return [m for m in _load_migrations() if m.ID == "0011_managed_archetype_model_defaults"]


def test_unmanaged_boot_defers_the_migration_instead_of_recording_it(unmanaged, tmp_path) -> None:
    from unittest.mock import patch

    from olano.migrations import pending_migration_ids, run_pending_migrations

    with patch("olano.migrations._load_migrations", return_value=_only_0011()):
        assert run_pending_migrations(tmp_path, []) == []
        # Not pending either: the boot fast path must stay a no-op on a box
        # where the posture never flips (a genuinely unmanaged install).
        assert pending_migration_ids([]) == []


def test_migration_runs_on_the_boot_where_the_posture_flips(
    unmanaged, tmp_path, monkeypatch
) -> None:
    from unittest.mock import patch

    from olano.config import system_settings as ss
    from olano.migrations import run_pending_migrations

    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )

    with patch("olano.migrations._load_migrations", return_value=_only_0011()):
        assert run_pending_migrations(tmp_path, []) == []
        assert _installed(path)["model"] == "anthropic:claude-sonnet-4-6"

        # The deployment manifest lands, the next boot resolves managed —
        # the migration must still be there to run.
        monkeypatch.setattr(ss, "managed_olano_default", lambda: True)
        applied = run_pending_migrations(tmp_path, [])

    assert applied == ["0011_managed_archetype_model_defaults"]
    assert _installed(path)["model"] == installer.MANAGED_CONDUCTOR_MODEL


# ── migration 0020: the conductor comes off the deep tier ─────────────────────
#
# 0011/0012 put AgentFather on the deep tier (`olano:pro`, then the alias
# `default:pro`). The installer writes `default:turbo` now; 0020 moves the
# conductors already on disk, under the same "only what the installer wrote"
# rule, on every posture (the alias resolves per box).


def _apply_0020(home: Path) -> None:
    from olano.migrations import _load_migrations

    for mod in _load_migrations():
        if mod.ID == "0020_agentfather_turbo":
            mod.apply(home)
            return
    raise AssertionError("migration 0020 not discovered")


@pytest.mark.parametrize("current", ["default:pro", "olano:pro"])
def test_0020_moves_an_installer_placed_agentfather_to_turbo(unmanaged, tmp_path, current) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents, "agentfather", {"name": "agentfather", "archetype": "agentfather", "model": current}
    )
    _apply_0020(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_0020_recognises_a_conductor_by_name_when_provenance_is_missing(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(agents, "agentfather", {"name": "agentfather", "model": "default:pro"})
    _apply_0020(tmp_path)
    assert _installed(path)["model"] == "default:turbo"


def test_0020_leaves_owner_picked_models_and_other_agents_alone(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    chosen = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "default:max"},
    )
    # A worker on the deep tier is an owner's choice — 0020 is about the conductor.
    worker = _write_agent(
        agents, "analyst", {"name": "analyst", "archetype": "it-admin", "model": "default:pro"}
    )
    before = {p: p.read_text(encoding="utf-8") for p in (chosen, worker)}
    _apply_0020(tmp_path)
    for p, text in before.items():
        assert p.read_text(encoding="utf-8") == text


def test_0020_is_idempotent(managed, tmp_path) -> None:
    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "default:pro"},
    )
    _apply_0020(tmp_path)
    after_first = path.read_text(encoding="utf-8")
    _apply_0020(tmp_path)
    assert path.read_text(encoding="utf-8") == after_first


def test_0011_then_0012_then_0020_land_a_legacy_agentfather_on_turbo(managed, tmp_path) -> None:
    """The whole chain a box provisioned before any of this took: the pack
    literal → olano:pro (0011) → default:pro (0012) → default:turbo (0020)."""
    from olano.migrations import run_pending_migrations

    agents = tmp_path / "agents"
    path = _write_agent(
        agents,
        "agentfather",
        {"name": "agentfather", "archetype": "agentfather", "model": "anthropic:claude-sonnet-4-6"},
    )
    applied = run_pending_migrations(tmp_path, [])
    assert "0020_agentfather_turbo" in applied
    assert _installed(path)["model"] == "default:turbo"
