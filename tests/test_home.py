"""Tests for Olano home resolution and per-home config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from olano.config.home import (
    HomeConfig,
    ensure_olano_home,
    get_olano_home,
    get_olano_paths,
    is_postgres_available,
    load_home_config,
    resolve_checkpoint_backend,
    resolve_store_backend,
    save_home_config,
    set_olano_home,
    update_home_config,
)

_BACKEND_ENVS = (
    "LIONCLAW_CHECKPOINT_BACKEND",
    "LIONCLAW_CHECKPOINT_DB_URL",
    "LIONCLAW_STORE_BACKEND",
    "LIONCLAW_STORE_DB_URL",
)


def _clear_backend_env(monkeypatch) -> None:
    for var in _BACKEND_ENVS:
        monkeypatch.delenv(var, raising=False)


from olano.core.checkpoints import (
    _BUSY_TIMEOUT_MS,
    aclose_agent_checkpointer,
    aclose_checkpointers,
    get_async_checkpointer,
    get_checkpointer,
)


def test_get_lionclaw_home_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OLANO_HOME", raising=False)
    monkeypatch.delenv("LIONCLAW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    # A host with none of the directories present gets the current default. An
    # existing ~/olano or ~/.lionclaw is still preferred over creating a new
    # one — see test_existing_legacy_dir_used_when_no_new_dir.
    assert get_olano_home() == (tmp_path / ".olano").resolve()


def test_get_lionclaw_home_env(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "client-a"
    monkeypatch.setenv("LIONCLAW_HOME", str(custom))

    assert get_olano_home() == custom.resolve()


def test_get_lionclaw_home_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "from-env"))
    override = tmp_path / "from-flag"

    assert get_olano_home(override) == override.resolve()


def test_set_lionclaw_home_updates_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LIONCLAW_HOME", raising=False)
    home = set_olano_home(tmp_path / "workspace")

    assert home == (tmp_path / "workspace").resolve()
    assert get_olano_home() == home


def test_ensure_lionclaw_home_creates_scaffold(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    paths = ensure_olano_home()

    assert paths.agents_dir.exists()
    assert paths.vault_dir.exists()
    assert paths.sessions_dir.exists()
    assert paths.logs_dir.exists()
    assert paths.scheduler_dir.exists()
    assert paths.config_path.exists()


def test_save_and_load_home_config(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    save_home_config(
        HomeConfig(default_agent="captain", runtime_api_port=9001, dashboard_port=4200)
    )
    loaded = load_home_config()

    assert loaded.default_agent == "captain"
    assert loaded.runtime_api_port == 9001
    assert loaded.dashboard_port == 4200


def test_update_home_config_merges_known_fields(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    save_home_config(HomeConfig(default_agent="captain", dashboard_port=4200))
    loaded = update_home_config(
        {"default_agent": "merli", "runtime_api_port": 9100, "unknown_key": "ignored"}
    )

    # Patched fields applied, untouched fields preserved, unknown keys dropped.
    assert loaded.default_agent == "merli"
    assert loaded.runtime_api_port == 9100
    assert loaded.dashboard_port == 4200
    raw = yaml.safe_load(get_olano_paths().config_path.read_text())
    assert "unknown_key" not in raw


def test_update_home_config_preserves_cognition_block(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    # Seed a config.yaml that also carries a cognition block (owned by another
    # module). update_home_config must not clobber it the way save_home_config would.
    save_home_config(HomeConfig(default_agent="captain"))
    config_path = get_olano_paths().config_path
    raw = yaml.safe_load(config_path.read_text())
    raw["cognition"] = {"autonomous": False, "features": {"pulse": False}}
    config_path.write_text(yaml.dump(raw, sort_keys=False))

    update_home_config({"default_agent": "merli"})

    after = yaml.safe_load(config_path.read_text())
    assert after["default_agent"] == "merli"
    assert after["cognition"] == {"autonomous": False, "features": {"pulse": False}}


def test_get_lionclaw_paths(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    paths = get_olano_paths()

    assert paths.agents_dir == home / "agents"
    assert paths.vault_db_path == home / "vault" / "vault.db"
    assert paths.scheduler_db_path == home / "scheduler" / "scheduler.db"
    assert paths.chat_history_path == home / "logs" / "chat_history"


async def test_async_checkpointers_can_be_closed_and_reopened(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    ensure_olano_home()

    first = await get_async_checkpointer("merli")
    await aclose_checkpointers()
    second = await get_async_checkpointer("merli")

    assert first is not second

    await aclose_checkpointers()


async def test_async_checkpointer_sets_busy_timeout(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    ensure_olano_home()

    saver = await get_async_checkpointer("merli")
    cursor = await saver.conn.execute("PRAGMA busy_timeout")
    row = await cursor.fetchone()
    await cursor.close()

    # A generous busy_timeout is what turns concurrent-write contention into a
    # WAIT instead of an immediate "database is locked".
    assert row is not None and row[0] == _BUSY_TIMEOUT_MS

    await aclose_checkpointers()


def test_sync_checkpointer_sets_busy_timeout(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    ensure_olano_home()

    saver = get_checkpointer("merli")
    row = saver.conn.execute("PRAGMA busy_timeout").fetchone()

    assert row is not None and row[0] == _BUSY_TIMEOUT_MS


async def test_aclose_agent_evicts_only_that_agent(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    ensure_olano_home()

    # An ephemeral worker name (unique per run) + a stable agent.
    ephemeral_name = "merli__cog__fact_distill__abc123"
    ephemeral = await get_async_checkpointer(ephemeral_name)
    stable = await get_async_checkpointer("merli")

    # Cached: re-fetch returns the same instance until evicted.
    assert await get_async_checkpointer(ephemeral_name) is ephemeral

    await aclose_agent_checkpointer(ephemeral_name)

    # The ephemeral connection is gone (a fresh one is opened on next fetch),
    # while the stable agent's connection is untouched.
    assert await get_async_checkpointer(ephemeral_name) is not ephemeral
    assert await get_async_checkpointer("merli") is stable

    # Evicting an unknown name is a harmless no-op.
    await aclose_agent_checkpointer("never-cached")

    await aclose_checkpointers()


# ── Persistence-backend resolution ──────────────────────────────────────────


def test_resolve_checkpoint_backend_default(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    # Postgres is now the default backend. A bare config (no URL) raises the
    # actionable error — there is no silent SQLite fallback.
    with pytest.raises(ValueError, match="requires a connection URL"):
        resolve_checkpoint_backend(HomeConfig())
    # With a URL it resolves to postgres.
    assert resolve_checkpoint_backend(HomeConfig(checkpoint_db_url="postgresql://default/db")) == (
        "postgres",
        "postgresql://default/db",
    )


def test_resolve_checkpoint_backend_from_yaml(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    cfg = HomeConfig(checkpoint_backend="postgres", checkpoint_db_url="postgresql://yaml/db")
    assert resolve_checkpoint_backend(cfg) == ("postgres", "postgresql://yaml/db")


def test_resolve_checkpoint_backend_env_overrides_yaml(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_BACKEND", "postgres")
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_DB_URL", "postgresql://env/db")
    # YAML says sqlite, but env wins.
    cfg = HomeConfig(checkpoint_backend="sqlite")
    assert resolve_checkpoint_backend(cfg) == ("postgres", "postgresql://env/db")


def test_resolve_checkpoint_backend_postgres_requires_url(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    with pytest.raises(ValueError, match="requires a connection URL"):
        resolve_checkpoint_backend(HomeConfig(checkpoint_backend="postgres"))


def test_resolve_checkpoint_backend_rejects_unknown(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    with pytest.raises(ValueError, match="Unknown persistence backend"):
        resolve_checkpoint_backend(HomeConfig(checkpoint_backend="mysql"))


def test_resolve_store_backend_inherits_checkpoint_url(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    # store=postgres with no store URL inherits the checkpoint DSN.
    cfg = HomeConfig(
        checkpoint_backend="postgres",
        checkpoint_db_url="postgresql://shared/db",
        store_backend="postgres",
    )
    assert resolve_store_backend(cfg) == ("postgres", "postgresql://shared/db")


def test_resolve_store_backend_default(monkeypatch) -> None:
    _clear_backend_env(monkeypatch)
    # Postgres is the default; a bare config (no URL to inherit) raises.
    with pytest.raises(ValueError, match="requires a connection URL"):
        resolve_store_backend(HomeConfig())
    # With a checkpoint DSN, the store inherits it (one server, distinct tables).
    assert resolve_store_backend(HomeConfig(checkpoint_db_url="postgresql://default/db")) == (
        "postgres",
        "postgresql://default/db",
    )


def test_backend_config_round_trips_through_yaml(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "client-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    _clear_backend_env(monkeypatch)
    save_home_config(
        HomeConfig(checkpoint_backend="postgres", checkpoint_db_url="postgresql://x/db"),
        override=home,
    )
    loaded = load_home_config(home)
    assert loaded.checkpoint_backend == "postgres"
    assert loaded.checkpoint_db_url == "postgresql://x/db"


def test_is_postgres_available_returns_bool() -> None:
    assert isinstance(is_postgres_available(), bool)


# ── home resolution across the lionclaw → olano rename ──────────────────────
#
# The precedence chain is the whole contract of the rename: an already-deployed
# instance must keep resolving to the ~/.lionclaw (or the visible ~/olano of
# the months that was the default) that holds its agents, vault and sessions,
# while a fresh host gets ~/.olano. Every branch is pinned here because a
# regression looks, to an operator, like their data was deleted.


@pytest.fixture
def _clean_home_env(monkeypatch):
    monkeypatch.delenv("OLANO_HOME", raising=False)
    monkeypatch.delenv("LIONCLAW_HOME", raising=False)


def test_explicit_override_beats_everything(tmp_path, monkeypatch, _clean_home_env):
    monkeypatch.setenv("OLANO_HOME", str(tmp_path / "from-env"))
    assert get_olano_home(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_olano_home_env_wins_over_legacy_env(tmp_path, monkeypatch, _clean_home_env):
    monkeypatch.setenv("OLANO_HOME", str(tmp_path / "new"))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "legacy"))
    assert get_olano_home() == (tmp_path / "new").resolve()


def test_legacy_env_still_honoured(tmp_path, monkeypatch, _clean_home_env):
    """Both currently-deployed instances set only LIONCLAW_HOME."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "legacy"))
    assert get_olano_home() == (tmp_path / "legacy").resolve()


