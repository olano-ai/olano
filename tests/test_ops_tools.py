from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.workspace import Workspace
from olano.pulse.engine import PulseEngine
from olano.scheduler.cron import OlanoScheduler
from olano.scheduler.db import SchedulerDB
from olano.tools.heartbeat_tools import (
    heartbeat_add,
    heartbeat_configure,
    heartbeat_install_from_skill,
    heartbeat_list,
    heartbeat_remove,
    heartbeat_status,
    heartbeat_update,
)
from olano.tools.pulse_tools import (
    approvals_approve,
    approvals_defer,
    approvals_list,
    approvals_reject,
    ops_list,
    pulse_defer,
    pulse_generate,
    pulse_list,
    pulse_reject,
)


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """The pulse_generate tool drives model-backed curation; provide a dummy
    provider key so the readiness preflight passes and it reaches the mock."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def test_agent_config_injects_ops_tools_at_top_level(tmp_workspace: Path) -> None:
    """Ops tools (scheduler/heartbeat/pulse/approvals/ops) are universal
    top-level defaults; the OpsManager subagent is no longer added."""
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.config.ops_defaults import OPS_TOOL_NAMES

    config = AgentConfig(
        name="ops-test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_workspace),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
        tools=["safe_shell"],
    )

    assert "whoami" in config.tools
    assert "safe_shell" in config.tools
    for name in OPS_TOOL_NAMES:
        assert name in config.tools, f"{name} should be a top-level default"
    assert not any(sub.name == "OpsManager" for sub in config.subagents)
    assert config.builtin_subagents == []


def test_ops_tools_can_be_switched_off_one_by_one(tmp_workspace: Path) -> None:
    config = AgentConfig(
        name="ops-test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_workspace),
        disabled_tools=["scheduler_delete", "approvals_approve"],
    )
    assert "scheduler_delete" not in config.tools
    assert "approvals_approve" not in config.tools
    assert "scheduler_create" in config.tools


def test_explicit_ops_subagent_is_still_honored(tmp_workspace: Path) -> None:
    config = AgentConfig(
        name="ops-test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_workspace),
        builtin_subagents=["ops"],
    )
    ops = next((sub for sub in config.subagents if sub.name == "OpsManager"), None)
    assert ops is not None
    assert "scheduler_create" in ops.tools


def test_customer_service_mode_withholds_the_ops_defaults(tmp_workspace: Path) -> None:
    """A customer must never see or change the operator's scheduler, heartbeat,
    PULSE, approvals or ops queue, so Customer Service mode (memory.per_customer,
    mirrored from customer_service.enabled) gets none of the ops defaults. An
    explicit tools: entry still binds, and the catalog agrees with the builder."""
    from olano.config.internal_tools import effective_internal_tools
    from olano.config.ops_defaults import OPS_TOOL_NAMES

    config = AgentConfig(
        name="cs-test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_workspace),
        customer_service={"enabled": True},
        tools=["scheduler_list"],
    )
    assert config.memory.per_customer is True
    assert "whoami" in config.tools  # the other universal defaults stay
    assert set(config.tools) & set(OPS_TOOL_NAMES) == {"scheduler_list"}

    rows = {r["name"]: r for r in effective_internal_tools(config) if r["category"] == "Operations"}
    assert rows["scheduler_list"]["on"] is True
    assert rows["approvals_approve"]["on"] is False
    assert rows["approvals_approve"]["toggleable"] is False
    assert "Customer Service" in rows["approvals_approve"]["note"]


@pytest.mark.parametrize(
    "name",
    [
        "approvals_approve",
        "approvals_reject",
        "approvals_defer",
        "approvals_clear_expired",
        "pulse_accept",
        "pulse_undo",
        "scheduler_run_now",
        "heartbeat_configure",
    ],
)
def test_ops_tools_that_act_on_held_work_are_owner_only(name: str) -> None:
    """These execute or revert the work a human was asked to review, fire a
    full turn now, or rewrite agent.json5. The name-based fallback read
    approve/accept/undo as read-only, so now that they sit on every agent's
    top-level spec they carry explicit metadata: a peer, an inbound MCP/A2A
    caller, a non-admin member or a channel customer (is_owner=False) is
    refused by the pipeline's owner-only layer."""
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import get_tool_metadata
    from olano.tools.registry import resolve_tools

    (tool,) = resolve_tools([name])
    meta = get_tool_metadata(tool)
    assert meta.owner_only is True, name
    assert meta.capability == "mutating", name

    config = AgentConfig(name="ops-owner", model="anthropic:claude-haiku-4-5-20251001")
    with push_request_context(source="peer", channel="peer", user_id="other", is_owner=False):
        denied = evaluate_tool_access(name, meta, config)
    assert denied.allowed is False
    assert denied.layer == 5
    with push_request_context(source="api", channel="api", is_owner=True, is_admin=True):
        assert evaluate_tool_access(name, meta, config).allowed is True


