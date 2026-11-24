"""Shared HEARTBEAT.md parsing and rendering.

Single source of truth for the heartbeat task format, used by both the
scheduler poll (``olano/scheduler/cron.py``) and the agent-facing
``heartbeat_*`` tools (``olano/tools/heartbeat_tools.py``) — one
parser/renderer pair so what the tools write is exactly what the poll
executes.

Format — a YAML-ish list; blank lines and ``#`` comments end an entry:

    - task: Check the inbox
      when: 0 9 * * 1-5      # 5-field cron (server-local, standard: 0=Sun) or "always"
      notes: Optional context passed along with the heartbeat message
"""

from __future__ import annotations

import re
from pathlib import Path

_TASK_RE = re.compile(r"-\s+task:\s+(.+)")
_WHEN_RE = re.compile(r"when:\s+(.+)")
_NOTES_RE = re.compile(r"notes:\s+(.+)")

EMPTY_HEARTBEAT_SCAFFOLD = (
    "# HEARTBEAT\n\n"
    "# Add entries like:\n"
    "# - task: Review the inbox\n"
    "#   when: always\n"
    "#   notes: Optional context\n"
)


def parse_heartbeat_text(content: str) -> list[dict[str, str]]:
    """Parse HEARTBEAT.md-style text into ``{task, when?, notes?}`` dicts."""
    tasks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if current.get("task"):
                tasks.append(current)
                current = {}
            continue

        match = _TASK_RE.match(stripped)
        if match:
            if current.get("task"):
                tasks.append(current)
            current = {"task": match.group(1).strip()}
            continue

        match = _WHEN_RE.match(stripped)
        if match and current:
            current["when"] = match.group(1).strip().strip('"').strip("'")
            continue

        match = _NOTES_RE.match(stripped)
        if match and current:
            current["notes"] = match.group(1).strip()
            continue

    if current.get("task"):
        tasks.append(current)
    return tasks


def parse_heartbeat_file(path: Path) -> list[dict[str, str]]:
    """Parse a HEARTBEAT.md file into task dicts."""
    return parse_heartbeat_text(path.read_text(encoding="utf-8"))


def render_heartbeat_tasks(tasks: list[dict[str, str]]) -> str:
    """Render task dicts back into HEARTBEAT.md text (inverse of the parser)."""
    if not tasks:
        return EMPTY_HEARTBEAT_SCAFFOLD

    lines: list[str] = []
    for task in tasks:
        lines.append(f"- task: {task['task']}")
        lines.append(f"  when: {task.get('when', 'always')}")
        notes = str(task.get("notes") or "").strip()
        if notes:
            lines.append(f"  notes: {notes}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