def test_legacy_env_beats_an_existing_new_directory(tmp_path, monkeypatch, _clean_home_env):
    """A pinned home must win even if an empty ~/.olano appears beside it.

    This is the data-loss guard: the TC unit file pins LIONCLAW_HOME, and a
    stray ~/.olano must not silently redirect the runtime away from it.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".olano").mkdir()
    legacy = tmp_path / "pinned"
    legacy.mkdir()
    monkeypatch.setenv("LIONCLAW_HOME", str(legacy))
    assert get_olano_home() == legacy.resolve()


def test_existing_legacy_dir_used_when_no_new_dir(tmp_path, monkeypatch, _clean_home_env):
    """An upgraded box with no env pin keeps its ~/.lionclaw."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".lionclaw").mkdir()
    assert get_olano_home() == (tmp_path / ".lionclaw").resolve()


def test_existing_dot_olano_used_when_no_new_dir(tmp_path, monkeypatch, _clean_home_env):
    """An existing ~/.olano is the home, with or without anything beside it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".olano").mkdir()
    assert get_olano_home() == (tmp_path / ".olano").resolve()


def test_new_dir_preferred_when_both_exist(tmp_path, monkeypatch, _clean_home_env):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".olano").mkdir()
    (tmp_path / ".lionclaw").mkdir()
    assert get_olano_home() == (tmp_path / ".olano").resolve()


def test_hidden_dir_preferred_when_all_three_exist(tmp_path, monkeypatch, _clean_home_env):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "olano").mkdir()
    (tmp_path / ".olano").mkdir()
    (tmp_path / ".lionclaw").mkdir()
    assert get_olano_home() == (tmp_path / ".olano").resolve()


def test_fresh_host_gets_dot_olano(tmp_path, monkeypatch, _clean_home_env):
    from olano.config.home import get_default_olano_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert get_olano_home() == (tmp_path / ".olano").resolve()
    assert get_default_olano_home() == (tmp_path / ".olano").resolve()


def test_existing_visible_dir_without_markers_is_the_home(tmp_path, monkeypatch, _clean_home_env):
    """A plain ~/olano (no checkout markers) is adopted: it is what an install
    made while the visible name was the default holds."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "olano").mkdir()
    (tmp_path / "olano" / "config.yaml").write_text("default_agent: merli\n")
    assert get_olano_home() == (tmp_path / "olano").resolve()


