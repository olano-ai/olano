"""Per-agent runtime state persisted as a sidecar JSON file.

This holds operational state that must survive a server/dashboard restart but
does NOT belong in ``agent.json5`` (which is hand-editable and rewritten wholesale
by ``save_agent_config``, stripping comments). Currently tracks the ``paused``
flag — the user's desired "off" state for an agent — so a paused agent stays
paused across restarts and only boots when explicitly started.

Stored at ``<home>/agents/<name>/runtime_state.json`` (see
``OlanoHomePaths.agent_runtime_state_path``). Removed automatically when the
agent is deleted, since the whole agent dir is rmtree'd.
"""

from __future__ import annotations

import json

from olano.config.home import get_olano_paths


def _load_runtime_state(name: str) -> dict:
    path = get_olano_paths().agent_runtime_state_path(name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_runtime_state(name: str, state: dict) -> None:
    path = get_olano_paths().agent_runtime_state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_agent_paused(name: str) -> bool:
    """Return True if the agent has been explicitly paused by the user.

    Defaults to False (not paused) when no state file exists, so newly created
    agents boot normally with zero configuration.
    """
    return bool(_load_runtime_state(name).get("paused", False))


def set_agent_paused(name: str, paused: bool) -> None:
    """Persist the agent's paused flag, preserving any other runtime state."""
    state = _load_runtime_state(name)
    state["paused"] = bool(paused)
    _save_runtime_state(name, state)
