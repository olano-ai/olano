"""Agent CRUD — create, read, update, delete agent configs.

Thin, validated wrappers over :mod:`olano.config.loader`. Every write path
round-trips through :class:`~olano.config.agent_config.AgentConfig` so a
malformed change is rejected before it touches disk.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from olano.config.agent_config import AgentConfig
from olano.config.home import get_olano_paths
from olano.config.loader import (
    find_agent_config_path,
    load_agent_config,
    save_agent_config,
)
from olano.config.loader import list_agents as _list_agent_names


def list_agents(*, source: str = "auto") -> list[dict[str, Any]]:
    """Return a summary record for every configured agent."""
    out: list[dict[str, Any]] = []
    # Deliberately unfiltered — this is the admin surface that *shows* the
    # hidden flag rather than acting on it.
    for name in _list_agent_names(source=source, include_hidden=True):
        try:
            cfg = load_agent_config(name, source=source)
        except Exception as exc:  # malformed config shouldn't hide the rest
            out.append({"name": name, "error": str(exc)})
            continue
        out.append(
            {
                "name": cfg.name,
                "description": cfg.description,
                "emoji": cfg.emoji,
                "model": cfg.model,
                "archetype": cfg.archetype,
                "human_only": cfg.human_only,
                "hidden": cfg.hidden,
                "tools": cfg.tools,
                "subagents": [s.name for s in cfg.subagents],
            }
        )
    return out


def get_agent(name: str, *, source: str = "auto") -> dict[str, Any]:
    """Return the full validated config for a single agent."""
    cfg = load_agent_config(name, source=source)
    return cfg.model_dump(exclude={"workspace"})


def create_agent(
    name: str,
    *,
    model: str | None = None,
    display_name: str = "",
    description: str = "",
    system_prompt: str = "",
    tools: list[str] | None = None,
    overwrite: bool = False,
    extra: dict[str, Any] | None = None,
    title: str = "",
) -> dict[str, Any]:
    """Create a new agent config from scratch.

    ``extra`` may carry any other ``AgentConfig`` field (e.g. ``security``,
    ``memory``, ``channels``). The merged dict is validated before saving.
    ``title`` is the agent's position and lands in ``org.title`` — the one
    field every surface reads a title from — merged over any ``org`` block
    ``extra`` carries.
    """
    if find_agent_config_path(name, source="home") is not None and not overwrite:
        raise FileExistsError(f"Agent '{name}' already exists. Pass overwrite=True to replace it.")
    data: dict[str, Any] = {"name": name, "description": description}
    if display_name.strip():
        data["display_name"] = display_name.strip()
    model, model_note = resolve_model(model)
    if model:
        data["model"] = model
    if system_prompt:
        data["system_prompt"] = system_prompt
    if tools is not None:
        data["tools"] = tools
    if extra:
        data.update(extra)
    if title.strip():
        org = dict(data.get("org") or {})
        org["title"] = title.strip()
        data["org"] = org
    secrets = _vault_channel_secrets(name, data)
    cfg = AgentConfig.model_validate(data)
    path = save_agent_config(cfg)
    result = {"name": cfg.name, "path": str(path), "created": True, "model": cfg.model, **secrets}
    if model_note:
        result["model_note"] = model_note
    return result


def set_external_access(
    name: str,
    *,
    mcp: bool | None = None,
    a2a: bool | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Change whether outside systems may reach ``name`` over MCP / A2A.

    ``None`` leaves a field as it is. The config model enforces the rule that a
    description is required while either door is open, so turning one on
    without a description fails loudly here rather than at boot. Takes effect on
    the agent's next restart (the caller decides when).
    """
    cfg = load_agent_config(name, source="home")
    current = cfg.external_access.model_dump()
    if mcp is not None:
        current["mcp"] = bool(mcp)
    if a2a is not None:
        current["a2a"] = bool(a2a)
    if description is not None:
        current["description"] = description.strip()
    result = update_agent(name, {"external_access": current})
    result["external_access"] = current
    result["applies"] = "after restart"
    return result


