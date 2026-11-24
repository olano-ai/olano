"""System diagnostics — version + a lightweight health summary.

``update_lionclaw`` itself is intentionally *not* here: upgrading the package is
a shell operation and is exposed (HITL-gated) through the ``olano_admin``
toolkit's ``olano_update`` tool, which runs ``olano update``.
"""

from __future__ import annotations

from typing import Any

from olano.config.home import get_olano_paths, load_home_config
from olano.config.loader import list_agents as _list_agent_names


def lionclaw_version() -> dict[str, Any]:
    """Return the installed olano package version (best effort)."""
    try:
        from importlib.metadata import version

        v = version("olano")
    except Exception:
        v = "unknown"
    return {"version": v}


def doctor() -> dict[str, Any]:
    """Return a lightweight health summary of the active olano home."""
    paths = get_olano_paths()
    cfg = load_home_config()
    return {
        **lionclaw_version(),
        "home": str(paths.root),
        "home_exists": paths.root.exists(),
        "config_path": str(paths.config_path),
        "default_agent": cfg.default_agent,
        "checkpoint_backend": cfg.checkpoint_backend,
        "store_backend": cfg.store_backend,
        "agents": _list_agent_names(),
        "logs_dir": str(paths.logs_dir),
        "fleet_scoped_tool_holders": fleet_scoped_tool_holders(),
        "fleet_access_holders": fleet_access_holders(),
    }


def fleet_access_holders() -> list[str]:
    """The agents whose shell and file tools reach the whole deployment.

    ``agent.json5 → fleet_access`` (``security/fleet_access.py``) opens every
    agent's directory, the common skills and the logs. AgentFather is expected
    here; any other name is worth a look. Best-effort, like
    :func:`fleet_scoped_tool_holders`: an unreadable config is skipped.
    """
    from olano.config.loader import load_agent_config

    out: list[str] = []
    for name in _list_agent_names():
        try:
            cfg = load_agent_config(name, source="home")
        except Exception:
            continue
        if bool(getattr(cfg, "fleet_access", False)):
            out.append(name)
    return out


def fleet_scoped_tool_holders() -> list[dict[str, Any]]:
    """Which agents hold tools that act across the whole deployment, and which.

    Nothing grants the ``olano_admin`` toolkit by default, and the agent-driven
    grant paths are admin-gated — but an agent may already carry one from an
    earlier version or a deliberate operator tick. This is how an operator
    audits that without opening every config: ``olano_doctor`` from AgentFather,
    or the CLI's doctor. AgentFather itself is expected here; anything else is
    worth a look. Best-effort — an unreadable config is skipped, never fatal.
    """
    from olano.admin.integrations import privileged_tool_names
    from olano.config.loader import load_agent_config

    try:
        privileged = privileged_tool_names()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for name in _list_agent_names():
        try:
            cfg = load_agent_config(name, source="home")
        except Exception:
            continue
        held = {t for t in (cfg.tools or []) if t in privileged}
        for sub in cfg.subagents or []:
            held.update(t for t in (sub.tools or []) if t in privileged)
        if held:
            out.append({"agent": name, "tools": sorted(held)})
    return out
