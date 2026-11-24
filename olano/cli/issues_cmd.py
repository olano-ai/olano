"""Helpers behind the ``olano issues`` CLI group (see cli/main.py).

The CLI operates the local ``issues.db`` directly — no running server
needed. It is an owner-trusted surface: the default actor is
``human-owner``, and ``--as`` may name any resolvable principal (the CLI
runs with full local access, so impersonation is already in the operator's
power; the flag just keeps the audit log honest about it).
"""

from __future__ import annotations

from typing import Any

import click


def resolve_actor(as_user: str) -> str:
    """``--as`` value → canonical principal (default ``human-owner``)."""
    from olano.issues.principals import HUMAN_PREFIX, resolve_assignee

    if not as_user:
        return f"{HUMAN_PREFIX}owner"
    canonical, err = resolve_assignee(as_user)
    if err:
        raise click.ClickException(err.removeprefix("[tool-error] "))
    return canonical


def resolve_assignee_arg(assignee: str) -> str:
    from olano.issues.principals import resolve_assignee

    canonical, err = resolve_assignee(assignee)
    if err:
        raise click.ClickException(err.removeprefix("[tool-error] "))
    return canonical


def format_issue_line(issue: dict[str, Any]) -> str:
    labels = ", ".join(issue.get("labels") or [])
    return (
        f"#{issue['id']:<4} [{issue['state']:<6}] [{issue.get('type', 'task'):<8}] "
        f"{issue['title']}  (author: {issue['author']}, "
        f"assignee: {issue['assignee'] or '—'}"
        + (f", labels: {labels}" if labels else "")
        + f", updated: {issue['updated_at'][:19]})"
    )


def format_issue_thread(issue: dict[str, Any], events: list[dict[str, Any]]) -> str:
    lines = [format_issue_line(issue)]
    if issue.get("body"):
        lines.append(issue["body"])
    lines.append(f"created: {issue['created_at'][:19]}")
    for event in events:
        payload = event.get("payload") or {}
        if event["kind"] == "comment":
            lines.append(f"[{event['ts'][:19]}] {event['actor']}: {payload.get('body', '')}")
        else:
            detail = payload.get("assignee") or payload.get("reason") or ""
            suffix = f" ({detail})" if detail else ""
            lines.append(f"[{event['ts'][:19]}] {event['actor']} — {event['kind']}{suffix}")
    return "\n".join(lines)


def deliver_assignment(issue: dict[str, Any], actor: str) -> None:
    from olano.issues.delivery import notify_assignment

    notify_assignment(issue, actor)
