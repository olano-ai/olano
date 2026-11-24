"""Read and write <home>/.install.json — tracks installed version and migration history."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_INSTALL_JSON = ".install.json"
_MAX_HISTORY = 20


@dataclass
class InstallState:
    version: str | None = None
    installed_at: str | None = None
    # "local" (uv tool / PyPI), "source" (git checkout), "docker", "binary".
    # Plain string so new modes stay readable by older releases.
    mode: str = "local"
    applied_migrations: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    # Code identity ("<version>@<git-sha>") of the last boot that ran the
    # post-upgrade steps — lets the runtime detect a `git pull` + restart
    # (where the package version never changes) and auto-apply upgrades.
    boot_marker: str | None = None


def read_install_state(home: Path) -> InstallState:
    path = home / _INSTALL_JSON
    if not path.exists():
        return InstallState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return InstallState()
    return InstallState(
        version=raw.get("version"),
        installed_at=raw.get("installed_at"),
        mode=raw.get("mode", "local"),
        applied_migrations=list(raw.get("applied_migrations") or []),
        history=list(raw.get("history") or []),
        boot_marker=raw.get("boot_marker"),
    )


def write_install_state(home: Path, state: InstallState) -> None:
    path = home / _INSTALL_JSON
    data = {
        "version": state.version,
        "installed_at": state.installed_at,
        "mode": state.mode,
        "applied_migrations": state.applied_migrations,
        "history": state.history[-_MAX_HISTORY:],
        "boot_marker": state.boot_marker,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_version(home: Path, new_version: str, installed_at: str) -> InstallState:
    """Append the current version to history and write the new version."""
    import os

    state = read_install_state(home)
    mode = os.environ.get("LIONCLAW_INSTALL_MODE", state.mode)
    if state.version:
        entry = {"version": state.version, "installed_at": state.installed_at}
        state.history.append(entry)
        state.history = state.history[-_MAX_HISTORY:]
    state.version = new_version
    state.installed_at = installed_at
    state.mode = mode
    write_install_state(home, state)
    return state


def previous_version(home: Path) -> str | None:
    """Return the last recorded version before the current one."""
    state = read_install_state(home)
    if state.history:
        return state.history[-1].get("version")
    return None
