"""Per-kind assignment delivery.

Agents get a **dedicated issue poll** (``scheduler/cron.py``
``_add_issues_job`` — one per agent, deliberately independent of the
heartbeat): open assigned issues with activity since the agent's watermark
surface as one autonomous turn. Humans get a dashboard notification — their
work queue is the Issues view filtered to them. Author kind never matters;
only the assignee's kind picks the path.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.issues.principals import agent_principal, bare_name, kind

logger = logging.getLogger(__name__)


def pending_for_agent(agent_name: str) -> list[dict[str, Any]]:
    """Open issues assigned to this agent with activity since its watermark.

    Never raises — a broken issues DB must not take the scheduler down.
    The watermark advances via :func:`mark_checked` only, so a crashed
    delivery turn retries on the next poll.
    """
    try:
        from olano.issues.store import get_issue_store

        store = get_issue_store()
        principal = agent_principal(agent_name)
        assigned = store.list_issues(state="open", assignee=principal, limit=20)
        checked_at = store.get_watermark(principal)
        # ISO-8601 UTC timestamps — lexicographic compare is chronological.
        return [i for i in assigned if str(i.get("updated_at") or "") > checked_at]
    except Exception:
        logger.debug("pending_for_agent failed for %s", agent_name, exc_info=True)
        return []


def mark_checked(agent_name: str) -> None:
    """Advance the agent's watermark to now. Never raises."""
    try:
        from olano.issues.store import get_issue_store

        get_issue_store().set_watermark(agent_principal(agent_name))
    except Exception:
        logger.debug("mark_checked failed for %s", agent_name, exc_info=True)


def format_poll_message(pending: list[dict[str, Any]]) -> str:
    """The autonomous-turn prompt listing an agent's pending issues."""
    listing = "\n".join(
        f"#{i['id']} [{i.get('type', 'task')}] {i['title']} (from {i['author']})" for i in pending
    )
    return (
        "ISSUE TRACKER: assigned issues with new activity\n\n"
        f"{listing}\n\n"
        "Review each with issue_show, respond with issue_comment, and "
        "issue_close what you finish."
    )


def issue_action(issue_id: int | str) -> dict[str, str]:
    """The notification ``action`` that opens one issue's detail page.

    ``IssuesView`` addresses its detail page with ``?issue=N`` (selection lives
    in the URL so links are shareable), which is what makes an assignment
    notification a working link rather than a dead end at the list. One
    definition so a rebuilt link (``runtime/notifications._restored_action``)
    and a freshly raised one can never point at different places.
    """
    return {"label": "Open issue", "href": f"/issues?issue={issue_id}"}


def notify_assignment(issue: dict, actor: str) -> None:
    """Best-effort, never raises. No-op unless the assignee is a human."""
    assignee = str(issue.get("assignee") or "")
    if kind(assignee) != "human" or assignee == actor:
        return
    try:
        from olano.runtime.notifications import notify

        notify(
            kind="issue_assigned",
            title=f"Issue #{issue['id']} assigned to {bare_name(assignee)}",
            body=f"{actor} assigned: {issue.get('title', '')}",
            level="info",
            dedupe_key=f"issue-{issue['id']}-assign-{assignee}",
            # Without this the row names an issue it cannot open — the whole
            # point of the notification is to get the human to that issue.
            action=issue_action(issue["id"]),
        )
    except Exception:
        logger.debug("issue assignment notification failed", exc_info=True)