def test_workers_and_delegates_never_inherit_mutating_ops_tools() -> None:
    from olano.cognition.worker import _WORKER_HARD_DENY
    from olano.config.ops_defaults import (
        OPS_MUTATING_TOOL_NAMES,
        OPS_READ_ONLY_TOOL_NAMES,
        OPS_TOOL_NAMES,
    )
    from olano.security.delegate_guards import DEFAULT_BLOCKED_TOOLS

    assert OPS_READ_ONLY_TOOL_NAMES <= set(OPS_TOOL_NAMES)
    assert OPS_MUTATING_TOOL_NAMES <= _WORKER_HARD_DENY
    assert OPS_MUTATING_TOOL_NAMES <= DEFAULT_BLOCKED_TOOLS
    assert "approvals_approve" in OPS_MUTATING_TOOL_NAMES
    assert "scheduler_run_now" in OPS_MUTATING_TOOL_NAMES


def test_heartbeat_tools_manage_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    Workspace(root=tmp_path).init()

    added = heartbeat_add.invoke(
        {"task": "Review PR inbox", "when": "0 9 * * 1-5", "notes": "Weekday standup prep"}
    )
    listed = heartbeat_list.invoke({})
    updated = heartbeat_update.invoke(
        {
            "task_ref": "1",
            "task": "Review PR inbox and triage",
            "when": "0 10 * * 1-5",
            "notes": "After morning sync",
        }
    )
    removed = heartbeat_remove.invoke({"task_ref": "Review PR inbox and triage"})

    assert "Heartbeat task added" in added
    assert "Review PR inbox" in listed
    assert "when: 0 9 * * 1-5" in listed
    assert "Heartbeat task updated" in updated
    assert "Heartbeat task removed" in removed
    assert "No heartbeat checks configured" in heartbeat_list.invoke({})


def test_heartbeat_install_from_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    workspace = Workspace(root=tmp_path)
    workspace.init()
    skill_dir = workspace.skills_dir / "weekly_report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Weekly Report
heartbeats:
  - task: Collect weekly ticket summary
    when: "0 13 * * 5"
    notes: Build the Friday report draft
---

