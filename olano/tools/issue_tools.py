"""Issue tracking tools — issues connect *principals* (agents and humans).

Seven tools over ``olano/issues/store.py`` — the stable contract for any
future backend swap (section 6 of the issue-tracking design record):
signatures here must not grow vendor concepts (repo/owner/index).

The author/actor of every mutation binds from the ``current_agent``
contextvar (``tools/talk_to.py``) as the canonical principal ``ai-<name>``,
never from a tool parameter — an agent cannot file or comment as somebody
else. Assignees may be agents (``ai-merli``) or dashboard humans
(``human-carlos``); bare names resolve when unambiguous
(``issues/principals.py``). Agent assignees surface at their next heartbeat
tick (``scheduler/cron.py``); human assignees get a dashboard notification.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from olano.issues.store import IssueError, get_issue_store
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


def _actor() -> str:
    from olano.issues.principals import agent_principal
    from olano.tools.talk_to import current_agent

    return agent_principal(current_agent.get())


def _publish(issue: dict, kind: str) -> None:
    """Best-effort ``issue_updated`` SSE ping for the dashboard.

    Deliberately carries no ``agent`` key: it is a refresh signal, not an
    attributed event, so the member-visibility filter lets it through.
    """
    try:
        from olano.tools import talk_to

        publisher = talk_to._PUBLISHER  # bound by RuntimeServer; None in CLI/tests
        if publisher:
            publisher("issue_updated", {"id": issue["id"], "kind": kind, "state": issue["state"]})
    except Exception:
        pass


def _deliver(issue: dict) -> None:
    from olano.issues.delivery import notify_assignment

    notify_assignment(issue, _actor())


def _schedule_embed() -> None:
    """Queue background embedding of new issues — never on the turn's path."""
    try:
        from olano.issues.semantic import notify_index

        notify_index()
    except Exception:
        pass


def _format_issue(issue: dict) -> str:
    head = (
        f"#{issue['id']} [{issue['state']}] [{issue.get('type', 'task')}] {issue['title']}"
        f" (author: {issue['author']}, assignee: {issue['assignee'] or '—'})"
    )
    labels = ", ".join(issue["labels"])
    return head + (f" [labels: {labels}]" if labels else "")


@tool
def issue_create(
    title: str,
    body: str = "",
    assignee: str = "",
    labels: str = "",
    issue_type: str = "task",
    allow_duplicate: bool = False,
) -> str:
    """Open a new issue, optionally assigned to an agent or a human.

    Use this for durable work items that should outlive the current
    conversation — bugs found, tasks to hand off, follow-ups. ``labels`` is
    comma-separated; ``issue_type`` is one of task|bug|feature|question|
    incident. Assignee accepts a bare name when unambiguous, or the explicit
    ``ai-<agent>`` / ``human-<user>`` form. Agent assignees see the issue at
    their next issue poll; human assignees get a dashboard notification.

    Duplicate check: if a semantically similar OPEN issue already exists,
    creation is refused with the matches listed — comment on the existing
    issue instead, or retry with ``allow_duplicate=true`` if it is genuinely
    distinct.
    """
    from olano.issues.principals import resolve_assignee

    canonical, err = resolve_assignee(assignee)
    if err:
        return err
    if not allow_duplicate:
        try:
            from olano.issues.semantic import duplicate_candidates

            dupes = duplicate_candidates(title, body)
        except Exception:
            dupes = []
        if dupes:
            listing = "\n".join(
                f"  #{d['id']} {d['title']} (similarity {d['score']})" for d in dupes
            )
            return (
                "[duplicate-check] Similar open issues already exist:\n"
                f"{listing}\n"
                "Comment on the existing issue (issue_comment) instead, or if this "
                "is genuinely a different problem retry with allow_duplicate=true."
            )
    try:
        issue = get_issue_store().create(
            title=title,
            body=body,
            author=_actor(),
            assignee=canonical,
            labels=[part.strip() for part in labels.split(",") if part.strip()],
            issue_type=issue_type,
        )
        _publish(issue, "create")
        _deliver(issue)
        _schedule_embed()
        return f"Created {_format_issue(issue)}"
    except IssueError as exc:
        return f"[tool-error] {exc}"
    except Exception as exc:
        return f"[tool-error] issue_create: {exc}"


