"""AsanaAgent tools — project and task management via Asana API.

Auth env vars:
  ASANA_ACCESS_TOKEN — Personal Access Token or OAuth token
  ASANA_DEFAULT_WORKSPACE — workspace GID (optional, auto-detected)
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_BASE = "https://app.asana.com/api/1.0"


def _headers() -> dict:
    token = get_credential("ASANA_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("[missing-credentials] ASANA_ACCESS_TOKEN not set in vault.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get_workspace() -> str:
    ws = get_credential("ASANA_DEFAULT_WORKSPACE", "")
    if ws:
        return ws
    r = httpx.get(f"{_BASE}/workspaces", headers=_headers(), timeout=10)
    r.raise_for_status()
    items = r.json().get("data", [])
    if not items:
        raise ValueError("No Asana workspaces found.")
    return cast(str, items[0]["gid"])


@tool
def asana_list_projects(
    workspace_gid: Annotated[str, "Workspace GID (optional, uses default)"] = "",
    archived: Annotated[bool, "Include archived projects"] = False,
) -> str:
    """List Asana projects in a workspace."""
    try:
        ws = workspace_gid or _get_workspace()
        r = httpx.get(
            f"{_BASE}/projects",
            headers=_headers(),
            params={
                "workspace": ws,
                "archived": str(archived).lower(),
                "opt_fields": "gid,name,notes,created_at",
            },
            timeout=15,
        )
        r.raise_for_status()
        projects = r.json().get("data", [])
        if not projects:
            return "No projects found."
        lines = [f"Asana projects ({len(projects)}):"]
        for p in projects:
            lines.append(f"  [{p['gid']}] {p['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_list_tasks(
    project_gid: Annotated[str, "Project GID to list tasks from"],
    completed: Annotated[bool, "Include completed tasks"] = False,
    limit: Annotated[int, "Max tasks to return"] = 25,
) -> str:
    """List tasks in an Asana project."""
    try:
        r = httpx.get(
            f"{_BASE}/tasks",
            headers=_headers(),
            params={
                "project": project_gid,
                "completed_since": "now" if not completed else "",
                "opt_fields": "gid,name,completed,due_on,assignee.name,notes",
                "limit": limit,
            },
            timeout=15,
        )
        r.raise_for_status()
        tasks = r.json().get("data", [])
        if not tasks:
            return "No tasks found."
        lines = [f"Tasks in project {project_gid} ({len(tasks)}):"]
        for t in tasks:
            status = "[x]" if t.get("completed") else "[ ]"
            assignee = (t.get("assignee") or {}).get("name", "unassigned")
            due = t.get("due_on", "no due date")
            lines.append(f"  {status} [{t['gid']}] {t['name']} — {assignee} | due: {due}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_create_task(
    name: Annotated[str, "Task name"],
    project_gid: Annotated[str, "Project GID to add the task to"],
    notes: Annotated[str, "Task description / notes"] = "",
    due_on: Annotated[str, "Due date YYYY-MM-DD (optional)"] = "",
    assignee_gid: Annotated[str, "Assignee GID (optional)"] = "",
) -> str:
    """Create a new task in an Asana project."""
    try:
        data: dict = {"name": name, "projects": [project_gid]}
        if notes:
            data["notes"] = notes
        if due_on:
            data["due_on"] = due_on
        if assignee_gid:
            data["assignee"] = assignee_gid

        r = httpx.post(
            f"{_BASE}/tasks",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"data": data},
            timeout=15,
        )
        r.raise_for_status()
        task = r.json().get("data", {})
        return f"Task created: [{task['gid']}] {task['name']}"
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_update_task(
    task_gid: Annotated[str, "Task GID to update"],
    name: Annotated[str, "New task name (optional)"] = "",
    notes: Annotated[str, "New notes (optional)"] = "",
    completed: Annotated[bool, "Mark as completed"] = False,
    due_on: Annotated[str, "New due date YYYY-MM-DD (optional)"] = "",
) -> str:
    """Update an existing Asana task."""
    try:
        data: dict = {}
        if name:
            data["name"] = name
        if notes:
            data["notes"] = notes
        if completed:
            data["completed"] = True
        if due_on:
            data["due_on"] = due_on

        r = httpx.put(
            f"{_BASE}/tasks/{task_gid}",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"data": data},
            timeout=15,
        )
        r.raise_for_status()
        task = r.json().get("data", {})
        return f"Task updated: [{task['gid']}] {task['name']}"
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_add_comment(
    task_gid: Annotated[str, "Task GID"],
    text: Annotated[str, "Comment text"],
) -> str:
    """Add a comment to an Asana task."""
    try:
        r = httpx.post(
            f"{_BASE}/tasks/{task_gid}/stories",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"data": {"text": text}},
            timeout=15,
        )
        r.raise_for_status()
        story = r.json().get("data", {})
        return f"Comment added (story GID: {story.get('gid', '?')})"
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_list_sections(
    project_gid: Annotated[str, "Project GID"],
) -> str:
    """List sections (columns) in an Asana project."""
    try:
        r = httpx.get(
            f"{_BASE}/projects/{project_gid}/sections",
            headers=_headers(),
            params={"opt_fields": "gid,name"},
            timeout=10,
        )
        r.raise_for_status()
        sections = r.json().get("data", [])
        if not sections:
            return "No sections found."
        lines = [f"Sections in project {project_gid}:"]
        for s in sections:
            lines.append(f"  [{s['gid']}] {s['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] asana: {e}"


@tool
def asana_move_task(
    task_gid: Annotated[str, "Task GID to move"],
    section_gid: Annotated[str, "Target section GID"],
) -> str:
    """Move a task to a different section in Asana."""
    try:
        r = httpx.post(
            f"{_BASE}/sections/{section_gid}/addTask",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"data": {"task": task_gid}},
            timeout=15,
        )
        r.raise_for_status()
        return f"Task {task_gid} moved to section {section_gid}."
    except Exception as e:
        return f"[tool-error] asana: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