def test_visible_dir_beats_the_original_name(tmp_path, monkeypatch, _clean_home_env):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "olano").mkdir()
    (tmp_path / ".lionclaw").mkdir()
    assert get_olano_home() == (tmp_path / "olano").resolve()


@pytest.mark.parametrize("marker", ["pyproject.toml", ".git"])
def test_source_checkout_at_visible_name_is_never_the_home(
    tmp_path, monkeypatch, _clean_home_env, marker
):
    """``git clone`` from a home directory lands exactly at ~/olano.

    Adopting that checkout as the home would put the vault, every agent and
    every session in a git work tree, so it is skipped and the fresh home goes
    to the default ~/.olano. Both markers count: a checkout always has
    pyproject.toml, and .git covers a clone of any other project named olano.
    """
    from olano.config.home import get_default_olano_home, is_source_checkout

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    checkout = tmp_path / "olano"
    checkout.mkdir()
    if marker == ".git":
        (checkout / marker).mkdir()
    else:
        (checkout / marker).write_text("[project]\nname = 'olano'\n")
    assert is_source_checkout(checkout)
    assert get_olano_home() == (tmp_path / ".olano").resolve()
    assert get_default_olano_home() == (tmp_path / ".olano").resolve()
    assert not (checkout / "config.yaml").exists()


