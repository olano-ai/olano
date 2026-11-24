"""Per-agent visibility scoping for non-admin members (multi-user mode).

Covers the surfaces the dashboard pages read beyond the agent roster:
Overview/Insights analytics, the work journal + highlights, A2A activity
(≥1-granted-participant rule), HITL approvals, Pulse/Cortex, scheduler
by-id operations, per-agent config/connection reads, workspace reads, the
public /api/status roster, and the SSE frame filter.

Follows the client conventions of test_agent_access.py: the admin bootstraps
multi-user mode, members are created via the admin API, and admin-ness is
simply which TestClient carries which session cookie.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.analytics.usage_ledger import record_usage_event
from olano.config.home import get_olano_paths
from olano.runtime import auth as auth_layer
from olano.runtime.api import create_api
from olano.security.request_context import push_request_context


def _runner(model="anthropic:claude-haiku-4-5-20251001", reply="ok"):
    r = MagicMock()
    r.config = MagicMock(model=model)
    r.ainvoke = AsyncMock(return_value=reply)
    return r


@pytest.fixture
def runtime_mock():
    runtime = MagicMock()
    runtime._runners = {
        "merli": _runner(reply="hi from merli"),
        "scout": _runner(reply="hi from scout"),
    }
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    runtime.scheduler = None  # /api/status: len(list_jobs()) must not hit a Mock
    runtime._self_learning = []
    return runtime


@pytest.fixture
def app(runtime_mock):
    return create_api(runtime_mock)


def _admin(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"})
    return c


def _make_member(app, admin: TestClient, email="bob@x.com") -> tuple[TestClient, str]:
    uid = admin.post(
        "/api/admin/users", json={"email": email, "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": email, "password": "pw"})
    return member, uid


def _member_with_merli(app, admin: TestClient, email="bob@x.com") -> tuple[TestClient, str]:
    member, uid = _make_member(app, admin, email=email)
    assert admin.post("/api/agents/merli/access", json={"user_id": uid}).status_code == 200
    return member, uid


def _seed_agent_config(name: str, extra: dict | None = None) -> None:
    """Write a minimal agent.json5 into the isolated home's agents dir."""
    d = get_olano_paths().agents_dir / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "tools": ["web_search"], **(extra or {})}
    (d / "agent.json5").write_text(json.dumps(data), encoding="utf-8")


