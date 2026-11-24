"""Analytics overview, journal, reports and thread cost.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from datetime import date, datetime, timezone
from functools import partial
from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_analytics_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the analytics routes to ``app``."""
    # ── Analytics / Insights ───────────────────────────────────────────────────

    @app.get("/api/analytics/overview")
    async def analytics_overview(days: int = 30, hours: Optional[int] = None) -> dict[str, Any]:
        """Cross-agent usage, tool, and A2A rollups for the Insights dashboard.

        Cost/token usage is sourced from the per-call usage ledger — the same
        store for every window, which is what keeps the dashboard's window
        buttons comparable (a shorter window is a subset of a longer one, so it
        can never total more). Tool-call counts and A2A edges come from the
        transcript / A2A logs. All over a trailing ``days``-day window (1-366,
        default 30); passing ``hours`` (1-168) instead selects a rolling
        last-``hours`` window with an hourly series — the dashboard's "1 day"
        view sends ``hours=24``. Checkpoint mining fills only window days the
        ledger predates (``usage.source`` names the stores that fed the payload).

        Fully local by design: every olano-billed call records its charge on
        the box at call time (usage + service ledgers, cognition/backfill
        included), so this payload never fetches from the cloud — its
        ``credits_summary`` (managed model + service credits) is what the
        dashboard headlines. The cloud remains authoritative only for the
        BILLING balance (`/api/olano/credits`, the balance card).
        """
        from olano.analytics import threadcost

        # Members get every rollup scoped to their granted agents (usage, tools,
        # and A2A — the latter keeps exchanges with ≥1 granted participant).
        allowed = auth_layer.allowed_agent_names()
        if hours is not None:
            hours = max(1, min(int(hours), 168))
            return await asyncio.to_thread(
                partial(threadcost.overview, 1, hours=hours, agents=allowed)
            )
        days = max(1, min(int(days or 30), 366))
        return await asyncio.to_thread(partial(threadcost.overview, days, agents=allowed))

    @app.get("/api/analytics/journal")
    async def analytics_journal_list() -> dict[str, Any]:
        """List available journal days (newest first) + today's live entry.

        ``latest_active`` is the most recent day with recorded usage so the UI can
        open on a day that has something to show instead of an empty today.
        """
        from olano.analytics import journal

        today = datetime.now(timezone.utc).date().isoformat()
        # Always (re)build today so the list reflects current activity.
        await asyncio.to_thread(journal.build_journal_day, today)
        days = await asyncio.to_thread(journal.list_journal_days)
        latest_active = await asyncio.to_thread(journal.latest_active_day)
        return {"days": days, "today": today, "latest_active": latest_active}

    @app.get("/api/analytics/journal/{day}")
    async def analytics_journal_day(day: str) -> dict[str, Any]:
        """Return one day's journal entry.

        Always rebuilds the deterministic metrics (cheap, one day's data) so the
        numbers stay fresh and any previously-saved corrupt narrative self-heals;
        a good LLM narrative is preserved across rebuilds.
        """
        from olano.analytics import journal

        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid date (YYYY-MM-DD)") from exc
        entry = await asyncio.to_thread(journal.build_journal_day, day)
        # Members see only their agents' rows; cross-agent aggregates and the
        # fleet-wide narrative are stripped (unfilterable free text).
        return _api._scope_journal_entry(entry, auth_layer.allowed_agent_names())

    @app.post("/api/analytics/journal/{day}/highlights")
    async def analytics_journal_highlights(day: str, model: Optional[str] = None) -> dict[str, Any]:
        """Generate (or regenerate) the LLM highlights paragraph for a day.

        The narrative summarises the day's actual *work* — the conversations
        agents handled, the scheduled tasks and heartbeat checks they ran, the
        agent-to-agent messages they exchanged, and their cortex activity per
        agent — and carries no usage statistics. Admin-only: generation spends
        model budget and the prompt/narrative span every agent and every
        user's conversations.
        """
        from olano.analytics import journal

        auth_layer.assert_admin()
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid date (YYYY-MM-DD)") from exc
        return await journal.generate_highlights(day, model=model)

    @app.get("/api/analytics/highlights/latest")
    async def analytics_highlights_latest() -> dict[str, Any]:
        """The most recent day with a written highlights narrative, for the
        Overview daily-digest panel. Includes ``today`` so the panel's
        "Generate highlights" button knows which day to (re)generate. The
        narrative is composed from the per-agent map to the viewer's scope
        (member → their granted agents, unless the team-transparency flag is on)."""
        from olano.analytics import journal

        data = await asyncio.to_thread(journal.latest_highlights)
        return _api._scope_narrative_payload(data)

    # ── Weekly / monthly rollup reports (over the daily per-agent highlights) ──

    @app.get("/api/analytics/reports/{period}")
    async def reports_list(period: str) -> dict[str, Any]:
        """Available report keys for a period (weekly | monthly), newest first."""
        from olano.analytics import reports

        try:
            period = reports.normalize_period(period)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        keys = await asyncio.to_thread(reports.list_reports, period)
        return {"period": period, "keys": keys}

    @app.get("/api/analytics/reports/{period}/{key}")
    async def reports_get(period: str, key: str) -> dict[str, Any]:
        """One weekly/monthly report, scoped to the viewer's agents."""
        from olano.analytics import reports

        try:
            period = reports.normalize_period(period)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = await asyncio.to_thread(reports.read_report, period, key)
        if data is None:
            raise HTTPException(status_code=404, detail=f"No {period} report '{key}'")
        return _api._scope_narrative_payload(data)

    @app.post("/api/analytics/reports/{period}/generate")
    async def reports_generate(
        period: str, anchor: Optional[str] = None, model: Optional[str] = None
    ) -> dict[str, Any]:
        """Generate (or regenerate) a weekly/monthly rollup — admin-only, since
        it spends model budget and rolls up every agent's work. ``anchor`` is any
        date inside the target period (default: current period)."""
        from olano.analytics import reports

        auth_layer.assert_admin()
        try:
            period = reports.normalize_period(period)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = await reports.generate_report(period, anchor, model=model)
        return _api._scope_narrative_payload(data)

    # ── Analytics / Insights drill-down (per-thread / per-call / per-tool) ──────
    # These read the durable LangGraph checkpoint store (threads.db) to attribute
    # spend down to the individual LLM call and the tools each call invoked —
    # retroactively, with no logging changes. A small derived cache (threadcost.db)
    # keeps agent/thread ranking fast; per-thread drill-down mines one thread live.

    # ``days`` / ``hours`` on the ranking endpoints are the drill-down's
    # time-range selector (neither = all history). They mirror the Overview's
    # params exactly — the dashboard's "1 day" button sends ``hours=24`` to both
    # tabs, so one button means one window everywhere. Rows are filtered by the
    # thread's last-active time, so a long thread's whole lifetime cost lands in
    # whichever window that moment falls into — see
    # ``threadcost.service._since_bound``.

    @app.get("/api/analytics/threadcost/agents")
    async def threadcost_agents(days: int = 0, hours: int = 0) -> dict[str, Any]:
        """Agents ranked by token cost (mined from the checkpoint store).
        Members see only their granted agents (rows and totals)."""
        from olano.analytics import threadcost

        allowed = auth_layer.allowed_agent_names()
        return await asyncio.to_thread(
            partial(
                threadcost.agents_ranked,
                agents_filter=allowed,
                days=_api._window_days(days),
                hours=_api._window_hours(hours),
            )
        )

    @app.get("/api/analytics/threadcost/agents/{name}/threads")
    async def threadcost_agent_threads(
        name: str,
        origin: str = "all",
        limit: int = 50,
        offset: int = 0,
        days: int = 0,
        hours: int = 0,
    ) -> dict[str, Any]:
        """Threads for an agent ranked by cost (agent access enforced)."""
        from olano.analytics import threadcost

        auth_layer.assert_agent_access(name)
        limit = max(1, min(int(limit or 50), 200))
        data = await asyncio.to_thread(
            partial(
                threadcost.threads_ranked,
                name,
                origin=origin,
                limit=limit,
                offset=offset,
                days=_api._window_days(days),
                hours=_api._window_hours(hours),
            )
        )
        data["threads"] = [
            t
            for t in data.get("threads", [])
            if auth_layer.thread_visible_to_current_user(t.get("thread_id", ""))
        ]
        return data

    def _assert_thread_agent_access(thread_id: str) -> None:
        """Defense-in-depth for drill-down endpoints: beyond the per-human
        namespace check, the thread's owning agent must also be granted."""
        allowed = auth_layer.allowed_agent_names()
        if allowed is None:
            return
        from olano.analytics.threadcost import resolver

        agent = resolver.resolve_agent(thread_id, resolver.build_thread_agent_index())
        if agent not in allowed:
            raise HTTPException(status_code=404, detail="Thread not found")

    @app.get("/api/analytics/threadcost/threads/{thread_id}/calls")
    async def threadcost_thread_calls(thread_id: str) -> dict[str, Any]:
        """Per-call breakdown + turn execution tree + per-tool attribution."""
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        _assert_thread_agent_access(thread_id)
        from olano.analytics import threadcost

        return await asyncio.to_thread(threadcost.thread_calls, thread_id)

    @app.get("/api/analytics/threadcost/threads/{thread_id}/calls/{seq}/raw")
    async def threadcost_call_raw(thread_id: str, seq: int, ns: str = "") -> dict[str, Any]:
        """Raw prompt context + response for one call (gated by thread visibility)."""
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        _assert_thread_agent_access(thread_id)
        from olano.analytics import threadcost

        return await asyncio.to_thread(threadcost.call_raw, thread_id, ns, int(seq))

    @app.get("/api/analytics/threadcost/tools")
    async def threadcost_tools(
        agent: Optional[str] = None,
        thread_id: Optional[str] = None,
        days: int = 0,
        hours: int = 0,
    ) -> dict[str, Any]:
        """Tools ranked by the tokens/cost of the calls that invoked them."""
        if thread_id and not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        if agent:
            auth_layer.assert_agent_access(agent)
        from olano.analytics import threadcost

        allowed = auth_layer.allowed_agent_names()
        return await asyncio.to_thread(
            partial(
                threadcost.tools_rollup,
                agent,
                thread_id,
                agents_filter=allowed,
                days=_api._window_days(days),
                hours=_api._window_hours(hours),
            )
        )

    @app.get("/api/analytics/threadcost/origins")
    async def threadcost_origins(days: int = 0, hours: int = 0) -> dict[str, Any]:
        """Spend split by interactive vs. autonomous/scheduled/peer origin.
        Members see the split over their granted agents only."""
        from olano.analytics import threadcost

        allowed = auth_layer.allowed_agent_names()
        return await asyncio.to_thread(
            partial(
                threadcost.origins,
                agents_filter=allowed,
                days=_api._window_days(days),
                hours=_api._window_hours(hours),
            )
        )

    @app.get("/api/analytics/threadcost/diagnostics")
    async def threadcost_diagnostics() -> dict[str, Any]:
        """Insights pipeline health: active backend, store reachability, a live
        checkpoint-read probe (with the exact error on failure), cache/ledger
        counts. First stop when the Insights page shows zeros. Admin-only —
        the probe names arbitrary threads/agents and the report is operator
        infrastructure detail."""
        auth_layer.assert_admin()
        from olano.analytics import threadcost

        return await asyncio.to_thread(threadcost.diagnostics)