def test_legacy_data_still_wins_beside_a_checkout(tmp_path, monkeypatch, _clean_home_env):
    """A checkout at ~/olano changes where a NEW home goes, never which existing one is used."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "olano").mkdir()
    (tmp_path / "olano" / "pyproject.toml").write_text("")
    (tmp_path / ".lionclaw").mkdir()
    assert get_olano_home() == (tmp_path / ".lionclaw").resolve()


def test_a_home_is_never_mistaken_for_a_checkout(tmp_path):
    from olano.config.home import is_source_checkout

    home = tmp_path / "olano"
    (home / "agents" / "merli").mkdir(parents=True)
    (home / "config.yaml").write_text("default_agent: merli\n")
    assert not is_source_checkout(home)
    assert not is_source_checkout(tmp_path / "absent")


def test_set_home_writes_both_env_names(tmp_path, monkeypatch, _clean_home_env):
    """A subprocess still running pre-rename code reads LIONCLAW_HOME; leaving
    it stale would point parent and child at different homes."""
    import os

    set_olano_home(tmp_path / "target")
    assert os.environ["OLANO_HOME"] == str((tmp_path / "target").resolve())
    assert os.environ["LIONCLAW_HOME"] == str((tmp_path / "target").resolve())


def test_is_default_home_accepts_every_default_layout(tmp_path, monkeypatch):
    from olano.config.home import is_default_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert is_default_home(tmp_path / "olano")
    assert is_default_home(tmp_path / ".olano")
    assert is_default_home(tmp_path / ".lionclaw")
    assert not is_default_home(tmp_path / "somewhere-else")


# ── the parsed-config store ───────────────────────────────────────────────────
# `read_home_config_doc` is the single read path for <home>/config.yaml: every
# `load_*_settings()` and `load_home_config()` goes through it. Its invariants
# are load-bearing and easy to break by accident, so they get named tests
# rather than incidental coverage from the settings loaders.


def _write_config(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_doc_returns_the_parsed_document(tmp_path):
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    _write_config(home, "default_agent: merli\ncognition:\n  intensity: relaxed\n")
    doc = read_home_config_doc(home)
    assert doc["default_agent"] == "merli"
    assert doc["cognition"]["intensity"] == "relaxed"


def test_config_doc_is_a_copy_the_caller_may_mutate(tmp_path):
    """The read-modify-write writers (`update_home_config`,
    `system_settings._write_raw`) mutate the dict they are handed. Returning the
    shared snapshot would let one of them corrupt what every later reader sees,
    with no write to disk to explain it."""
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    _write_config(home, "default_agent: merli\ncognition:\n  intensity: relaxed\n")

    got = read_home_config_doc(home)
    got["cognition"]["intensity"] = "MUTATED"
    got["injected"] = True

    fresh = read_home_config_doc(home)
    assert fresh["cognition"]["intensity"] == "relaxed"
    assert "injected" not in fresh


def test_config_doc_is_keyed_per_home(tmp_path):
    """Tests and `--home` point at different homes inside one process; a process
    singleton would serve one home's settings to another."""
    from olano.config.home import read_home_config_doc

    a, b = tmp_path / "a", tmp_path / "b"
    _write_config(a, "default_agent: alpha\n")
    _write_config(b, "default_agent: beta\n")
    assert read_home_config_doc(a)["default_agent"] == "alpha"
    assert read_home_config_doc(b)["default_agent"] == "beta"
    assert read_home_config_doc(a)["default_agent"] == "alpha"


