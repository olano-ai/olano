"""Tests for olano/update/install_state.py."""

from __future__ import annotations

from pathlib import Path

from olano.update.install_state import (
    _MAX_HISTORY,
    InstallState,
    previous_version,
    read_install_state,
    record_version,
    write_install_state,
)


def test_write_then_read_round_trip(tmp_path: Path):
    state = InstallState(
        version="1.0.0",
        installed_at="2026-01-01T00:00:00",
        mode="local",
        applied_migrations=["0001_init"],
        history=[],
    )
    write_install_state(tmp_path, state)
    loaded = read_install_state(tmp_path)
    assert loaded.version == "1.0.0"
    assert loaded.installed_at == "2026-01-01T00:00:00"
    assert loaded.mode == "local"
    assert loaded.applied_migrations == ["0001_init"]


def test_missing_file_returns_defaults(tmp_path: Path):
    state = read_install_state(tmp_path)
    assert state.version is None
    assert state.applied_migrations == []
    assert state.history == []


def test_append_history_bounded(tmp_path: Path):
    # Create a state already at max history
    entries = [{"version": f"1.{i}.0", "installed_at": "x"} for i in range(_MAX_HISTORY)]
    state = InstallState(version="1.99.0", installed_at="y", history=entries)
    write_install_state(tmp_path, state)
    loaded = read_install_state(tmp_path)
    assert len(loaded.history) == _MAX_HISTORY


def test_record_version_appends_to_history(tmp_path: Path):
    # Write an initial state
    state = InstallState(version="1.0.0", installed_at="2026-01-01")
    write_install_state(tmp_path, state)
    record_version(tmp_path, "1.1.0", "2026-02-01")
    new_state = read_install_state(tmp_path)
    assert new_state.version == "1.1.0"
    assert len(new_state.history) == 1
    assert new_state.history[0]["version"] == "1.0.0"


def test_previous_version_returns_last_history_entry(tmp_path: Path):
    # History is ordered oldest-first; previous_version returns the last entry (most recent prior)
    state = InstallState(
        version="2.0.0",
        history=[
            {"version": "1.8.0", "installed_at": "x"},
            {"version": "1.9.0", "installed_at": "y"},
        ],
    )
    write_install_state(tmp_path, state)
    assert previous_version(tmp_path) == "1.9.0"


def test_previous_version_returns_none_on_empty_history(tmp_path: Path):
    write_install_state(tmp_path, InstallState(version="1.0.0"))
    assert previous_version(tmp_path) is None


def test_corrupt_json_returns_defaults(tmp_path: Path):
    (tmp_path / ".install.json").write_text("not json {{", encoding="utf-8")
    state = read_install_state(tmp_path)
    assert state.version is None
