"""Agent-facing FAST tools for ideas + brainstorming capture.

These are cheap, synchronous primitives the agent calls inline during a
conversation. DEEP expansion (research one-pagers, prototypes) happens via
cognition workers (see ``olano/cognition/playbooks.py``), triggered by a
Pulse ``run_worker`` action or a scheduled cognition job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context


def _workspace_root() -> Path:
    request_context = get_request_context()
    workspace_root = request_context.metadata.get("workspace_root")
    workspace = get_workspace(str(workspace_root) if workspace_root else None)
    workspace.init()
    return workspace.root


def _append_under_section(path: Path, heading: str, line: str, *, title: str) -> None:
    """Append ``line`` under ``## heading`` in a markdown file, creating as needed."""
    content = path.read_text(encoding="utf-8") if path.exists() else f"# {title}\n\n"
    head = f"## {heading}"
    if head in content:
        content = content.replace(f"{head}\n", f"{head}\n{line}\n", 1)
    else:
        content = content.rstrip() + f"\n\n{head}\n{line}\n"
    path.write_text(content, encoding="utf-8")


@tool
def idea_add(idea: str, tags: str = "") -> str:
    """Capture a new idea to IDEAS.md (under '## Inbox'). Fast, no confirmation needed.

    Use this whenever the user floats an idea, a 'what if', or something worth
    exploring later. ``tags`` is an optional comma-separated list."""
    root = _workspace_root()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = f"  _(tags: {tags.strip()})_" if tags.strip() else ""
    line = f"- [{ts}] {idea.strip()}{suffix}"
    _append_under_section(root / "IDEAS.md", "Inbox", line, title="Ideas")
    return "Captured idea to IDEAS.md."


@tool
def ideas_list(max_items: int = 20) -> str:
    """List captured ideas from IDEAS.md (most recent first)."""
    root = _workspace_root()
    path = root / "IDEAS.md"
    if not path.exists():
        return "No ideas captured yet."
    bullets = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith(("- ", "* "))
    ]
    if not bullets:
        return "No ideas captured yet."
    return "\n".join(reversed(bullets[-max_items:]))


@tool
def brainstorm_note(note: str, topic: str = "General") -> str:
    """Append a brainstorming note to BRAINSTORMING.md under the given topic heading."""
    root = _workspace_root()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"- [{ts}] {note.strip()}"
    _append_under_section(
        root / "BRAINSTORMING.md", topic.strip() or "General", line, title="Brainstorming"
    )
    return f"Added brainstorming note under '{topic.strip() or 'General'}'."


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