def test_config_doc_sees_a_rewrite_of_the_same_size(tmp_path):
    """The cache is revalidated by stat on every read, so there is no staleness
    window — not even for an in-place rewrite that keeps the file the same size
    (`olano config` runs in a separate process; operators hand-edit this file).
    Same length either way so only the mtime/inode can distinguish them."""
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    for i in range(50):
        want = "relaxedxx" if i % 2 else "aggressive"
        _write_config(home, f"cognition:\n  intensity: {want}\n")
        assert read_home_config_doc(home)["cognition"]["intensity"] == want, f"stale at {i}"


def test_config_doc_sees_a_write_through_the_atomic_writer(tmp_path):
    from olano.config.home import read_home_config_doc, write_home_config_text

    home = tmp_path / "home"
    _write_config(home, "default_agent: merli\n")
    assert read_home_config_doc(home)["default_agent"] == "merli"

    write_home_config_text(home / "config.yaml", "default_agent: other\n")
    assert read_home_config_doc(home)["default_agent"] == "other"


def test_config_doc_missing_file_is_empty_not_an_error(tmp_path):
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    home.mkdir()
    assert read_home_config_doc(home) == {}
    # …and it recovers once the file appears.
    _write_config(home, "default_agent: merli\n")
    assert read_home_config_doc(home)["default_agent"] == "merli"


def test_config_doc_raises_on_an_unparseable_file(tmp_path):
    """Caching a parse must not change what failure means. Swallowing this would
    turn a malformed config into a silent reset of every home-level field — a
    reverted runtime_api_port, a dropped checkpoint_db_url — where the old code
    raised. The settings loaders each catch it and fall back to defaults;
    `load_home_config` deliberately does not."""
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    _write_config(home, "{{{ not yaml")
    with pytest.raises(Exception):
        read_home_config_doc(home)


def test_config_doc_raises_on_a_non_mapping_document(tmp_path):
    from olano.config.home import read_home_config_doc

    home = tmp_path / "home"
    _write_config(home, "- a\n- b\n")
    with pytest.raises(TypeError):
        read_home_config_doc(home)


def test_config_doc_invalidate_forces_a_reparse(tmp_path):
    from olano.config.home import invalidate_home_config_doc, read_home_config_doc

    home = tmp_path / "home"
    _write_config(home, "default_agent: merli\n")
    assert read_home_config_doc(home)["default_agent"] == "merli"
    invalidate_home_config_doc(home / "config.yaml")
    assert read_home_config_doc(home)["default_agent"] == "merli"
    invalidate_home_config_doc()  # whole-cache clear
    assert read_home_config_doc(home)["default_agent"] == "merli"


def test_config_doc_is_actually_cached(tmp_path, monkeypatch):
    """The point of the store: an unchanged file is parsed once, not once per
    question asked. One GET /api/agents/{name} used to parse this file 22 times."""
    import olano.config.home as home_mod

    home = tmp_path / "home"
    _write_config(home, "default_agent: merli\ncognition:\n  intensity: relaxed\n")
    home_mod.invalidate_home_config_doc()

    parses = {"n": 0}
    real_load = home_mod.yaml.load

    def counting(*a, **kw):
        parses["n"] += 1
        return real_load(*a, **kw)

    monkeypatch.setattr(home_mod.yaml, "load", counting)
    for _ in range(10):
        home_mod.read_home_config_doc(home)
    assert parses["n"] == 1, f"expected one parse for ten reads, got {parses['n']}"
