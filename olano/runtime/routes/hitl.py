"""Human-in-the-loop approvals and dashboard notifications.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

from typing import Any

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def _decider_principal() -> str:
    """The dashboard user resolving an approval, as an issues-style principal.

    ``human-<username>``, the same identity the issue tracker records, so one
    person reads the same in both audit trails. Single-user mode resolves to
    the owner account.
    """
    from olano.issues.principals import human_principal

    user = auth_layer.current_user()
    if user is None:
        return "unknown"
    return human_principal(user.username or user.id)


def register_hitl_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the hitl routes to ``app``."""

    @app.get("/api/hitl/policy")
    async def hitl_policy() -> dict:
        """Expose the HITL approval policy so the dashboard can compute a tool's
        *effective* approval state without duplicating the tool→category map.

        The map here is the single source of truth shared with the runtime
        (``olano.security.hitl_policy``); the UI layers per-agent profile,
        toggles, and per-tool overrides on top of it client-side.
        """
        from olano.security.hitl_policy import CATEGORY_TOOLS, PROFILE_CATEGORIES

        return {
            "categories": {cat: sorted(tools) for cat, tools in CATEGORY_TOOLS.items()},
            "profiles": {p: sorted(cats) for p, cats in PROFILE_CATEGORIES.items()},
            "write_toggle_category": "write_external",
            "shell_toggle_category": "shell",
        }

    @app.get("/api/hitl/pending")
    async def hitl_pending() -> dict:
        """List approval requests deferred to the inbox (awaiting a human).

        These are gated actions an autonomous (scheduled/cognition) or delegated
        A2A turn paused on — the turn is blocked on each request's future until
        resolved here or it times out. See ``security/hitl.py``.
        """
        from olano.security import hitl

        # Members only see (and can only count) approvals for their agents.
        allowed = auth_layer.allowed_agent_names()
        items = []
        for req in hitl.list_pending():
            if allowed is not None and req.agent_name not in allowed:
                continue
            origin = (req.payload or {}).get("origin") or {}
            items.append(
                {
                    "id": req.id,
                    "agent": req.agent_name,
                    "action": req.action,
                    "description": req.description,
                    "risk": req.risk,
                    "root_source": origin.get("root_source", ""),
                    "call_path": origin.get("call_path", []),
                    "args": (req.payload or {}).get("args"),
                    "created_at": req.created_at.isoformat(),
                    "timeout_seconds": req.timeout_seconds,
                }
            )
        return {"pending": items}

    @app.post("/api/hitl/resolve")
    async def hitl_resolve(body: dict) -> dict:
        """Approve or reject a pending inbox approval. Body:
        ``{"id": str, "approved": bool, "note": str?}``."""
        from olano.security import hitl

        req_id = (body or {}).get("id")
        approved = bool((body or {}).get("approved"))
        note = str((body or {}).get("note") or "")
        if not req_id:
            raise HTTPException(status_code=400, detail="id is required")
        # Approving a gated action is scoped like seeing it: members may only
        # resolve requests belonging to agents they can access (404 otherwise,
        # so foreign approval ids are not disclosed).
        allowed = auth_layer.allowed_agent_names()
        if allowed is not None:
            target = next((r for r in hitl.list_pending() if r.id == req_id), None)
            if target is None or target.agent_name not in allowed:
                raise HTTPException(status_code=404, detail="No such pending approval")
        ok = hitl.resolve(
            req_id,
            approved=approved,
            note=note,
            decided_by=_decider_principal(),
            via="dashboard",
        )
        if not ok:
            raise HTTPException(status_code=404, detail="No such pending approval")
        _api._push_event(
            "approvals_changed", {"resolved": req_id, "pending": len(hitl.list_pending())}
        )
        return {"ok": True}

    @app.get("/api/hitl/history")
    async def hitl_history(agent: str | None = None, limit: int = 200) -> dict:
        """The approval record: every gated action, who decided it and how.

        Read from the append-only ``<home>/audit/approvals.jsonl`` (see
        ``security/hitl.py``), newest first, one row per request. Members see
        rows for the agents they were granted only, like ``/api/hitl/pending``.
        """
        from olano.security import hitl

        allowed = auth_layer.allowed_agent_names()
        rows = hitl.read_audit_log(
            agent=agent,
            agents=set(allowed) if allowed is not None else None,
            limit=max(1, min(int(limit), 2000)),
        )
        return {"history": rows}

    # ── Notifications ─────────────────────────────────────────────────────────
    #
    # Things the operator needs to act on — a connector skipped for a missing
    # credential, a vault still locked, a disk over threshold. The runtime has
    # always known these; before this they only went to the log, so the visible
    # symptom was a bot that quietly stopped replying. Backed by
    # runtime/notifications.py; changes broadcast a `notifications_changed`
    # frame on /api/stream.

    def _visible_notifications(rows: list[dict]) -> list[dict]:
        """Scope agent-attributed rows to what this user may see.

        A notification names an agent, so it leaks the same thing the agent
        endpoints hide: members only get rows for agents they were granted,
        plus every non-agent row (disk, vault — deployment-wide).
        """
        allowed = auth_layer.allowed_agent_names()
        if allowed is None:
            return rows
        return [r for r in rows if not r.get("agent") or r["agent"] in allowed]

    @app.get("/api/notifications")
    async def notifications_list(include_read: bool = True, limit: int = 100) -> dict:
        from olano.runtime import notifications

        rows = _visible_notifications(
            notifications.list_notifications(include_read=include_read, limit=limit)
        )
        unread = [r for r in rows if not r["read"]]
        worst = ("error", "warn", "success", "info")
        level = next((lv for lv in worst if any(r["level"] == lv for r in unread)), None)
        return {
            "notifications": rows,
            "unread": len(unread),
            "total": len(rows),
            "level": level,
        }

    @app.post("/api/notifications/read")
    async def notifications_mark_read(body: dict | None = None) -> dict:
        """Mark notifications read. ``{"ids": [...]}``; omit ids for all."""
        from olano.runtime import notifications

        ids = (body or {}).get("ids")
        if ids is not None and not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="ids must be a list")
        visible = {r["id"] for r in _visible_notifications(notifications.list_notifications())}
        targets = [i for i in ids if i in visible] if ids is not None else sorted(visible)
        return {"ok": True, "marked": notifications.mark_read(targets)}

    @app.delete("/api/notifications/{notification_id}")
    async def notifications_dismiss(notification_id: str) -> dict:
        from olano.runtime import notifications

        visible = {r["id"] for r in _visible_notifications(notifications.list_notifications())}
        if notification_id not in visible:
            raise HTTPException(status_code=404, detail="No such notification")
        return {"ok": True, "dismissed": notifications.dismiss(notification_id)}

    @app.post("/api/notifications/clear")
    async def notifications_clear(body: dict | None = None) -> dict:
        """Clear read notifications, or everything with ``{"all": true}``."""
        from olano.runtime import notifications

        if auth_layer.allowed_agent_names() is not None:
            raise HTTPException(status_code=403, detail="Clearing notifications is an admin action")
        clear_all = bool((body or {}).get("all"))
        return {"ok": True, "cleared": notifications.clear(read_only=not clear_all)}