def update_agent(name: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into an existing agent config and re-validate.

    Top-level keys in ``updates`` replace the corresponding config keys; the
    result is validated through ``AgentConfig`` before being written, so an
    invalid edit fails loudly instead of corrupting the file.
    """
    from olano.deployment.locks import deployment_write_lock

    # Read-modify-write under one lock hold: this is the dashboard's save path,
    # and loading outside the lock let a concurrent edit be clobbered wholesale
    # (the write rewrites the entire file from a model that never saw it).
    with deployment_write_lock():
        cfg = load_agent_config(name, source="home")
        data = cfg.model_dump(exclude={"workspace"})
        data.update(updates)
        data["name"] = name  # name is the identity — never let updates rename in place
        secrets = _vault_channel_secrets(name, data)
        new_cfg = AgentConfig.model_validate(data)
        path = save_agent_config(new_cfg)
    return {"name": name, "path": str(path), "updated": sorted(updates.keys()), **secrets}


def delete_agent(name: str, *, purge_sessions: bool = True) -> dict[str, Any]:
    """Delete an agent's config (and, by default, its JSONL session transcripts).

    Removes ``<home>/agents/<name>/``, the per-agent session directory
    ``<home>/sessions/<name>/`` and the agent's checkpoint history. Checkpoint
    rows are keyed by an agent-scoped ``<agent>::<thread_id>``, so the purge is
    every thread under that prefix (``checkpoints.delete_agent_threads``);
    they used to be left behind as orphans, and on a busy box those were the
    largest rows in the store, scanned by every sweep and never loadable again.
    """
    path = find_agent_config_path(name, source="home")
    if path is None:
        raise FileNotFoundError(f"Agent '{name}' not found in the home agents dir.")
    agent_dir = path.parent
    removed: list[str] = []
    if agent_dir.exists():
        shutil.rmtree(agent_dir)
        removed.append(str(agent_dir))
    purged_threads = 0
    if purge_sessions:
        sessions_dir = get_olano_paths().sessions_dir / name
        if sessions_dir.exists():
            shutil.rmtree(sessions_dir)
            removed.append(str(sessions_dir))
        from olano.core.checkpoints import delete_agent_threads

        purged_threads = delete_agent_threads(name)
    from olano.runtime import notifications

    notifications.forget_agent(name)
    return {
        "name": name,
        "deleted": True,
        "removed_paths": removed,
        "purged_threads": purged_threads,
    }


def resolve_model(model: str | None) -> tuple[str | None, str | None]:
    """Settle a new agent's model. Returns ``(model, note)``.

    Two different situations, deliberately handled differently:

    * **No model asked for** — do not fall through to ``AgentConfig``'s
      ``anthropic:claude-sonnet-4-6`` default, which is wrong on any install
      without an Anthropic key. Pick the cheapest model the install can actually
      run (subscription and local models rank ahead of metered ones) and say so.
    * **A model was asked for whose provider isn't connected** — say so, and
      keep it anyway. Substituting a model someone chose on purpose is worse
      than a clear warning: the key may be about to be added, and silently
      running a different model than requested is not a decision a tool gets to
      make.
    """
    from olano.admin.models import cheapest_model, model_provider_connected

    if model:
        connected, detail = model_provider_connected(model)
        if connected:
            return model, None
        return model, (
            f"'{model}' is set, but its provider is not connected ({detail}). "
            "The agent will fail on its first turn until that is fixed — "
            "connect the provider or pick a model from `olano_list_models`."
        )

    picked = cheapest_model()
    if not picked:
        return None, (
            "No provider is connected, so the built-in default model was used. "
            "Connect a provider (or run `olano auth openai-codex`) before "
            "talking to this agent."
        )
    return picked, f"No model was specified — using the cheapest connected model, '{picked}'."


def _vault_channel_secrets(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Move any channel credential in ``data`` into the vault, in place.

    The dashboard's write path has always done this (``_write_agent_config``);
    this is the same guarantee for the paths that bypass it — AgentFather's
    ``olano_create_agent`` / ``olano_update_agent`` and the CLI. Without
    it an agent-written config keeps a live bot token in plain text on disk.

    Returns the bits of the report worth handing back to the caller, so a tool
    can tell the operator where the credential went (or that a locked vault
    left it in the file).
    """
    from olano.config.channel_secrets import absorb_channel_secrets

    report = absorb_channel_secrets(name, data)
    result: dict[str, Any] = {}
    if report.vaulted:
        result["vaulted"] = [{"channel": c, "field": f, "key": k} for c, f, k in report.vaulted]
    if not report.vault_available:
        result["vault_locked"] = True
        result["plaintext_fields"] = [{"channel": c, "field": f} for c, f in report.left_in_config]
    return result


def _agent_dir(name: str) -> Path:
    return get_olano_paths().agents_dir / name
