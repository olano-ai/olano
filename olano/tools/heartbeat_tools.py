"""Heartbeat management tools for HEARTBEAT.md and skill-provided checks."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.scheduler.heartbeat import parse_heartbeat_text, render_heartbeat_tasks
from olano.security.tool_metadata import tool_metadata
from olano.skills.loader import discover_skills


def _heartbeat_path() -> Path:
    workspace = get_workspace()
    workspace.init()
    return workspace.root / "HEARTBEAT.md"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    _, _, remainder = text.partition("---\n")
    fm_text, sep, body = remainder.partition("\n---\n")
    if not sep:
        return {}, text
    return yaml.safe_load(fm_text) or {}, body


def _read_tasks(path: Path | None = None) -> list[dict[str, str]]:
    target = path or _heartbeat_path()
    if not target.exists():
        return []
    return parse_heartbeat_text(target.read_text(encoding="utf-8"))


def _write_tasks(tasks: list[dict[str, str]]) -> None:
    _heartbeat_path().write_text(render_heartbeat_tasks(tasks), encoding="utf-8")


def _resolve_task_index(tasks: list[dict[str, str]], task_ref: str) -> int | None:
    ref = task_ref.strip()
    if not ref:
        return None
    if ref.isdigit():
        idx = int(ref) - 1
        return idx if 0 <= idx < len(tasks) else None

    exact = [idx for idx, item in enumerate(tasks) if item.get("task", "").lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]

    prefix = [
        idx
        for idx, item in enumerate(tasks)
        if item.get("task", "").lower().startswith(ref.lower())
    ]
    if len(prefix) == 1:
        return prefix[0]
    return None


def _find_skill(skill_name: str) -> Path | None:
    workspace = get_workspace()
    roots = [workspace.skills_dir, workspace.managed_skills_dir]
    for root in roots:
        for skill in discover_skills(root):
            location = Path(skill["location"])
            aliases = {location.parent.name.lower(), skill["name"].lower()}
            if skill_name.lower() in aliases:
                return location
    return None


def _collect_section(body: str, heading: str) -> str:
    wanted = heading.strip().lower()
    collecting = False
    lines: list[str] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            collecting = stripped[3:].strip().lower() == wanted
            continue
        if collecting:
            lines.append(raw_line)
    return "\n".join(lines).strip()


def _parse_skill_heartbeats(skill_path: Path) -> list[dict[str, str]]:
    text = skill_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    items: list[dict[str, str]] = []

    declared = frontmatter.get("heartbeats") or []
    if isinstance(declared, list):
        for entry in declared:
            if isinstance(entry, str):
                item = {"task": entry.strip(), "when": "always"}
            elif isinstance(entry, dict):
                item = {
                    "task": str(entry.get("task") or "").strip(),
                    "when": str(entry.get("when") or "always").strip(),
                    "notes": str(entry.get("notes") or "").strip(),
                }
            else:
                continue
            if item["task"]:
                items.append(item)

    section = _collect_section(body, "Heartbeats")
    if section:
        items.extend(parse_heartbeat_text(section))

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        task_key = item.get("task", "").strip().lower()
        if task_key and task_key not in seen:
            seen.add(task_key)
            deduped.append(item)
    return deduped


@tool
def heartbeat_list() -> str:
    """List the current HEARTBEAT.md checks."""
    path = _heartbeat_path()
    tasks = _read_tasks(path)
    if not tasks:
        return f"No heartbeat checks configured in {path.name}."

    lines = [f"Heartbeat checks ({len(tasks)} total):"]
    for idx, task in enumerate(tasks, start=1):
        notes = f" | notes: {task['notes']}" if task.get("notes") else ""
        lines.append(f"{idx}. {task['task']} | when: {task.get('when', 'always')}{notes}")
    return "\n".join(lines)


@tool
def heartbeat_add(
    task: Annotated[str, "Task description to add to HEARTBEAT.md"],
    when: Annotated[str, "Cron expression or 'always'"] = "always",
    notes: Annotated[str, "Optional context passed with the heartbeat task"] = "",
) -> str:
    """Add a new heartbeat check."""
    task = task.strip()
    when = when.strip() or "always"
    if not task:
        return "Error: heartbeat task cannot be empty."

    tasks = _read_tasks()
    if any(item.get("task", "").lower() == task.lower() for item in tasks):
        return f"Error: heartbeat task '{task}' already exists."

    item = {"task": task, "when": when}
    if notes.strip():
        item["notes"] = notes.strip()
    tasks.append(item)
    _write_tasks(tasks)
    return f"Heartbeat task added: {task} ({when})."


@tool
def heartbeat_update(
    task_ref: Annotated[str, "Task number from heartbeat_list or an exact task name/prefix"],
    task: Annotated[str, "Updated task text (leave empty to keep current)"] = "",
    when: Annotated[str, "Updated cron expression or 'always' (leave empty to keep current)"] = "",
    notes: Annotated[str, "Updated notes (leave empty to keep current)"] = "",
    clear_notes: Annotated[bool, "Set true to remove notes from the task"] = False,
) -> str:
    """Update an existing heartbeat check."""
    tasks = _read_tasks()
    idx = _resolve_task_index(tasks, task_ref)
    if idx is None:
        return f"Error: no heartbeat task matched '{task_ref}'."

    entry = tasks[idx]
    if task.strip():
        entry["task"] = task.strip()
    if when.strip():
        entry["when"] = when.strip()
    if clear_notes:
        entry.pop("notes", None)
    elif notes.strip():
        entry["notes"] = notes.strip()

    _write_tasks(tasks)
    return f"Heartbeat task updated: {entry['task']}."


@tool
def heartbeat_remove(
    task_ref: Annotated[str, "Task number from heartbeat_list or an exact task name/prefix"],
) -> str:
    """Remove a heartbeat check from HEARTBEAT.md."""
    tasks = _read_tasks()
    idx = _resolve_task_index(tasks, task_ref)
    if idx is None:
        return f"Error: no heartbeat task matched '{task_ref}'."

    removed = tasks.pop(idx)
    _write_tasks(tasks)
    return f"Heartbeat task removed: {removed['task']}."


def _current_agent_name() -> str:
    """The agent this tool call runs for: the bound runtime context, else the
    home-config default agent."""
    from olano.tools.talk_to import current_agent

    name = current_agent.get() or ""
    if name:
        return name
    from olano.config.home import load_home_config

    return load_home_config().default_agent


def _parse_on_off(value: str) -> bool | None:
    """Map a user-ish on/off string to a bool; '' → None (leave unchanged)."""
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"on", "true", "yes", "enable", "enabled", "1"}:
        return True
    if normalized in {"off", "false", "no", "disable", "disabled", "0"}:
        return False
    raise ValueError(f"Expected 'on' or 'off', got {value!r}.")


def _sync_running_scheduler(agent: str) -> str:
    """Push the agent's (re-loaded) effective heartbeat settings into the live
    scheduler, if one is running in this process."""
    from olano.config.loader import load_agent_config
    from olano.config.system_settings import (
        effective_heartbeat_enabled,
        effective_heartbeat_interval,
    )
    from olano.scheduler.cron import get_active_scheduler

    scheduler = get_active_scheduler()
    if scheduler is None:
        return "Runtime scheduler not active in this process — applies on next runtime start."
    config = load_agent_config(agent)
    ok, message = scheduler.set_heartbeat_options(
        agent,
        enabled=effective_heartbeat_enabled(config),
        interval_minutes=effective_heartbeat_interval(config),
    )
    return message if ok else f"Live sync skipped: {message}"


@tool
def heartbeat_status() -> str:
    """Show heartbeat polling status for this agent: enabled/disabled, effective
    poll interval, where each value comes from (agent override vs system default),
    and how many checks are configured."""
    from olano.config.loader import load_agent_config
    from olano.config.system_settings import (
        effective_heartbeat_enabled,
        effective_heartbeat_interval,
        load_heartbeat_settings,
    )

    agent = _current_agent_name()
    try:
        config = load_agent_config(agent)
    except Exception:
        config = None

    system = load_heartbeat_settings()
    hb = getattr(config, "heartbeat", None)
    enabled_src = (
        "agent override" if hb is not None and hb.enabled is not None else "system default"
    )
    interval_src = (
        "agent override" if hb is not None and hb.interval_minutes is not None else "system default"
    )
    enabled = effective_heartbeat_enabled(config)
    interval = effective_heartbeat_interval(config)
    tasks = _read_tasks()

    from olano.scheduler.cron import get_active_scheduler

    runtime_note = (
        "Live in the running scheduler."
        if get_active_scheduler() is not None
        else "Runtime scheduler not active in this process — settings apply when the runtime runs."
    )
    return "\n".join(
        [
            f"Heartbeat for '{agent}': {'ENABLED' if enabled else 'DISABLED'} ({enabled_src}).",
            f"Poll interval: every {interval} min ({interval_src}; system default is "
            f"{system.interval_minutes} min).",
            f"Checks in HEARTBEAT.md: {len(tasks)} (skill HEARTBEAT.md files are also polled).",
            runtime_note,
        ]
    )


# Rewrites agent.json5 and changes how often the agent polls on its own:
# a config write, so owner-only and approved like the other config tools.
@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def heartbeat_configure(
    enabled: Annotated[
        str, "'on' to enable polling, 'off' to disable, empty to leave unchanged"
    ] = "",
    interval_minutes: Annotated[
        int, "Minutes between heartbeat polls (1-1440); 0 leaves the interval unchanged"
    ] = 0,
    reset_to_system_default: Annotated[
        bool, "Remove this agent's overrides so it follows the system-wide heartbeat settings"
    ] = False,
) -> str:
    """Enable/disable this agent's heartbeat polling or change how often it runs.
    Persists to agent.json5 and applies live to the running scheduler."""
    from olano.config.agent_config import AgentConfig
    from olano.config.loader import _load_json5, find_agent_config_path
    from olano.config.system_settings import (
        HEARTBEAT_INTERVAL_MAX_MINUTES,
        HEARTBEAT_INTERVAL_MIN_MINUTES,
    )

    try:
        enabled_value = _parse_on_off(enabled)
    except ValueError as exc:
        return f"Error: {exc}"
    if interval_minutes and not (
        HEARTBEAT_INTERVAL_MIN_MINUTES <= interval_minutes <= HEARTBEAT_INTERVAL_MAX_MINUTES
    ):
        return (
            f"Error: interval_minutes must be between {HEARTBEAT_INTERVAL_MIN_MINUTES} and "
            f"{HEARTBEAT_INTERVAL_MAX_MINUTES} (got {interval_minutes})."
        )
    if enabled_value is None and not interval_minutes and not reset_to_system_default:
        return (
            "Nothing to change. Pass enabled='on'/'off', interval_minutes=1-1440, "
            "or reset_to_system_default=true."
        )

    agent = _current_agent_name()
    path = find_agent_config_path(agent)
    if path is None:
        return f"Error: no agent.json5 found for agent '{agent}'."

    import json

    data = _load_json5(path.read_text(encoding="utf-8")) or {}
    data.setdefault("name", agent)
    block = {} if reset_to_system_default else dict(data.get("heartbeat") or {})
    if enabled_value is not None:
        block["enabled"] = enabled_value
    if interval_minutes:
        block["interval_minutes"] = interval_minutes
    block = {k: v for k, v in block.items() if v is not None}
    if block:
        data["heartbeat"] = block
    else:
        data.pop("heartbeat", None)

    # Validate the merged config before touching disk so a bad patch can never
    # corrupt agent.json5.
    try:
        AgentConfig.model_validate(dict(data))
    except Exception as exc:
        return f"Error: change would produce an invalid agent config: {exc}"

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return f"Saved heartbeat settings for '{agent}'. {_sync_running_scheduler(agent)}"


@tool
def heartbeat_install_from_skill(
    skill_name: Annotated[str, "Skill directory name or skill title"],
    replace_existing: Annotated[
        bool,
        "When true, overwrite matching heartbeat tasks with the skill's definitions",
    ] = False,
) -> str:
    """Install heartbeat checks declared by a local skill."""
    skill_path = _find_skill(skill_name.strip())
    if skill_path is None:
        return f"Error: skill '{skill_name}' was not found in workspace or managed skills."

    declared = _parse_skill_heartbeats(skill_path)
    if not declared:
        return f"Error: skill '{skill_name}' does not declare any heartbeat checks."

    tasks = _read_tasks()
    by_task = {item.get("task", "").lower(): idx for idx, item in enumerate(tasks)}
    added = 0
    updated = 0
    skipped = 0

    for item in declared:
        key = item["task"].lower()
        if key in by_task:
            if replace_existing:
                tasks[by_task[key]] = item
                updated += 1
            else:
                skipped += 1
            continue
        by_task[key] = len(tasks)
        tasks.append(item)
        added += 1

    _write_tasks(tasks)
    return (
        f"Installed heartbeat checks from skill '{skill_name}': "
        f"added={added}, updated={updated}, skipped={skipped}."
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