def _seed_a2a(events: list[dict]) -> None:
    logs = get_olano_paths().logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "agent_message", "data": data, "ts": "2099-01-01T00:00:00+00:00"})
        for data in events
    ]
    (logs / "a2a_activity.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── /api/status (public roster) ──────────────────────────────────────────────


def test_status_roster_scoped_by_viewer(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    assert set(admin.get("/api/status").json()["agents"]) >= {"merli", "scout"}
    assert member.get("/api/status").json()["agents"] == ["merli"]
    # Unauthenticated probe: alive, but no roster.
    anon = TestClient(app)
    body = anon.get("/api/status").json()
    assert body["status"] == "running"
    assert body["agents"] == []


# ── A2A activity (≥1 granted participant) ────────────────────────────────────


def test_a2a_activity_filtered_by_participants(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_a2a(
        [
            {"kind": "peer", "phase": "send", "from": "merli", "to": "scout", "message": "m1"},
            {"kind": "peer", "phase": "send", "from": "scout", "to": "merli", "message": "m2"},
            {"kind": "peer", "phase": "send", "from": "scout", "to": "ghost", "message": "m3"},
        ]
    )

    assert len(admin.get("/api/a2a/activity").json()) == 3
    member_msgs = {e["data"]["message"] for e in member.get("/api/a2a/activity").json()}
    # Conversations touching merli are visible — even with a non-granted peer —
    # but the scout↔ghost exchange (no granted participant) is not.
    assert member_msgs == {"m1", "m2"}


def test_a2a_threads_scoped_by_participants(app):
    """The rail rollup folds *after* the visibility filter, so a member's thread
    list and message counts match the transcripts they can actually open."""
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_a2a(
        [
            {"kind": "peer", "phase": "send", "from": "merli", "to": "scout", "message": "m1"},
            # opposite direction — must fold into the SAME conversation row
            {"kind": "peer", "phase": "send", "from": "scout", "to": "merli", "message": "m2"},
            {"kind": "peer", "phase": "send", "from": "scout", "to": "ghost", "message": "m3"},
        ]
    )

    admin_rows = {t["key"]: t for t in admin.get("/api/a2a/threads").json()}
    assert set(admin_rows) == {"peer:merli::scout", "peer:ghost::scout"}
    assert admin_rows["peer:merli::scout"]["messages"] == 2

    member_rows = member.get("/api/a2a/threads").json()
    assert [t["key"] for t in member_rows] == ["peer:merli::scout"]
    assert member_rows[0]["messages"] == 2


def test_a2a_clear_is_admin_only(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_a2a([{"kind": "peer", "phase": "send", "from": "merli", "to": "scout", "message": "x"}])

    assert member.delete("/api/a2a/activity").status_code == 403
    assert len(admin.get("/api/a2a/activity").json()) == 1  # survived the attempt
    assert admin.delete("/api/a2a/activity").status_code == 200
    assert admin.get("/api/a2a/activity").json() == []


# ── Analytics overview + journal + highlights ────────────────────────────────


def test_analytics_overview_scoped_to_granted_agents(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    record_usage_event("merli", "anthropic:m", 100, 10, 0.5)
    record_usage_event("scout", "openai:o", 200, 20, 2.0)
    _seed_a2a([{"kind": "peer", "phase": "send", "from": "scout", "to": "ghost", "message": "s"}])

    full = admin.get("/api/analytics/overview").json()
    assert {r["agent"] for r in full["usage"]["by_agent"]} == {"merli", "scout"}
    assert full["a2a"]["totals"]["messages"] == 1

    scoped = member.get("/api/analytics/overview").json()
    assert {r["agent"] for r in scoped["usage"]["by_agent"]} == {"merli"}
    # Totals, models and the daily series are recomputed from the member's
    # agents only — no cross-agent spend leaks through the aggregates.
    assert scoped["usage"]["totals"]["usd"] == pytest.approx(0.5)
    assert {r["model"] for r in scoped["usage"]["by_model"]} == {"anthropic:m"}
    assert sum(d["usd"] for d in scoped["usage"]["daily"]) == pytest.approx(0.5)
    # The scout→ghost exchange has no granted participant.
    assert scoped["a2a"]["totals"]["messages"] == 0
    assert scoped["a2a"]["edges"] == []
    assert scoped["a2a"]["recent"] == []


def test_overview_stats_are_fully_local_and_member_scoped(app):
    """The overview never fetches from the cloud (stats are box-authoritative);
    the combined credits_summary respects member scoping like every rollup."""
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    record_usage_event("merli", "olano:pro", 100, 10, 0.0, credits=2.0)
    record_usage_event("scout", "olano:turbo", 100, 10, 0.0, credits=9.0)

    full = admin.get("/api/analytics/overview").json()
    assert "cloud_credits" not in full
    assert full["credits_summary"]["model"] == pytest.approx(11.0)

    scoped = member.get("/api/analytics/overview").json()
    assert "cloud_credits" not in scoped
    # Only merli's managed spend — scout's never leaks into the member view.
    assert scoped["credits_summary"]["model"] == pytest.approx(2.0)
    tiers = {r["tier"] for r in scoped["usage"]["by_tier"]}
    assert tiers == {"pro"}


def test_journal_day_scoped_for_members(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    record_usage_event("merli", "anthropic:m", 100, 10, 0.5)
    record_usage_event("scout", "openai:o", 200, 20, 2.0)
    today = admin.get("/api/analytics/journal").json()["today"]

    full = admin.get(f"/api/analytics/journal/{today}").json()
    assert {r["agent"] for r in full["by_agent"]} == {"merli", "scout"}

    scoped = member.get(f"/api/analytics/journal/{today}").json()
    assert {r["agent"] for r in scoped["by_agent"]} == {"merli"}
    assert scoped["metrics"]["active_agents"] == ["merli"]
    assert scoped["metrics"]["usd"] == pytest.approx(0.5)
    assert scoped["by_model"] == []  # cross-agent split is unattributable
    assert scoped["narrative"] is None
    assert scoped["events"] == {"cognition_actions": [], "cortex": []}


def test_highlights_generation_admin_only(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    today = admin.get("/api/analytics/journal").json()["today"]

    # Generation spends model budget over the whole fleet → admin-only.
    assert member.post(f"/api/analytics/journal/{today}/highlights").status_code == 403


def _seed_journal_highlights(day: str, sections: dict[str, str], lead: str = "The day.") -> None:
    """Write a journal entry for ``day`` carrying a per-agent highlights map."""
    from olano.analytics import journal

    entry = journal.build_journal_day(day, save=True)
    text = f"LEAD: {lead}\n" + "".join(f"[[agent:{a}]]\n{t}\n" for a, t in sections.items())
    journal._apply_highlights(entry, text, "test:model")
    journal.journal_path(day).write_text(json.dumps(entry), encoding="utf-8")


def test_daily_narrative_scoped_to_member_agents(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    today = admin.get("/api/analytics/journal").json()["today"]
    _seed_journal_highlights(today, {"merli": "Handled billing.", "scout": "Ran a scan."})

    # Admin sees the whole fleet narrative (both agents); the internal per-agent
    # map is never exposed in the payload.
    full = admin.get(f"/api/analytics/journal/{today}").json()
    assert "merli" in full["narrative"] and "scout" in full["narrative"]
    assert "agent_highlights" not in full

    # Member sees ONLY their granted agent's bullet, composed from the map.
    scoped = member.get(f"/api/analytics/journal/{today}").json()
    assert "merli" in scoped["narrative"] and "scout" not in scoped["narrative"]
    assert "Handled billing." in scoped["narrative"]

    # The Overview daily-digest endpoint scopes the same way.
    latest = member.get("/api/analytics/highlights/latest").json()
    assert latest["narrative"] and "scout" not in latest["narrative"]


def test_highlights_all_agents_flag_shows_full_to_members(app):
    from olano.config.home import update_home_config

    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    today = admin.get("/api/analytics/journal").json()["today"]
    _seed_journal_highlights(today, {"merli": "Handled billing.", "scout": "Ran a scan."})

    # Team-transparency mode: members now see every agent's story.
    update_home_config({"highlights_all_agents": True})
    scoped = member.get(f"/api/analytics/journal/{today}").json()
    assert "merli" in scoped["narrative"] and "scout" in scoped["narrative"]
    # …but per-agent spend metrics stay scoped (financial, not the digest flag).
    assert {r["agent"] for r in scoped["by_agent"]} <= {"merli"}


def _seed_report(period: str, key: str, sections: dict[str, str]) -> None:
    from datetime import date

    from olano.analytics import journal, reports

    entry = reports.report_scaffold(period, key, date(2026, 7, 20), date(2026, 7, 26))
    text = "LEAD: Week.\n" + "".join(f"[[agent:{a}]]\n{t}\n" for a, t in sections.items())
    journal._apply_highlights(entry, text, "test:model")
    reports.report_path(period, key).write_text(json.dumps(entry), encoding="utf-8")


def test_reports_scoped_and_generation_admin_only(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_report("weekly", "2026-W30", {"merli": "Billing all week.", "scout": "Scans all week."})

    # Listing + reading are member-reachable; the narrative is scoped.
    assert "2026-W30" in member.get("/api/analytics/reports/weekly").json()["keys"]
    rep = member.get("/api/analytics/reports/weekly/2026-W30").json()
    assert "merli" in rep["narrative"] and "scout" not in rep["narrative"]
    assert admin.get("/api/analytics/reports/weekly/2026-W30").json()["narrative"].count("**") >= 4

    # Generation (paid, fleet-wide) is admin-only; bad period → 400.
    assert member.post("/api/analytics/reports/weekly/generate").status_code == 403
    assert admin.get("/api/analytics/reports/quarterly").status_code == 400


# ── Thread-cost drill-down ────────────────────────────────────────────────────


def test_threadcost_endpoints_scoped(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    assert member.get("/api/analytics/threadcost/agents/scout/threads").status_code == 403
    assert member.get("/api/analytics/threadcost/agents/merli/threads").status_code == 200
    assert member.get("/api/analytics/threadcost/tools?agent=scout").status_code == 403
    assert member.get("/api/analytics/threadcost/diagnostics").status_code == 403
    assert admin.get("/api/analytics/threadcost/diagnostics").status_code == 200
    # The empty-store rollups still answer for members (scoped to their set).
    assert member.get("/api/analytics/threadcost/agents").status_code == 200
    assert member.get("/api/analytics/threadcost/origins").status_code == 200


# ── HITL approvals ────────────────────────────────────────────────────────────


def test_hitl_pending_and_resolve_scoped(app):
    from olano.security import hitl

    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    hitl._pending["a1"] = hitl.ApprovalRequest(
        id="a1", agent_name="merli", action="send_email", description="d"
    )
    hitl._pending["a2"] = hitl.ApprovalRequest(
        id="a2", agent_name="scout", action="run_shell", description="d"
    )

    assert {p["id"] for p in admin.get("/api/hitl/pending").json()["pending"]} == {"a1", "a2"}
    assert {p["id"] for p in member.get("/api/hitl/pending").json()["pending"]} == {"a1"}

    # A member cannot resolve (approve!) another agent's gated action…
    assert member.post("/api/hitl/resolve", json={"id": "a2", "approved": True}).status_code == 404
    assert hitl._pending["a2"].approved is None  # untouched
    # …but can resolve their own agent's. (The entry itself is popped by the
    # awaiting request_approval coroutine, absent in this test.)
    assert member.post("/api/hitl/resolve", json={"id": "a1", "approved": False}).status_code == 200
    assert hitl._pending["a1"].approved is False
    assert hitl._pending["a1"].resolved_at is not None
    # The record names the member who decided, and through the dashboard.
    assert hitl._pending["a1"].decided_via == "dashboard"
    assert hitl._pending["a1"].decided_by.startswith("human-")
    assert hitl._pending["a1"].decided_by != "human-owner"


def test_hitl_history_scoped(app):
    from olano.security import hitl

    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    hitl.record_decision(
        agent_name="merli", action="gmail_send", approved=True, decided_by="whatsapp:1", via="v"
    )
    hitl.record_decision(
        agent_name="scout", action="run_shell", approved=False, decided_by="whatsapp:2", via="v"
    )

    assert [r["agent"] for r in admin.get("/api/hitl/history").json()["history"]] == [
        "scout",
        "merli",
    ]
    assert [r["agent"] for r in member.get("/api/hitl/history").json()["history"]] == ["merli"]


# ── Pulse / Cortex / self-improvement ────────────────────────────────────────


def test_pulse_and_cortex_require_agent_grant(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    assert member.get("/api/pulse?agent=scout").status_code == 403
    assert (
        member.post("/api/pulse/approve", json={"item_id": "x", "agent": "scout"}).status_code
        == 403
    )
    assert member.post("/api/pulse/generate", json={"agent": "scout"}).status_code == 403
    assert member.get("/api/cortex/items?agent=scout").status_code == 403
    assert member.get("/api/cortex/items/i1?agent=scout").status_code == 403
    assert member.post("/api/cortex/items/i1/discard", json={"agent": "scout"}).status_code == 403
    assert member.post("/api/self-improve", json={"agent": "scout"}).status_code == 403


def test_self_learning_status_filtered(app, runtime_mock):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    def _svc(name: str):
        return SimpleNamespace(config=SimpleNamespace(name=name), status=lambda: {"agent": name})

    runtime_mock._self_learning = [_svc("merli"), _svc("scout")]

    admin_agents = {s["agent"] for s in admin.get("/api/self-learning/status").json()["services"]}
    assert admin_agents == {"merli", "scout"}
    member_body = member.get("/api/self-learning/status").json()
    assert {s["agent"] for s in member_body["services"]} == {"merli"}
    assert member_body["count"] == 1
    # Explicit non-granted agent → denied outright.
    assert member.get("/api/self-learning/status?agent=scout").status_code == 403
    assert (
        member.post("/api/self-learning/reset-failures", json={"agent": "scout"}).status_code == 403
    )


# ── Scheduler by-id operations ────────────────────────────────────────────────


def test_scheduler_create_requires_grant(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    ok = member.post(
        "/api/scheduler/tasks", json={"name": "t", "prompt": "p", "agent_name": "merli"}
    )
    assert ok.status_code == 200
    denied = member.post(
        "/api/scheduler/tasks", json={"name": "t2", "prompt": "p", "agent_name": "scout"}
    )
    assert denied.status_code == 403
    # Members cannot create agent-less tasks either (they'd run unattributed).
    assert (
        member.post("/api/scheduler/tasks", json={"name": "t3", "prompt": "p"}).status_code == 403
    )
    # Admin keeps the no-agent form.
    assert admin.post("/api/scheduler/tasks", json={"name": "t4", "prompt": "p"}).status_code == 200


def test_scheduler_by_id_scoped_to_task_agent(app):
    from olano.scheduler.db import SchedulerDB

    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    db = SchedulerDB()
    merli_task = db.create_task(name="m", prompt="p", agent_name="merli")
    scout_task = db.create_task(name="s", prompt="p", agent_name="scout")

    # Reads and mutations 404 (not 403) on non-granted tasks — ids are guessable
    # for config jobs, so existence must not be disclosed.
    assert member.get(f"/api/scheduler/history/{scout_task}").status_code == 404
    assert member.put(f"/api/scheduler/tasks/{scout_task}", json={"name": "x"}).status_code == 404
    assert member.delete(f"/api/scheduler/tasks/{scout_task}").status_code == 404
    assert member.post(f"/api/scheduler/tasks/{scout_task}/run").status_code == 404
    assert db.get_task(scout_task) is not None  # survived

    assert member.get(f"/api/scheduler/history/{merli_task}").status_code == 200
    assert member.put(f"/api/scheduler/tasks/{merli_task}", json={"name": "x"}).status_code == 200
    assert member.delete(f"/api/scheduler/tasks/{merli_task}").status_code == 200

    # Config-schedule jobs resolve their agent from the cfg:<agent>:<idx> id.
    _seed_agent_config("scout", {"schedule": [{"name": "j", "message": "m", "cron": "0 * * * *"}]})
    assert member.get("/api/scheduler/history/cfg:scout:0").status_code == 404
    assert member.put("/api/scheduler/tasks/cfg:scout:0", json={"name": "x"}).status_code == 404
    assert admin.get("/api/scheduler/history/cfg:scout:0").status_code == 200


# ── Per-agent config / connection reads ──────────────────────────────────────


def test_agent_config_reads_scoped(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_agent_config("merli", {"env": {"SECRET_KEY": "v"}})
    _seed_agent_config("scout", {"env": {"SECRET_KEY": "v"}})

    assert member.get("/api/agents/scout").status_code == 403
    assert member.get("/api/agents/scout/org").status_code == 403
    assert member.get("/api/agents/scout/context").status_code == 403
    assert member.get("/api/agents/scout/webhooks").status_code == 403
    assert member.get("/api/agents/scout/webhooks/w1/logs").status_code == 403
    assert member.get("/api/agents/scout/connectors/health").status_code == 403
    assert member.get("/api/agents/scout/mcp/status").status_code == 403
    assert member.get("/api/agents/scout/mcp/health").status_code == 403
    # Writes too: the per-tool switches go through this one, and it is the only
    # MCP mutation carrying the per-agent check the reads have.
    assert (
        member.patch(
            "/api/agents/scout/mcp/tools?url=https://x.example/mcp",
            json={"disabled_tools": []},
        ).status_code
        == 403
    )
    assert member.get("/api/agents/scout/threads").status_code == 403
    assert member.get("/api/catalog/skills?agent=scout").status_code == 403
    # The skill viewer reads files out of a non-granted agent's skill dirs —
    # including its per-agent workspace — so it carries the same check as the
    # listing that feeds it.
    for route in ("detail", "file", "file-raw"):
        assert (
            member.get(
                f"/api/catalog/skills/{route}",
                params={"agent": "scout", "location": "/x/SKILL.md", "path": "SKILL.md"},
            ).status_code
            == 403
        )

    # Granted agent: readable, but the credential overlay is admin-only.
    mine = member.get("/api/agents/merli")
    assert mine.status_code == 200
    assert "env" not in mine.json()
    assert "env" in admin.get("/api/agents/merli").json()

    # Raw config (may embed secrets verbatim) needs admin-or-owner.
    assert member.get("/api/agents/merli/config/raw").status_code == 403
    assert admin.get("/api/agents/merli/config/raw").status_code == 200


def test_catalog_tools_usage_filtered(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_agent_config("merli")
    _seed_agent_config("scout")

    admin_agents = {
        e["agent"]
        for rows in admin.get("/api/catalog/tools/usage").json()["usage"].values()
        for e in rows
    }
    assert {"merli", "scout"} <= admin_agents
    member_agents = {
        e["agent"]
        for rows in member.get("/api/catalog/tools/usage").json()["usage"].values()
        for e in rows
    }
    assert member_agents == {"merli"}


def test_workspace_reads_scoped(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    _seed_agent_config("merli")  # a resolvable read target
    _seed_agent_config("scout")

    assert member.get("/api/files?agent=scout").status_code == 403
    assert member.get("/api/files/notes.md?agent=scout").status_code == 403
    assert member.get("/api/memory?agent=scout").status_code == 403
    assert member.get("/api/memory?agent=merli").status_code == 200
    assert admin.get("/api/memory?agent=scout").status_code == 200


def test_workspace_read_404s_on_deleted_but_still_granted_agent(app):
    """A grant that outlived its agent must not fall back to the default
    agent's workspace: the requested (vanished) agent 404s instead."""
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    # Grant an agent that has no config on disk (simulates a deleted agent
    # whose access row survived).
    admin.post("/api/agents/ghost/access", json={"user_id": uid})

    # Access passes on the stale grant, but there's no ghost workspace to read
    # and we must not serve the default agent's — so 404, not the default files.
    assert member.get("/api/files?agent=ghost").status_code == 404
    assert member.get("/api/memory?agent=ghost").status_code == 404


def test_delete_agent_revokes_access_grants(app):
    from olano.users.store import get_user_store

    admin = _admin(app)
    _member, uid = _member_with_merli(app, admin)
    _seed_agent_config("merli")
    assert get_user_store().db.has_access(uid, "merli") is True

    assert admin.delete("/api/agents/merli").status_code == 200
    # The grant is purged so it can't outlive the agent (default-fallback leak).
    assert get_user_store().db.has_access(uid, "merli") is False


def test_connection_endpoints_scoped(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    assert member.get("/api/auth/status?agent=scout").status_code == 403
    mine = member.get("/api/auth/status?agent=merli")
    assert mine.status_code == 200
    assert all("token_path" not in s for s in mine.json().values() if isinstance(s, dict))
    assert "token_path" in admin.get("/api/auth/status?agent=merli").json()["google"]

    # Token mutations need admin-or-owner on the agent (plain member: denied).
    assert (
        member.patch("/api/auth/google/share", json={"agent": "merli", "shared": True}).status_code
        == 403
    )
    assert member.delete("/api/auth/google?agent=merli").status_code == 403
    assert member.delete("/api/auth/google").status_code == 403  # system scope: admin only
    assert member.post("/api/auth/google/start", json={"agent": "merli"}).status_code == 403

    # An owner-role member may manage their own agent's connections.
    owner, owner_id = _make_member(app, admin, email="carol@x.com")
    admin.post("/api/agents/merli/access", json={"user_id": owner_id, "role": "owner"})
    assert (
        owner.patch("/api/auth/google/share", json={"agent": "merli", "shared": True}).status_code
        == 200
    )
    assert owner.delete("/api/auth/google?agent=merli").status_code == 200


def test_connector_setup_mutations_scoped(app):
    admin = _admin(app)
    member, uid = _member_with_merli(app, admin)
    _seed_agent_config("scout")

    # Agent-scoped connector mutations need admin-or-owner of that agent.
    assert member.post("/api/connectors/whatsapp/reset?agent=scout").status_code == 403
    # Deployment-level Signal provisioning is admin-only.
    assert (
        member.post("/api/connectors/signal/register", json={"account": "+15550001"}).status_code
        == 403
    )
    assert (
        member.post(
            "/api/connectors/signal/verify", json={"account": "+15550001", "code": "1"}
        ).status_code
        == 403
    )
    assert (
        member.post(
            "/api/connectors/signal/profile",
            json={"account": "+1", "given_name": "x", "agent": "scout"},
        ).status_code
        == 403
    )

    # An owner of the agent may drive its connector setup.
    owner, owner_id = _make_member(app, admin, email="dave@x.com")
    admin.post("/api/agents/scout/access", json={"user_id": owner_id, "role": "owner"})
    # reset reaches the handler (may 4xx on missing bridge, but not the 403 gate).
    assert owner.post("/api/connectors/whatsapp/reset?agent=scout").status_code != 403


def test_admin_only_operator_surfaces(app):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)

    assert member.get("/api/logs").status_code == 403
    assert member.get("/api/diagnostics/memory").status_code == 403
    assert member.post("/api/diagnostics/memory/monitor", json={"action": "on"}).status_code == 403
    assert admin.get("/api/logs").status_code == 200


# ── SSE frame filter + thread visibility (unit level) ────────────────────────


def test_event_visible_to_agents_rules():
    allowed = {"merli"}
    vis = auth_layer.event_visible_to_agents

    # No restriction (admin / single-user).
    assert vis({"type": "log", "data": "line"}, None) is True
    # Raw log lines never reach members.
    assert vis({"type": "log", "data": "line"}, allowed) is False
    # A2A frames: at least one granted participant.
    assert vis({"type": "agent_message", "data": {"from": "scout", "to": "merli"}}, allowed) is True
    assert vis({"type": "agent_message", "data": {"from": "merli", "to": "ghost"}}, allowed) is True
    assert (
        vis({"type": "agent_message", "data": {"from": "scout", "to": "ghost"}}, allowed) is False
    )
    # Agent-attributed frames must name a granted agent.
    assert vis({"type": "thread_message", "data": {"agent": "merli"}}, allowed) is True
    assert vis({"type": "chat_response", "data": {"agent": "scout"}}, allowed) is False
    assert vis({"type": "agents_update", "data": {"name": "scout"}}, allowed) is False
    # Unattributed frames (refresh signals) pass — their refetches are filtered.
    assert (
        vis({"type": "scheduler_update", "data": {"action": "created", "id": "t"}}, allowed) is True
    )
    assert vis({"type": "agents_update", "data": {"name": None}}, allowed) is True
    # …but a scheduler_update / file frame whose agent was stamped by _push_event
    # (config-job id → agent name; default-workspace write → default agent) is
    # scoped like any other attributed frame.
    assert (
        vis({"type": "scheduler_update", "data": {"id": "cfg:scout:0", "agent": "scout"}}, allowed)
        is False
    )
    assert (
        vis({"type": "scheduler_update", "data": {"id": "cfg:merli:0", "agent": "merli"}}, allowed)
        is True
    )
    assert (
        vis({"type": "file_updated", "data": {"agent": "scout", "path": "secret.md"}}, allowed)
        is False
    )


def test_push_event_attributes_scheduler_and_file_frames(app, runtime_mock):
    """_push_event stamps the owning agent onto scheduler_update (from a cfg:
    id) and file/memory frames (null agent → default) so the SSE filter can
    scope them — otherwise they'd leak a non-granted agent's name/paths."""
    from olano.runtime import api as api_mod

    captured: list[dict] = []
    q = type("Q", (), {"put_nowait": lambda self, e: captured.append(e)})()
    api_mod._subscribers.append(q)
    try:
        api_mod._push_event("scheduler_update", {"action": "updated", "id": "cfg:scout:3"})
        api_mod._push_event("file_updated", {"agent": None, "path": "p.md", "size": 1})
    finally:
        api_mod._subscribers.remove(q)

    by_type = {e["type"]: e["data"] for e in captured}
    assert by_type["scheduler_update"]["agent"] == "scout"
    # Null agent resolves to the deployment default agent (not left unattributed).
    assert by_type["file_updated"]["agent"] == api_mod._default_agent_name()


_EVERY_THREAD_SHAPE = (
    "3f6a1b2c-0000-4000-8000-000000000000",  # a dashboard chat
    "u:someone-else:t1",  # another human's, under the retired namespace
    "merli::u:someone-else:t1",  # …as the Insights drill-down scopes it
    "acp::merli::scout",  # an agent-to-agent pair thread
    "scheduled_merli_1a2b3c4d@20260101T000000-ab12",  # a scheduled run
    "heartbeat_merli@20260101T000000-ab12",
    "5550001234",  # a Telegram chat
    "u:someone-else:group-3-merli",  # a group another human created
)


def _member_ctx(uid: str) -> dict:
    return dict(
        source="api",
        channel="api",
        user_id=uid,
        session_id="s",
        authenticated=True,
        is_owner=False,
        is_admin=False,
        is_group=False,
    )


def test_thread_visibility_follows_agent_access(app):
    """A thread of a reachable agent is visible to every authenticated human —
    in every id shape the runtime mints — and to nobody anonymous. The agent
    grant (`assert_agent_access`) is the gate, and it is checked separately at
    each endpoint; nothing about the thread id decides."""
    admin = _admin(app)
    _member, uid = _member_with_merli(app, admin)
    ctx = _member_ctx(uid)
    with push_request_context(**ctx):
        assert auth_layer.thread_visibility_prefix() is None
        for tid in (*_EVERY_THREAD_SHAPE, f"u:{uid}:t1"):
            assert auth_layer.thread_visible_to_current_user(tid) is True, tid
    with push_request_context(**{**ctx, "user_id": "anonymous", "authenticated": False}):
        assert auth_layer.thread_visibility_prefix() == ""
        for tid in (*_EVERY_THREAD_SHAPE, f"u:{uid}:t1"):
            assert auth_layer.thread_visible_to_current_user(tid) is False, tid


def test_stream_thread_snapshot_matches_the_endpoint_rule(app):
    """`/api/stream` snapshots `thread_visibility_prefix()` at connect time and
    applies `event_visible_to_thread` per frame. The snapshot must agree,
    thread for thread, with `thread_visible_to_current_user` — the rule the
    thread endpoints apply — or a live frame could show what a GET hides (or
    hide what a GET shows)."""
    admin = _admin(app)
    _member, uid = _member_with_merli(app, admin)
    ctx = _member_ctx(uid)
    anonymous = {**ctx, "user_id": "anonymous", "authenticated": False}
    for who in (ctx, anonymous):
        with push_request_context(**who):
            prefix = auth_layer.thread_visibility_prefix()
            for tid in (*_EVERY_THREAD_SHAPE, f"u:{uid}:t1"):
                frame = {"type": "thread_updated", "data": {"agent": "merli", "thread_id": tid}}
                assert auth_layer.event_visible_to_thread(frame, prefix) == (
                    auth_layer.thread_visible_to_current_user(tid)
                ), tid


# ── continuing any thread from the list ──────────────────────────────────────


def test_namespace_thread_id_is_identity_for_every_caller(app):
    """The id the client sends is the id the turn runs on — member, admin or
    the single-user owner — and a missing id mints a plain UUID. The retired
    `u:<uid>:` prefix is never added: on a reopened id it was the fork that
    made an agent 'forget' the run on screen."""
    admin = _admin(app)
    _member, uid = _member_with_merli(app, admin)
    admin_id = admin.get("/api/auth/me").json()["user"]["id"]
    for user_id, is_admin in ((uid, False), (admin_id, True), ("owner", True)):
        ctx = {**_member_ctx(user_id), "is_owner": is_admin, "is_admin": is_admin}
        with push_request_context(**ctx):
            for tid in (*_EVERY_THREAD_SHAPE, f"u:{uid}:mine"):
                assert auth_layer.namespace_thread_id(tid) == tid, (user_id, tid)
            minted = auth_layer.namespace_thread_id(None)
            assert not minted.startswith("u:")
            uuid.UUID(minted)


@pytest.mark.parametrize("thread_id", _EVERY_THREAD_SHAPE)
def test_chat_continues_any_thread_for_member_and_admin(app, runtime_mock, thread_id):
    """A reply typed into a thread opened from the list runs on THAT thread.

    The dashboard used to prepend `u:<uid>:` to every id it ran a turn on, so a
    human replying inside a scheduled run's thread was forked onto a fresh id
    with none of the run's context — the agent then "could not remember" what
    was on screen. Now the id the client sends is the id the turn runs on, for
    members and admins alike."""
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    runner = runtime_mock._runners["merli"]
    for client in (member, admin):
        runner.ainvoke.reset_mock()
        res = client.post(
            "/api/chat",
            json={"agent": "merli", "message": "what did you just do?", "thread_id": thread_id},
        )
        assert res.status_code == 200, res.text
        assert res.json()["thread_id"] == thread_id
        assert runner.ainvoke.await_args.kwargs["thread_id"] == thread_id


def test_chat_new_conversation_is_a_bare_uuid(app, runtime_mock):
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    for client in (member, admin):
        res = client.post("/api/chat", json={"agent": "merli", "message": "hi"})
        assert res.status_code == 200, res.text
        tid = res.json()["thread_id"]
        assert not tid.startswith("u:")
        uuid.UUID(tid)


def test_chat_stream_keeps_the_thread_id_and_carries_the_humans_name(
    app, runtime_mock, monkeypatch
):
    """In production the SSE body runs after the auth middleware has exited, so
    it re-binds a turn context of its own. Two things must hold there: the
    `start` frame names the id the client sent (no namespaced fork), and the
    human's display name rides in that context's `metadata` so
    `runner._append_session` stamps `sender` on the row — a thread everyone
    with access can read has to say who wrote what. Identity itself
    (`user_id` / `is_admin`) is deliberately not re-bound.

    The TestClient consumes the body while the middleware is still active, so
    the generator's context would inherit the name either way; the assertion is
    therefore on the arguments the generator itself binds, not on what it
    happens to observe."""
    from olano.runtime import api as api_mod

    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    runner = runtime_mock._runners["merli"]
    tid = "scheduled_merli_1a2b3c4d@20260903T010019-0001ab12"
    seen: dict = {}

    real_push = api_mod.push_request_context

    def recording_push(**kwargs):
        # The generator's own binding is the one keyed on the turn's thread id.
        if kwargs.get("source") == "api" and kwargs.get("session_id") == tid:
            seen["turn_ctx"] = kwargs
        return real_push(**kwargs)

    monkeypatch.setattr(api_mod, "push_request_context", recording_push)

    def fake_events(payload, **kwargs):
        seen["thread_id"] = kwargs.get("thread_id")

        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events
    res = member.post(
        "/api/chat/stream", json={"agent": "merli", "message": "and then?", "thread_id": tid}
    )
    assert res.status_code == 200, res.text
    first = next(line for line in res.text.splitlines() if line.startswith("data: "))
    assert json.loads(first[len("data: ") :])["thread_id"] == tid
    assert seen["thread_id"] == tid
    turn_ctx = seen["turn_ctx"]
    assert turn_ctx["metadata"]["name"] == "Bob"
    for identity_field in ("user_id", "is_admin", "is_owner", "authenticated"):
        assert identity_field not in turn_ctx, identity_field


def test_only_agent_admins_delete_threads(app):
    """Every thread is visible to everyone with access, so a delete erases a
    conversation — and its checkpoint — for all of them. That stays with admins
    and the agent's owner; renaming stays open to anyone with access."""
    admin = _admin(app)
    member, _uid = _member_with_merli(app, admin)
    sessions = get_olano_paths().sessions_dir / "merli"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "scheduled_merli_1a2b3c4d@20260101T000000-ab12"
    path = sessions / f"{tid}.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "run", "ts": "2026-01-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    res = member.patch(f"/api/agents/merli/threads/{tid}", json={"name": "Morning report"})
    assert res.status_code == 200, res.text
    assert member.delete(f"/api/agents/merli/threads/{tid}").status_code == 403
    assert path.exists()
    assert admin.delete(f"/api/agents/merli/threads/{tid}").status_code == 200
    assert not path.exists()


def test_file_search_scoped(app):
    """The cross-workspace name search only ever walks workspaces the caller may
    read: a member's unscoped search covers their granted agents and nothing
    else, a named agent they lack is refused, and a granted-but-deleted agent
    404s rather than serving the default workspace."""
    admin = _admin(app)
    member, uid = _member_with_merli(app, admin)
    _seed_agent_config("merli")
    _seed_agent_config("scout")

    scoped = member.get("/api/files/search?q=memo")
    assert scoped.status_code == 200
    assert scoped.json()["agents_searched"] == ["merli"]

    assert member.get("/api/files/search?q=memo&agent=scout").status_code == 403
    assert admin.get("/api/files/search?q=memo&agent=scout").status_code == 200

    admin.post("/api/agents/ghost/access", json={"user_id": uid})
    assert member.get("/api/files/search?q=memo&agent=ghost").status_code == 404
