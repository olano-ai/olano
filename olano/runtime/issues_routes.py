"""Dashboard routes for the issue tracker.

(The home revision history's routes live in ``routes/revisions.py``.)

The dashboard never touches ``issues.db`` directly; these routes are the
backend-agnostic seam a Phase 2 store swap slips behind (the issue-tracking
design record, section 6). Mutations performed
here are authored as the authenticated dashboard human
(``human-<user slug>``; the single-user Owner sentinel yields
``human-owner``) — humans are first-class principals next to agents.

Registered from ``runtime/api.py:create_api`` alongside pages/handshake
routes, so the central security middleware applies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from olano.issues.store import IssueError, get_issue_store

logger = logging.getLogger(__name__)


def _actor() -> str:
    """The authenticated dashboard human as a canonical principal."""
    from olano.issues.principals import HUMAN_PREFIX, human_principal

    try:
        from olano.runtime.auth import current_user

        user = current_user()
        if user is not None and user.username:
            return human_principal(user.username)
    except Exception:  # pragma: no cover - auth outside request context
        pass
    return f"{HUMAN_PREFIX}owner"


def _resolve_assignee_or_400(raw: str) -> str:
    from olano.issues.principals import resolve_assignee

    canonical, err = resolve_assignee(raw)
    if err:
        raise HTTPException(status_code=400, detail=err.removeprefix("[tool-error] "))
    return canonical


def register_issues_routes(app: Any) -> None:
    """Attach /api/issues* (the tracker)."""

    # ── issues ───────────────────────────────────────────────────────────

    @app.get("/api/issues")
    async def list_issues(
        state: str = "open",
        assignee: str = "",
        author: str = "",
        label: str = "",
        type: str = "",
        q: str = "",
        sort: str = "updated",
        limit: int = 50,
    ) -> dict:
        def _list() -> list[dict]:
            ranked = None
            if q:
                try:
                    from olano.issues.semantic import hybrid_ranked_ids

                    ranked = hybrid_ranked_ids(q, limit=max(limit * 4, 50))
                except Exception:
                    ranked = None
            return get_issue_store().list_issues(
                state="" if state == "all" else state,
                assignee=assignee,
                author=author,
                label=label,
                issue_type=type,
                query=q,
                sort=sort,
                limit=limit,
                ranked_ids=ranked,
            )

        issues = await asyncio.to_thread(_list)
        return {"issues": issues}

    @app.get("/api/issues/similar")
    async def similar_issues_api(q: str, exclude: int = 0, limit: int = 3) -> dict:
        """Semantically similar OPEN issues — the new-issue modal's duplicate
        hint. Empty (never an error) when embeddings are unavailable."""

        def _similar() -> list[dict]:
            try:
                from olano.issues.semantic import similar_issues

                matches = similar_issues(
                    q, limit=max(1, min(int(limit), 10)), exclude_id=exclude or None
                )
            except Exception:
                return []
            return [
                {"id": m["id"], "title": m["title"], "state": m["state"], "score": m["score"]}
                for m in matches
            ]

        return {"similar": await asyncio.to_thread(_similar) if q.strip() else []}

    @app.get("/api/issues/meta")
    async def issues_meta() -> dict:
        """Dropdown vocabulary for the dashboard: types, states, principals,
        and every label currently in use."""
        from olano.issues.principals import (
            AGENT_PREFIX,
            HUMAN_PREFIX,
            _agent_roster,
            _human_display_names,
            _human_roster,
        )
        from olano.issues.store import VALID_STATES, VALID_TYPES

        def _gather() -> dict:
            store = get_issue_store()
            labels: set[str] = set()
            counts = {"open": 0, "closed": 0}
            for issue in store.list_issues(state="", limit=200):
                labels.update(issue.get("labels") or [])
                if issue["state"] in counts:
                    counts[issue["state"]] += 1
            return {
                "types": list(VALID_TYPES),
                "states": list(VALID_STATES),
                "agents": [f"{AGENT_PREFIX}{a}" for a in _agent_roster()],
                "humans": [f"{HUMAN_PREFIX}{h}" for h in _human_roster()],
                # principal -> the name to show a reader. A handle is a slug and
                # reads as one ("carlos-m-2"); the UI renders this when present
                # and falls back to the principal. Only entries that differ are
                # included, so the map stays small and the fallback is the rule
                # rather than the exception.
                "display_names": {
                    f"{HUMAN_PREFIX}{u}": n for u, n in _human_display_names().items()
                },
                "labels": sorted(labels),
                "counts": counts,
            }

        return await asyncio.to_thread(_gather)

    @app.get("/api/issues/{issue_id}")
    async def get_issue(issue_id: int) -> dict:
        def _read() -> dict | None:
            store = get_issue_store()
            issue = store.get(issue_id)
            if issue is None:
                return None
            return {"issue": issue, "events": store.events(issue_id)}

        payload = await asyncio.to_thread(_read)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"No issue #{issue_id}")
        return payload

    @app.post("/api/issues")
    async def create_issue(body: dict) -> dict:
        title = str(body.get("title") or "")
        actor = _actor()
        assignee = _resolve_assignee_or_400(str(body.get("assignee") or ""))
        try:
            issue = await asyncio.to_thread(
                lambda: get_issue_store().create(
                    title=title,
                    body=str(body.get("body") or ""),
                    author=actor,
                    assignee=assignee,
                    labels=[str(item) for item in (body.get("labels") or [])],
                    issue_type=str(body.get("type") or "task"),
                )
            )
        except IssueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish_update(issue, "create")
        _deliver(issue, actor)
        try:
            from olano.issues.semantic import notify_index

            notify_index()
        except Exception:
            pass
        return {"issue": issue}

    @app.post("/api/issues/{issue_id}/comment")
    async def comment_issue(issue_id: int, body: dict) -> dict:
        actor = _actor()
        return await _mutate(
            issue_id,
            "comment",
            lambda store: store.comment(issue_id, actor=actor, body=str(body.get("body") or "")),
        )

    @app.post("/api/issues/{issue_id}/close")
    async def close_issue(issue_id: int, body: dict | None = None) -> dict:
        reason = str((body or {}).get("reason") or "")
        actor = _actor()
        return await _mutate(
            issue_id,
            "close",
            lambda store: store.set_state(issue_id, actor=actor, state="closed", reason=reason),
        )

    @app.post("/api/issues/{issue_id}/reopen")
    async def reopen_issue(issue_id: int, body: dict | None = None) -> dict:
        reason = str((body or {}).get("reason") or "")
        actor = _actor()
        return await _mutate(
            issue_id,
            "reopen",
            lambda store: store.set_state(issue_id, actor=actor, state="open", reason=reason),
        )

    @app.post("/api/issues/{issue_id}/labels")
    async def set_issue_labels(issue_id: int, body: dict) -> dict:
        labels = [str(item) for item in (body.get("labels") or [])]
        actor = _actor()
        return await _mutate(
            issue_id,
            "label",
            lambda store: store.set_labels(issue_id, actor=actor, labels=labels),
        )

    @app.post("/api/issues/{issue_id}/type")
    async def set_issue_type(issue_id: int, body: dict) -> dict:
        issue_type = str(body.get("type") or "")
        actor = _actor()
        return await _mutate(
            issue_id,
            "type",
            lambda store: store.set_type(issue_id, actor=actor, issue_type=issue_type),
        )

    @app.post("/api/issues/{issue_id}/assign")
    async def assign_issue(issue_id: int, body: dict) -> dict:
        assignee = _resolve_assignee_or_400(str(body.get("assignee") or ""))
        actor = _actor()
        result = await _mutate(
            issue_id,
            "assign",
            lambda store: store.assign(issue_id, actor=actor, assignee=assignee),
        )
        _deliver(result["issue"], actor)
        return result

    async def _mutate(issue_id: int, kind: str, op: Any) -> dict:
        try:
            issue = await asyncio.to_thread(lambda: op(get_issue_store()))
        except IssueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish_update(issue, kind)
        return {"issue": issue}


def _deliver(issue: dict, actor: str) -> None:
    from olano.issues.delivery import notify_assignment

    notify_assignment(issue, actor)


def _publish_update(issue: dict, kind: str) -> None:
    try:
        from olano.runtime.api import _push_event

        _push_event("issue_updated", {"id": issue["id"], "kind": kind, "state": issue["state"]})
    except Exception:
        pass
