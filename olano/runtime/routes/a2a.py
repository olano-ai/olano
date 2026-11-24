"""Agent-to-agent activity feed.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_a2a_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the a2a routes to ``app``."""

    def _visible_a2a_events(
        *,
        kind: str | None = None,
        a: str | None = None,
        b: str | None = None,
        since: str | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """Scan the log tail and apply visibility + query filters, oldest→newest.

        Every filter — including the per-member visibility rule — runs *before
        any* page limit is taken, so a narrow filter still returns a full page
        rather than whatever happened to survive a tail slice.
        """
        from olano.analytics.a2a_log import norm_agent

        allowed = auth_layer.allowed_agent_names()
        wanted_kind = kind if kind in ("peer", "subagent") else None
        participants = {norm_agent(p) for p in (a, b) if p}
        query = (q or "").strip().casefold()

        out: list[dict[str, Any]] = []
        for ev in _api._read_a2a_activity(_api._A2A_SCAN_LIMIT):
            data = _api._a2a_event_data(ev)
            if data is None:
                continue
            if allowed is not None and not auth_layer.a2a_event_visible(data, allowed):
                continue
            if not _api._a2a_matches(
                data, kind=wanted_kind, participants=participants, query=query
            ):
                continue
            ts = _api._a2a_scan_ts(ev)
            if since and ts and ts < since:
                continue
            if before and ts and ts >= before:
                continue
            out.append(ev)
        return out

    @app.get("/api/a2a/activity")
    async def a2a_activity(
        limit: int = 500,
        kind: str | None = None,
        a: str | None = None,
        b: str | None = None,
        since: str | None = None,
        before: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted A2A activity events for dashboard replay.

        Members see an exchange when at least one participant is an agent they
        can access; exchanges strictly between non-granted agents are hidden.

        Optional filters: ``kind`` (``peer``/``subagent``), ``a``/``b``
        (participants — both given means the exchange must involve both),
        ``since``/``before`` (ISO bounds; ``before`` doubles as the load-older
        cursor), and ``q`` (substring over participants, message, and reply).
        The newest ``limit`` matches are returned, oldest→newest.
        """
        limit = max(1, min(int(limit or 500), 2000))
        # The scan reads + JSON-parses up to the log tail (megabytes) per request.
        matches = await asyncio.to_thread(
            _visible_a2a_events, kind=kind, a=a, b=b, since=since, before=before, q=q
        )
        return matches[-limit:]

    @app.get("/api/a2a/threads")
    async def a2a_threads(
        kind: str | None = None,
        since: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """One entry per agent-to-agent conversation, newest activity first.

        This is what the dashboard's thread rail lists. Peer exchanges fold
        both directions into a single conversation; subagent delegations stay
        directional. Same visibility rule as ``/api/a2a/activity``, applied
        before the fold so a member's counts match the transcripts they can
        actually open.
        """
        return await asyncio.to_thread(
            lambda: _api._fold_a2a_threads(_visible_a2a_events(kind=kind, since=since, q=q))
        )

    @app.delete("/api/a2a/activity")
    async def clear_a2a_activity() -> dict[str, str]:
        """Clear persisted A2A activity history (admin-only: the log is a
        deployment-wide store shared by every user and feeds analytics)."""
        auth_layer.assert_admin()
        path = _api._a2a_activity_path()
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "cleared"}