@tool
def issue_comment(issue_id: int, body: str) -> str:
    """Add a comment to an existing issue (find ids with issue_list)."""
    try:
        issue = get_issue_store().comment(int(issue_id), actor=_actor(), body=body)
        _publish(issue, "comment")
        return f"Commented on {_format_issue(issue)}"
    except IssueError as exc:
        return f"[tool-error] {exc}"
    except Exception as exc:
        return f"[tool-error] issue_comment: {exc}"


@tool
def issue_close(issue_id: int, reason: str = "") -> str:
    """Close an open issue, optionally recording why (fixed, wontfix, duplicate…)."""
    try:
        issue = get_issue_store().set_state(
            int(issue_id), actor=_actor(), state="closed", reason=reason
        )
        _publish(issue, "close")
        return f"Closed {_format_issue(issue)}"
    except IssueError as exc:
        return f"[tool-error] {exc}"
    except Exception as exc:
        return f"[tool-error] issue_close: {exc}"


@tool
def issue_reopen(issue_id: int, reason: str = "") -> str:
    """Reopen a closed issue, optionally recording why."""
    try:
        issue = get_issue_store().set_state(
            int(issue_id), actor=_actor(), state="open", reason=reason
        )
        _publish(issue, "reopen")
        return f"Reopened {_format_issue(issue)}"
    except IssueError as exc:
        return f"[tool-error] {exc}"
    except Exception as exc:
        return f"[tool-error] issue_reopen: {exc}"


@tool
def issue_list(
    state: str = "open",
    assignee: str = "",
    author: str = "",
    label: str = "",
    issue_type: str = "",
    query: str = "",
    limit: int = 20,
) -> str:
    """List issues. state: open|closed|all. Filter by assignee/author/label/type.

    ``query`` searches title, body, and comments — hybrid keyword + semantic,
    so "billing error" also finds "payment failing"; results are best-match
    first. Tip: ``issue_list(assignee=<your own name>)`` is your work queue,
    and search before filing to avoid duplicates.
    """
    from olano.issues.principals import resolve_assignee

    canonical, err = resolve_assignee(assignee)
    if err:
        return err
    ranked = None
    if query:
        try:
            from olano.issues.semantic import hybrid_ranked_ids

            ranked = hybrid_ranked_ids(query, limit=max(limit * 4, 50))
        except Exception:
            ranked = None
    try:
        issues = get_issue_store().list_issues(
            state="" if state == "all" else state,
            assignee=canonical,
            author=author,
            label=label,
            issue_type=issue_type,
            query=query,
            sort="relevance" if query else "updated",
            limit=limit,
            ranked_ids=ranked,
        )
        if not issues:
            return "No matching issues."
        lines = [_format_issue(issue) for issue in issues]
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] issue_list: {exc}"


@tool
def issue_show(issue_id: int) -> str:
    """Show one issue in full: body plus the comment/state-change thread."""
    try:
        store = get_issue_store()
        issue = store.get(int(issue_id))
        if issue is None:
            return f"[tool-error] No issue #{issue_id}."
        lines = [_format_issue(issue)]
        if issue["body"]:
            lines.append(issue["body"])
        for event in store.events(int(issue_id)):
            payload = event["payload"]
            if event["kind"] == "comment":
                lines.append(f"[{event['ts']}] {event['actor']}: {payload.get('body', '')}")
            else:
                detail = payload.get("assignee") or payload.get("reason") or ""
                suffix = f" ({detail})" if detail else ""
                lines.append(f"[{event['ts']}] {event['actor']} — {event['kind']}{suffix}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] issue_show: {exc}"


@tool
def issue_assign(issue_id: int, assignee: str) -> str:
    """Assign an issue to an agent or human (empty assignee unassigns).

    Accepts a bare name when unambiguous, or ``ai-<agent>`` /
    ``human-<user>``. Agent assignees are notified at their next heartbeat
    tick (for urgent handoffs also message them with talk_to_agent); human
    assignees get a dashboard notification.
    """
    from olano.issues.principals import resolve_assignee

    canonical, err = resolve_assignee(assignee)
    if err:
        return err
    try:
        issue = get_issue_store().assign(int(issue_id), actor=_actor(), assignee=canonical)
        _publish(issue, "assign")
        _deliver(issue)
        return f"Assigned {_format_issue(issue)}"
    except IssueError as exc:
        return f"[tool-error] {exc}"
    except Exception as exc:
        return f"[tool-error] issue_assign: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
