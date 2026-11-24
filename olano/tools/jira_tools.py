"""Jira tools for the JiraAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_JIRA_CREDS_MSG = (
    "[missing-credentials] JIRA_SERVER, JIRA_USER, and JIRA_API_TOKEN must be set in vault."
)


def _jira_client() -> tuple[Any, str | None]:
    """Return (client, error_string). One is always None."""
    jira_mod, err = optional_import("jira", purpose="Jira issue tracker access")
    if err:
        return None, err
    server = get_credential("JIRA_SERVER", "")
    user = get_credential("JIRA_USER") or get_credential("JIRA_EMAIL", "")
    token = get_credential("JIRA_API_TOKEN", "")
    if not all([server, user, token]):
        return None, _JIRA_CREDS_MSG
    assert jira_mod is not None  # narrowed: err is None ⇒ import succeeded
    try:
        return jira_mod.JIRA(server=server, basic_auth=(user, token)), None
    except Exception as exc:
        return None, f"[tool-error] Jira auth: {exc}"


@tool
def jira_list_issues(project: str = "", jql: str = "", limit: int = 10) -> str:
    """List Jira issues. Provide project key OR custom JQL query."""
    client, err = _jira_client()
    if err:
        return err
    try:
        if not jql:
            jql = f"project={project}" if project else "order by created DESC"
        issues = client.search_issues(jql, maxResults=limit)
        lines = []
        for i in issues:
            key = getattr(i, "key", "?")
            fields = getattr(i, "fields", None)
            status = getattr(getattr(fields, "status", None), "name", "?") if fields else "?"
            summary = getattr(fields, "summary", "?") if fields else "?"
            lines.append(f"[{key}] [{status}] {summary}")
        return "\n".join(lines) or "No issues found."
    except Exception as exc:
        return f"[tool-error] jira_list_issues: {exc}"


@tool
def jira_create_issue(
    project: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    priority: str = "Medium",
) -> str:
    """Create a Jira issue."""
    client, err = _jira_client()
    if err:
        return err
    try:
        issue = client.create_issue(
            project=project,
            summary=summary,
            description=description,
            issuetype={"name": issue_type},
            priority={"name": priority},
        )
        permalink = issue.permalink() if hasattr(issue, "permalink") else ""
        return f"Created {getattr(issue, 'key', '?')}: {summary} — {permalink}"
    except Exception as exc:
        return f"[tool-error] jira_create_issue: {exc}"


@tool
def jira_update_status(issue_key: str, transition_name: str) -> str:
    """Transition a Jira issue to a new status (e.g. 'In Progress', 'Done')."""
    client, err = _jira_client()
    if err:
        return err
    try:
        transitions = client.transitions(issue_key)
        tid = None
        for t in transitions:
            if t.get("name", "").lower() == transition_name.lower():
                tid = t.get("id")
                break
        if not tid:
            available = [t.get("name", "?") for t in transitions]
            return f"Transition '{transition_name}' not found. Available: {', '.join(available)}"
        client.transition_issue(issue_key, tid)
        return f"Moved {issue_key} to '{transition_name}'"
    except Exception as exc:
        return f"[tool-error] jira_update_status: {exc}"


@tool
def jira_add_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue."""
    client, err = _jira_client()
    if err:
        return err
    try:
        client.add_comment(issue_key, comment)
        return f"Comment added to {issue_key}"
    except Exception as exc:
        return f"[tool-error] jira_add_comment: {exc}"


@tool
def jira_get_issue(issue_key: str) -> str:
    """Get full details for a Jira issue."""
    client, err = _jira_client()
    if err:
        return err
    try:
        i = client.issue(issue_key)
        fields = getattr(i, "fields", None)
        assignee = getattr(getattr(fields, "assignee", None), "displayName", "unassigned")
        status = getattr(getattr(fields, "status", None), "name", "?")
        priority = getattr(getattr(fields, "priority", None), "name", "?")
        summary = getattr(fields, "summary", "?")
        description = getattr(fields, "description", "") or "(no description)"
        return (
            f"{issue_key}: {summary}\n"
            f"  Status: {status} | Priority: {priority} | Assignee: {assignee}\n\n"
            f"{description}"
        )
    except Exception as exc:
        return f"[tool-error] jira_get_issue: {exc}"


@tool
def jira_assign_issue(issue_key: str, assignee: str) -> str:
    """Assign a Jira issue to a user (use accountId or email, depending on Jira setup)."""
    client, err = _jira_client()
    if err:
        return err
    try:
        client.assign_issue(issue_key, assignee)
        return f"Assigned {issue_key} to {assignee}"
    except Exception as exc:
        return f"[tool-error] jira_assign_issue: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