# Weekly Report
""",
        encoding="utf-8",
    )

    result = heartbeat_install_from_skill.invoke({"skill_name": "weekly_report"})
    listed = heartbeat_list.invoke({})

    assert "added=1" in result
    assert "Collect weekly ticket summary" in listed
    assert "0 13 * * 5" in listed


def _make_agent_on_disk(lionclaw_home: Path, name: str) -> Path:
    """Write a minimal agent.json5 into the home agents dir and return its path."""
    import json

    agent_dir = lionclaw_home / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(
        json.dumps(
            {
                "name": name,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_heartbeat_configure_persists_and_syncs_live(
    tmp_path: Path, lionclaw_home: Path, monkeypatch
) -> None:
    import json

    from olano.tools.talk_to import current_agent

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    cfg_path = _make_agent_on_disk(lionclaw_home, "hb-cfg-agent")
    token = current_agent.set("hb-cfg-agent")
    try:
        scheduler = MagicMock()
        scheduler.set_heartbeat_options.return_value = (True, "synced")
        with patch("olano.scheduler.cron.get_active_scheduler", return_value=scheduler):
            result = heartbeat_configure.invoke({"enabled": "off", "interval_minutes": 30})

        assert "Saved heartbeat settings" in result
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["heartbeat"] == {"enabled": False, "interval_minutes": 30}
        # Live sync pushed the *effective* values into the running scheduler.
        scheduler.set_heartbeat_options.assert_called_once_with(
            "hb-cfg-agent", enabled=False, interval_minutes=30
        )

        # Reset back to system defaults removes the override block entirely.
        with patch("olano.scheduler.cron.get_active_scheduler", return_value=None):
            result = heartbeat_configure.invoke({"reset_to_system_default": True})
        assert "Saved heartbeat settings" in result
        assert "heartbeat" not in json.loads(cfg_path.read_text(encoding="utf-8"))
    finally:
        current_agent.reset(token)


def test_heartbeat_configure_validates_input(
    tmp_path: Path, lionclaw_home: Path, monkeypatch
) -> None:
    from olano.tools.talk_to import current_agent

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    _make_agent_on_disk(lionclaw_home, "hb-val-agent")
    token = current_agent.set("hb-val-agent")
    try:
        assert "between 1 and 1440" in heartbeat_configure.invoke({"interval_minutes": 5000})
        assert "Expected 'on' or 'off'" in heartbeat_configure.invoke({"enabled": "maybe"})
        assert "Nothing to change" in heartbeat_configure.invoke({})
    finally:
        current_agent.reset(token)


def test_heartbeat_status_reports_effective_settings(
    tmp_path: Path, lionclaw_home: Path, monkeypatch
) -> None:
    import json

    from olano.tools.talk_to import current_agent

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    cfg_path = _make_agent_on_disk(lionclaw_home, "hb-status-agent")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["heartbeat"] = {"interval_minutes": 30}
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    token = current_agent.set("hb-status-agent")
    try:
        status = heartbeat_status.invoke({})
    finally:
        current_agent.reset(token)

    assert "ENABLED (system default)" in status
    assert "every 30 min (agent override" in status
    assert "system default is 60 min" in status


def test_pulse_tools_manage_items(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    workspace = Workspace(root=tmp_path)
    workspace.init()
    (tmp_path / "SOUL.md").write_text("# Soul\n", encoding="utf-8")

    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="pulse-test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path)),
    )
    engine = PulseEngine(config, workspace.state_dir)
    proposal = engine.add(
        content="Review proposed SOUL.md updates",
        lane="approval",
        action={
            "type": "append_lines",
            "path": "SOUL.md",
            "heading": "Self-Improvement",
            "lines": ["Prefer operational answers when managing automation."],
        },
    )
    first_proposal_id = proposal.id
    reminder = engine.add("Review editorial direction for PULSE")
    ops_item = engine.generate_security_alert("Host sandbox unexpectedly enabled.")

    listed = pulse_list.invoke({"max_items": 10})
    deferred = pulse_defer.invoke({"item_id": reminder.id, "hours": 12})
    rejected = pulse_reject.invoke({"item_id": reminder.id})
    approval_listed = approvals_list.invoke({"max_items": 10})
    approval_deferred = approvals_defer.invoke({"item_id": proposal.id, "hours": 6})
    approval_rejected = approvals_reject.invoke({"item_id": proposal.id})

    engine = PulseEngine(config, workspace.state_dir)
    # Distinct content from the first (rejected) proposal — the engine suppresses
    # re-adds of recently-rejected items for 7 days, which is intended, so the
    # approve-target must be a genuinely new item.
    proposal = engine.add(
        content="Apply approved SOUL.md self-improvement",
        lane="approval",
        action={
            "type": "append_lines",
            "path": "SOUL.md",
            "heading": "Self-Improvement",
            "lines": ["Prefer operational answers when managing automation."],
        },
    )
    approved = approvals_approve.invoke({"item_id": proposal.id})
    ops_listed = ops_list.invoke({"max_items": 10})

    assert reminder.id in listed
    assert "Deferred PULSE item" in deferred
    assert "Rejected PULSE item" in rejected
    assert first_proposal_id in approval_listed
    assert "Deferred approval item" in approval_deferred
    assert "Rejected approval item" in approval_rejected
    assert "Updated SOUL.md." in approved
    assert ops_item.id in ops_listed
    assert "Prefer operational answers" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


def test_sync_db_task_registers_one_time_job(lionclaw_home: Path) -> None:
    db = SchedulerDB()
    task_id = db.create_task(
        name="Friday report",
        prompt="Build the resolved-ticket report",
        run_at="2026-04-17T13:00:00",
        timezone="UTC",
        agent_name="ops-agent",
    )
    scheduler = OlanoScheduler()

    with patch.object(scheduler._scheduler, "add_job") as add_job:
        ok, message = scheduler.sync_db_task(task_id)

    assert ok is True
    assert "synced" in message
    assert add_job.call_args.kwargs["id"] == f"db_{task_id[:8]}"
    assert add_job.call_args.kwargs["trigger"].__class__.__name__ == "DateTrigger"


async def test_run_db_task_now_invokes_runner_and_records_history(lionclaw_home: Path) -> None:
    db = SchedulerDB()
    task_id = db.create_task(
        name="Immediate report",
        prompt="Summarize the latest resolved tickets",
        schedule="0 13 * * 5",
        timezone="UTC",
        agent_name="ops-agent",
    )
    scheduler = OlanoScheduler()
    runner = MagicMock()
    runner.ainvoke = AsyncMock(return_value="report sent")
    scheduler._runners["ops-agent"] = runner

    ok, message = scheduler.run_db_task_now(task_id)
    await asyncio.sleep(0)

    history = db.get_history(task_id, limit=1)
    assert ok is True
    assert "queued for immediate execution" in message
    assert history[0]["result"] == "report sent"
    runner.ainvoke.assert_awaited_once()


async def test_run_db_task_now_from_thread_uses_runtime_loop(lionclaw_home: Path) -> None:
    """A run-now from a loop-less worker thread (sync agent tool in the
    executor) must execute on the runtime loop, not a private ``asyncio.run``
    loop — the runner's checkpointer holds asyncio locks bound to the runtime
    loop, so a foreign loop crashed with "is bound to a different event loop"."""
    db = SchedulerDB()
    task_id = db.create_task(
        name="Immediate report",
        prompt="Summarize the latest resolved tickets",
        schedule="0 13 * * 5",
        timezone="UTC",
        agent_name="ops-agent",
    )
    scheduler = OlanoScheduler()
    runtime_loop = asyncio.get_running_loop()
    scheduler._loop = runtime_loop  # what start() captures in the runtime

    seen_loop: asyncio.AbstractEventLoop | None = None

    async def _record_loop(prompt: str, thread_id: str | None = None, **_overrides: object) -> str:
        nonlocal seen_loop
        seen_loop = asyncio.get_running_loop()
        return "report sent"

    runner = MagicMock()
    runner.ainvoke = _record_loop
    scheduler._runners["ops-agent"] = runner

    ok, message = await asyncio.to_thread(scheduler.run_db_task_now, task_id)
    for _ in range(200):  # the run is fire-and-forget — wait for it to land
        if seen_loop is not None:
            break
        await asyncio.sleep(0.01)

    assert ok is True
    assert "queued for immediate execution" in message
    assert seen_loop is runtime_loop
    assert db.get_history(task_id, limit=1)[0]["result"] == "report sent"


def test_scheduler_tolerates_late_wakeups() -> None:
    """Jobs sharing an on-the-hour slot wake a second or two late on a busy
    loop; APScheduler's default 1s misfire grace silently skipped them all."""
    scheduler = OlanoScheduler()
    defaults = scheduler._scheduler._job_defaults
    assert defaults["misfire_grace_time"] >= 60
    assert defaults["coalesce"] is True


