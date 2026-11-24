"""Linear tools for the LinearAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_LINEAR_CREDS_MSG = "[missing-credentials] LINEAR_API_KEY not set in vault."


def _linear_request(query: str, variables: dict | None = None) -> tuple[dict | None, str | None]:
    """Execute a Linear GraphQL query. Returns (data, error_string)."""
    requests_mod, err = optional_import("requests", purpose="Linear API access")
    if err:
        return None, err
    assert requests_mod is not None
    token = get_credential("LINEAR_API_KEY")
    if not token:
        return None, _LINEAR_CREDS_MSG
    try:
        resp = requests_mod.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            return None, f"[tool-error] Linear API: {data['errors']}"
        return data.get("data", {}), None
    except Exception as exc:
        return None, f"[tool-error] Linear request: {exc}"


@tool
def linear_list_issues(team: str = "", limit: int = 10) -> str:
    """List Linear issues, optionally filtered by team key (e.g. 'ENG')."""
    filter_clause = f'filter: {{team: {{key: {{eq: "{team}"}}}}}}, ' if team else ""
    query = f"""
    query {{
      issues({filter_clause}first: {limit}) {{
        nodes {{ id identifier title state {{ name }} priority assignee {{ name }} }}
      }}
    }}
    """
    data, err = _linear_request(query)
    if err:
        return err
    issues = (data or {}).get("issues", {}).get("nodes", [])
    lines = []
    for i in issues:
        state = i.get("state", {}).get("name", "?")
        assignee = (i.get("assignee") or {}).get("name", "unassigned")
        lines.append(f"[{i.get('identifier', '?')}] [{state}] {i.get('title', '?')} ({assignee})")
    return "\n".join(lines) or "No issues found."


@tool
def linear_create_issue(
    title: str, description: str = "", team_key: str = "", priority: int = 0
) -> str:
    """Create a Linear issue. priority: 0=none, 1=urgent, 2=high, 3=medium, 4=low."""
    team_id = ""
    if team_key:
        data, err = _linear_request(
            f'query {{ teams(filter: {{key: {{eq: "{team_key}"}}}}) {{ nodes {{ id }} }} }}'
        )
        if err:
            return err
        nodes = (data or {}).get("teams", {}).get("nodes", [])
        if nodes:
            team_id = nodes[0].get("id", "")

    variables: dict[str, Any] = {"title": title, "description": description, "priority": priority}
    if team_id:
        variables["teamId"] = team_id

    query = """
    mutation CreateIssue($title: String!, $description: String, $teamId: String, $priority: Int) {
      issueCreate(input: {title: $title, description: $description, teamId: $teamId, priority: $priority}) {
        success
        issue { identifier title url }
      }
    }
    """
    data, err = _linear_request(query, variables)
    if err:
        return err
    issue = (data or {}).get("issueCreate", {}).get("issue", {})
    return f"Created {issue.get('identifier')}: {issue.get('title')} — {issue.get('url')}"


@tool
def linear_update_issue(issue_id: str, state_name: str = "", priority: int | None = None) -> str:
    """Update a Linear issue's state or priority."""
    variables: dict[str, Any] = {"id": issue_id}
    update: dict[str, Any] = {}

    if state_name:
        data, err = _linear_request(
            f'query {{ workflowStates(filter: {{name: {{eq: "{state_name}"}}}}) {{ nodes {{ id }} }} }}'
        )
        if err:
            return err
        nodes = (data or {}).get("workflowStates", {}).get("nodes", [])
        if nodes:
            update["stateId"] = nodes[0].get("id")

    if priority is not None:
        update["priority"] = priority

    variables["input"] = update
    query = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) { success issue { identifier title } }
    }
    """
    data, err = _linear_request(query, variables)
    if err:
        return err
    issue = (data or {}).get("issueUpdate", {}).get("issue", {})
    return f"Updated {issue.get('identifier')}: {issue.get('title')}"


@tool
def linear_add_comment(issue_id: str, body: str) -> str:
    """Add a comment to a Linear issue."""
    query = """
    mutation CommentCreate($id: String!, $body: String!) {
      commentCreate(input: {issueId: $id, body: $body}) {
        success
        comment { id }
      }
    }
    """
    data, err = _linear_request(query, {"id": issue_id, "body": body})
    if err:
        return err
    comment = (data or {}).get("commentCreate", {}).get("comment", {})
    return f"Comment created on {issue_id}: {comment.get('id', '?')}"


@tool
def linear_get_issue(identifier: str) -> str:
    """Get full details of a Linear issue by identifier (e.g. ENG-123)."""
    query = """
    query GetIssue($id: String!) {
      issue(id: $id) {
        identifier title description state { name } priority url
        assignee { name } team { key name } createdAt updatedAt
      }
    }
    """
    data, err = _linear_request(query, {"id": identifier})
    if err:
        return err
    issue = (data or {}).get("issue")
    if not issue:
        return f"Issue {identifier} not found."
    return (
        f"{issue.get('identifier')}: {issue.get('title')}\n"
        f"  State: {issue.get('state', {}).get('name', '?')} | "
        f"Priority: {issue.get('priority', '?')}\n"
        f"  Assignee: {(issue.get('assignee') or {}).get('name', 'unassigned')}\n"
        f"  Team: {issue.get('team', {}).get('key', '?')}\n"
        f"  URL: {issue.get('url', '?')}\n"
        f"  Updated: {issue.get('updatedAt', '?')}\n\n"
        f"{issue.get('description') or '(no description)'}"
    )


get_tools, get_tool = make_toolkit_api(globals())