async def test_pulse_generate_tool_creates_curated_items(
    tmp_path: Path,
    lionclaw_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    workspace = Workspace(root=tmp_path)
    workspace.init()
    (tmp_path / "IDENTITY.md").write_text("# Identity\nDesign-oriented.\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\nPrefer leverage.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\nInterested in automation.\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n- Working on PULSE.\n", encoding="utf-8")
    sessions_dir = lionclaw_home / "sessions" / "ops"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "thread-1.jsonl").write_text(
        "\n".join(
            [
                '{"role":"user","content":"Help me redesign PULSE."}',
                '{"role":"assistant","content":"I will make it more editorial."}',
                '{"role":"user","content":"Separate the signal from the ops noise."}',
                '{"role":"assistant","content":"I will turn that into a curated lane."}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = MagicMock(
        content=(
            '{"items":[{"type":"recommendation","priority":"normal","title":"Separate insights from ops",'
            '"summary":"The workspace is now ready for a cleaner editorial PULSE lane.",'
            '"why_now":"You are actively implementing the redesign.",'
            '"suggested_next_step":"Document the Phase 3.1 semantics in the architecture spec.",'
            '"references":["LIONCLAW_2.0_FINAL_ARCHITECTURE_PHASE_3_1.md"]}]}'
        )
    )

    with patch("olano.models.build_model", return_value=mock_model):
        result = await pulse_generate.ainvoke({"thread_id": "thread-1"})

    assert "Generated 1 curated PULSE item" in result
    assert "Separate insights from ops" in result


@pytest.mark.parametrize(
    "module",
    ["olano.security.delegate_guards", "olano.skills.loader", "olano.config.ops_defaults"],
)
def test_ops_tool_names_import_cleanly_as_first_import(module: str) -> None:
    """The ops name sets are a leaf (security.ops_tool_names): delegate_guards
    folds the mutating subset into its block list, and agent_config imports
    delegate_guards at module level, so pulling the names from olano.config
    there made ``import olano.security.delegate_guards`` (or anything reaching
    it before olano.config, skills.loader included) fail on a cold process with
    a partially initialised module. Only a fresh interpreter can test it."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
