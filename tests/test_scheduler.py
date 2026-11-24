"""Tests for OlanoScheduler and heartbeat support."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from olano.config.agent_config import AgentConfig
from olano.scheduler.cron import (
    OlanoScheduler,
    _cron_matches,
    _load_state,
    _parse_heartbeat,
    _save_state,
    _should_rerun,
)
from olano.scheduler.heartbeat import parse_heartbeat_text, render_heartbeat_tasks


class TestHeartbeatParser:
    def test_parse_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "HEARTBEAT.md"
        path.write_text("# Empty\n# Just a comment\n", encoding="utf-8")
        tasks = _parse_heartbeat(path)
        assert tasks == []

    def test_parse_task(self, tmp_path: Path) -> None:
        path = tmp_path / "HEARTBEAT.md"
        path.write_text(
            "- task: Check for urgent messages\n  when: always\n  notes: Priority inbox\n",
            encoding="utf-8",
        )
        tasks = _parse_heartbeat(path)
        assert len(tasks) == 1
        assert tasks[0]["task"] == "Check for urgent messages"
        assert tasks[0]["when"] == "always"
        assert tasks[0]["notes"] == "Priority inbox"

    def test_parse_multiple_tasks(self, tmp_path: Path) -> None:
        path = tmp_path / "HEARTBEAT.md"
        path.write_text(
            "- task: Task A\n  when: always\n\n- task: Task B\n  when: 0 22 * * *\n",
            encoding="utf-8",
        )
        tasks = _parse_heartbeat(path)
        assert len(tasks) == 2
        assert tasks[0]["task"] == "Task A"
        assert tasks[1]["task"] == "Task B"


class TestHeartbeatState:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        state = _load_state(tmp_path / "heartbeat-state.json")
        assert state == {}

    def test_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "heartbeat-state.json"
        state = {"Task A": "2025-01-01T00:00:00+00:00"}
        _save_state(path, state)
        loaded = _load_state(path)
        assert loaded == state


class TestShouldRerun:
    def test_always_rerun_after_cooldown(self) -> None:
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert _should_rerun(old_time.isoformat(), "always", now) is True

    def test_no_rerun_within_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        just_now = now.isoformat()
        assert _should_rerun(just_now, "always", now) is False

    def test_rerun_unknown_when(self) -> None:
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert _should_rerun(old_time.isoformat(), "0 9 * * 1", now) is True


class TestLionClawScheduler:
    def test_list_jobs_empty(self) -> None:
        sched = OlanoScheduler()
        assert sched.list_jobs() == []

    def test_register_agent_no_schedule(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        sched = OlanoScheduler()
        runner = MagicMock()
        sched.register_agent(base_config, runner, workspace_root=None)
        assert base_config.name in sched._jobs
        # Even with no workspace (no heartbeat), every agent gets its issue
        # poll — assigned-issue delivery must not depend on other features.
        assert sched._jobs[base_config.name] == [f"{base_config.name}_issues"]

    def test_register_agent_with_heartbeat(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        # Create HEARTBEAT.md with a task
        hb = tmp_workspace / "HEARTBEAT.md"
        hb.write_text("- task: Daily check\n  when: always\n", encoding="utf-8")

        sched = OlanoScheduler()
        runner = MagicMock()

        with patch("apscheduler.schedulers.asyncio.AsyncIOScheduler.add_job"):
            sched.register_agent(base_config, runner, workspace_root=tmp_workspace)

        # heartbeat job should be registered
        jobs = sched._jobs.get(base_config.name, [])
        assert f"{base_config.name}_heartbeat" in jobs

    def test_register_agent_without_heartbeat_file_still_registers_poll(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        """The poll must exist even before HEARTBEAT.md does: tasks added later
        (heartbeat_add) and skill-level HEARTBEAT.md files fire without a restart."""
        assert not (tmp_workspace / "HEARTBEAT.md").exists()

        sched = OlanoScheduler()
        runner = MagicMock()

        with patch("apscheduler.schedulers.asyncio.AsyncIOScheduler.add_job"):
            sched.register_agent(base_config, runner, workspace_root=tmp_workspace)

        jobs = sched._jobs.get(base_config.name, [])
        assert f"{base_config.name}_heartbeat" in jobs

    def test_unregister_agent(self, base_config: AgentConfig, tmp_workspace: Path) -> None:
        sched = OlanoScheduler()
        runner = MagicMock()
        sched.register_agent(base_config, runner, workspace_root=None)
        sched.unregister_agent(base_config.name)
        assert base_config.name not in sched._jobs


class TestHeartbeatSettingsResolution:
    def test_defaults_are_enabled_hourly(self) -> None:
        from olano.config.system_settings import (
            effective_heartbeat_enabled,
            effective_heartbeat_interval,
        )

        assert effective_heartbeat_enabled(None) is True
        assert effective_heartbeat_interval(None) == 60

    def test_agent_override_wins(self, base_config: AgentConfig) -> None:
        from olano.config.system_settings import (
            effective_heartbeat_enabled,
            effective_heartbeat_interval,
        )

        base_config.heartbeat.enabled = False
        base_config.heartbeat.interval_minutes = 15
        assert effective_heartbeat_enabled(base_config) is False
        assert effective_heartbeat_interval(base_config) == 15

    def test_home_config_yaml_sets_system_default(self, lionclaw_home: Path) -> None:
        from olano.config.system_settings import (
            effective_heartbeat_enabled,
            effective_heartbeat_interval,
        )

        (lionclaw_home / "config.yaml").write_text(
            "heartbeat:\n  enabled: false\n  interval_minutes: 30\n", encoding="utf-8"
        )
        # No per-agent override (None fields) → the yaml block resolves.
        assert effective_heartbeat_enabled(None) is False
        assert effective_heartbeat_interval(None) == 30

    def test_interval_is_clamped(self, base_config: AgentConfig) -> None:
        from olano.config.system_settings import effective_heartbeat_interval

        base_config.heartbeat.interval_minutes = 0
        assert effective_heartbeat_interval(base_config) == 1
        base_config.heartbeat.interval_minutes = 10_000
        assert effective_heartbeat_interval(base_config) == 1440


class TestHeartbeatOptions:
    def test_add_heartbeat_job_uses_configured_interval(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        base_config.heartbeat.interval_minutes = 15
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched._add_heartbeat_job(base_config, MagicMock(), tmp_workspace)

        trigger = add_job.call_args.kwargs["trigger"]
        assert isinstance(trigger, IntervalTrigger)
        assert trigger.interval == timedelta(minutes=15)
        assert sched._heartbeat_settings[base_config.name] == {
            "enabled": True,
            "interval_minutes": 15,
        }

    def test_set_heartbeat_options_updates_live_settings(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched._add_heartbeat_job(base_config, MagicMock(), tmp_workspace)

        with patch.object(sched._scheduler, "reschedule_job") as resched:
            ok, message = sched.set_heartbeat_options(
                base_config.name, enabled=False, interval_minutes=30
            )

        assert ok is True
        assert "disabled" in message and "30 min" in message
        assert sched._heartbeat_settings[base_config.name] == {
            "enabled": False,
            "interval_minutes": 30,
        }
        resched.assert_called_once()

    def test_set_heartbeat_options_unknown_agent(self) -> None:
        sched = OlanoScheduler()
        ok, message = sched.set_heartbeat_options("ghost", enabled=True)
        assert ok is False
        assert "no heartbeat poll" in message

    async def test_disabled_heartbeat_skips_poll(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Check inbox\n  when: always\n", encoding="utf-8"
        )
        runner = _mock_runner()
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched._add_heartbeat_job(base_config, runner, tmp_workspace)
        run = add_job.call_args.args[0]

        sched.set_heartbeat_options(base_config.name, enabled=False)
        await run()
        runner.ainvoke.assert_not_awaited()

        with patch.object(sched._scheduler, "reschedule_job"):
            sched.set_heartbeat_options(base_config.name, enabled=True)
        await run()
        runner.ainvoke.assert_awaited_once()


class TestCronMatches:
    def test_every_minute_matches(self) -> None:
        assert _cron_matches("* * * * *", datetime.now(timezone.utc)) is True

    def test_cron_outside_window_does_not_match(self) -> None:
        # A cron whose next fire is days away can't fall in the 5-minute
        # lookback window regardless of the host timezone.
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=3)
        assert _cron_matches(f"0 0 {future.day} {future.month} *", now) is False

    def test_unparseable_expression_is_permissive(self) -> None:
        now = datetime.now(timezone.utc)
        assert _cron_matches("every day at noon", now) is True
        assert _cron_matches("a b c d e", now) is True

    def test_lookback_scales_with_poll_interval(self) -> None:
        # A cron that fired ~30 min ago is inside an hourly poll's window but
        # outside a 5-minute one — no fire time may fall between two polls.
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        fired_minute = (now - timedelta(minutes=30)).minute
        expr = f"{fired_minute} * * * *"
        assert _cron_matches(expr, now, lookback_minutes=60) is True
        assert _cron_matches(expr, now, lookback_minutes=5) is False


class TestShouldRerunCron:
    def test_no_rerun_when_cron_task_just_ran(self) -> None:
        now = datetime.now(timezone.utc)
        just_ran = (now - timedelta(seconds=30)).isoformat()
        assert _should_rerun(just_ran, "0 9 * * 1", now) is False


class TestCollectSkillHeartbeats:
    @staticmethod
    def _make_skill(
        skill_dir: Path,
        *,
        heartbeat: str = "- task: fetch BTC price\n  when: always\n",
        skill_md: str | None = "---\nname: Price Check\n---\n\nUse CoinGecko.",
    ) -> None:
        skill_dir.mkdir(parents=True)
        (skill_dir / "HEARTBEAT.md").write_text(heartbeat, encoding="utf-8")
        if skill_md is not None:
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    def test_discovers_workspace_and_system_skills(self, tmp_path: Path) -> None:
        self._make_skill(tmp_path / "skills" / "price-check")
        # system/skills is scanned recursively, so nested layouts count too.
        self._make_skill(tmp_path / "system" / "skills" / "core" / "reporter")
        (tmp_path / "skills" / "no-heartbeat").mkdir(parents=True)
        (tmp_path / "skills" / "no-heartbeat" / "SKILL.md").write_text(
            "---\nname: Plain\n---\n", encoding="utf-8"
        )

        results = OlanoScheduler._collect_skill_heartbeats(tmp_path)
        slugs = {slug for slug, _, _ in results}
        assert slugs == {"price-check", "reporter"}

    def test_frontmatter_stripped_from_skill_body(self, tmp_path: Path) -> None:
        self._make_skill(tmp_path / "skills" / "price-check")
        ((slug, hb_path, body),) = OlanoScheduler._collect_skill_heartbeats(tmp_path)
        assert slug == "price-check"
        assert hb_path.name == "HEARTBEAT.md"
        assert body == "Use CoinGecko."

    def test_skill_without_skill_md_gets_empty_body(self, tmp_path: Path) -> None:
        self._make_skill(tmp_path / "skills" / "bare", skill_md=None)
        ((_, _, body),) = OlanoScheduler._collect_skill_heartbeats(tmp_path)
        assert body == ""


def _mock_runner(response: str = "HEARTBEAT_OK") -> MagicMock:
    runner = MagicMock()
    runner.ainvoke = AsyncMock(return_value=response)
    return runner


def _capture_heartbeat_run(
    config: AgentConfig, runner: MagicMock, workspace: Path
) -> Callable[[], object]:
    """Register the heartbeat job against a mocked APScheduler and return the
    poll coroutine function the scheduler would fire every 5 minutes."""
    sched = OlanoScheduler()
    with patch.object(sched._scheduler, "add_job") as add_job:
        job_id = sched._add_heartbeat_job(config, runner, workspace)
    assert job_id == f"{config.name}_heartbeat"
    return add_job.call_args.args[0]


class TestHeartbeatExecution:
    """End-to-end poll behaviour: what the agent actually receives when a task fires."""

    async def test_task_fires_with_context_thread_and_state(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Check inbox\n  when: always\n  notes: Priority first\n",
            encoding="utf-8",
        )
        runner = _mock_runner()
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        await run()

        runner.ainvoke.assert_awaited_once()
        message = runner.ainvoke.call_args.args[0]
        assert message.startswith("HEARTBEAT: Check inbox")
        assert "Context: Priority first" in message
        # Each firing gets its own thread (see TestFreshRunThreads) — the run is
        # still bound to this agent's heartbeat lineage, hence the prefix.
        assert runner.ainvoke.call_args.kwargs["thread_id"].startswith(
            f"heartbeat_{base_config.name}@"
        )

        state_path = tmp_workspace / "memories" / "heartbeat-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert "Check inbox" in state

    async def test_always_task_respects_cooldown(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Check inbox\n  when: always\n", encoding="utf-8"
        )
        runner = _mock_runner()
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        await run()
        await run()

        assert runner.ainvoke.await_count == 1

    async def test_cron_task_outside_window_is_skipped(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=3)
        (tmp_workspace / "HEARTBEAT.md").write_text(
            f"- task: Monthly report\n  when: 0 0 {future.day} {future.month} *\n",
            encoding="utf-8",
        )
        runner = _mock_runner()
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        await run()

        runner.ainvoke.assert_not_awaited()

    async def test_skill_heartbeat_fires_without_workspace_file(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        """Skill-level HEARTBEAT.md must work standalone — namespaced key, skill
        context injected — even when the workspace has no HEARTBEAT.md at all."""
        assert not (tmp_workspace / "HEARTBEAT.md").exists()
        skill_dir = tmp_workspace / "skills" / "price-check"
        skill_dir.mkdir(parents=True)
        (skill_dir / "HEARTBEAT.md").write_text(
            "- task: fetch BTC price\n  when: always\n", encoding="utf-8"
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: Price Check\n---\n\nUse CoinGecko.", encoding="utf-8"
        )
        runner = _mock_runner()
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        await run()

        runner.ainvoke.assert_awaited_once()
        message = runner.ainvoke.call_args.args[0]
        assert message.startswith("HEARTBEAT: [price-check] fetch BTC price")
        assert "Skill instructions:" in message
        assert "Use CoinGecko." in message

        state = json.loads(
            (tmp_workspace / "memories" / "heartbeat-state.json").read_text(encoding="utf-8")
        )
        assert "[price-check] fetch BTC price" in state

    async def test_findings_are_captured_to_pulse(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Check inbox\n  when: always\n", encoding="utf-8"
        )
        runner = _mock_runner("Found 3 urgent emails")
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        with patch("olano.pulse.generators.PulseGenerator") as gen_cls:
            await run()

        gen_cls.assert_called_once_with(base_config, tmp_workspace)
        findings = gen_cls.return_value.capture_heartbeat_findings.call_args.args[0]
        assert findings == [
            {"task": "Check inbox", "content": "Found 3 urgent emails", "priority": "normal"}
        ]

    async def test_heartbeat_ok_is_not_captured_to_pulse(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Check inbox\n  when: always\n", encoding="utf-8"
        )
        runner = _mock_runner("HEARTBEAT_OK")
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        with patch("olano.pulse.generators.PulseGenerator") as gen_cls:
            await run()

        gen_cls.assert_not_called()

    async def test_failed_task_skips_state_but_later_tasks_still_run(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "HEARTBEAT.md").write_text(
            "- task: Broken task\n  when: always\n\n- task: Working task\n  when: always\n",
            encoding="utf-8",
        )
        runner = MagicMock()
        runner.ainvoke = AsyncMock(side_effect=[RuntimeError("boom"), "HEARTBEAT_OK"])
        run = _capture_heartbeat_run(base_config, runner, tmp_workspace)

        await run()

        assert runner.ainvoke.await_count == 2
        state = json.loads(
            (tmp_workspace / "memories" / "heartbeat-state.json").read_text(encoding="utf-8")
        )
        assert "Working task" in state
        assert "Broken task" not in state  # failed → eligible again next poll


class TestToolSchedulerRoundTrip:
    """The heartbeat_* tools and the scheduler poll share one format."""

    def test_render_parse_round_trip(self) -> None:
        tasks = [
            {"task": "Check RSS", "when": "*/15 * * * *", "notes": "tech feeds"},
            {"task": "Tidy memory", "when": "always"},
        ]
        assert parse_heartbeat_text(render_heartbeat_tasks(tasks)) == tasks

    def test_quoted_when_is_unquoted(self) -> None:
        tasks = parse_heartbeat_text('- task: Nightly\n  when: "0 22 * * *"\n')
        assert tasks == [{"task": "Nightly", "when": "0 22 * * *"}]

    def test_heartbeat_add_writes_scheduler_parseable_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
        from olano.tools.heartbeat_tools import heartbeat_add

        heartbeat_add.invoke({"task": "Check RSS", "when": "*/15 * * * *", "notes": "tech feeds"})

        tasks = _parse_heartbeat(tmp_path / "HEARTBEAT.md")
        assert tasks == [{"task": "Check RSS", "when": "*/15 * * * *", "notes": "tech feeds"}]


class TestConfigSchedule:
    """agent.json5 `schedule` jobs: deterministic ids, enumeration, live resync."""

    @staticmethod
    def _sched(**kw):
        from olano.config.agent_config import ScheduleConfig

        return ScheduleConfig(**kw)

    def test_deterministic_ids_and_enumeration(self, base_config: AgentConfig) -> None:
        base_config.schedule = [
            self._sched(cron="0 10 * * *", message="morning", name="AM"),
            self._sched(cron="0 18 * * *", message="evening"),
        ]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, MagicMock(), workspace_root=None)

        ids = [c.kwargs["id"] for c in add_job.call_args_list if c.kwargs["id"].startswith("cfg:")]
        assert ids == [f"cfg:{base_config.name}:0", f"cfg:{base_config.name}:1"]

        jobs = sched.list_config_jobs(base_config.name)
        assert [j["id"] for j in jobs] == ids
        assert jobs[0]["cron"] == "0 10 * * *"
        assert jobs[0]["name"] == "AM"
        assert jobs[0]["agent"] == base_config.name

    def test_disabled_entry_keeps_descriptor_without_live_job(
        self, base_config: AgentConfig
    ) -> None:
        base_config.schedule = [
            self._sched(cron="0 10 * * *", message="on"),
            self._sched(cron="0 18 * * *", message="off", enabled=False),
        ]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, MagicMock(), workspace_root=None)

        registered = [
            c.kwargs["id"] for c in add_job.call_args_list if c.kwargs["id"].startswith("cfg:")
        ]
        assert registered == [f"cfg:{base_config.name}:0"]  # disabled one not registered

        jobs = sched.list_config_jobs(base_config.name)
        assert len(jobs) == 2
        assert jobs[1]["enabled"] is False
        assert jobs[1]["next_run"] is None

    def test_reregistration_does_not_duplicate(self, base_config: AgentConfig) -> None:
        """Re-registering the same agent REPLACES the config job (stable id) —
        the old random-uuid scheme silently accumulated duplicates here."""
        base_config.schedule = [self._sched(cron="0 10 * * *", message="m")]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched.register_agent(base_config, MagicMock(), workspace_root=None)
            sched.unregister_agent(base_config.name)
            sched.register_agent(base_config, MagicMock(), workspace_root=None)

        jobs = sched.list_config_jobs(base_config.name)
        assert [j["id"] for j in jobs] == [f"cfg:{base_config.name}:0"]

    def test_resync_replaces_jobs_without_stale_ids(self, base_config: AgentConfig) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="old")]
        sched = OlanoScheduler()
        with (
            patch.object(sched._scheduler, "add_job"),
            patch.object(sched._scheduler, "remove_job") as remove_job,
        ):
            sched.register_agent(base_config, MagicMock(), workspace_root=None)
            base_config.schedule = [
                self._sched(cron="0 9 * * *", message="new1"),
                self._sched(cron="0 21 * * *", message="new2"),
            ]
            ok, _ = sched.resync_agent_config_schedule(base_config)

        assert ok is True
        removed = [c.args[0] for c in remove_job.call_args_list]
        assert f"cfg:{base_config.name}:0" in removed
        jobs = sched.list_config_jobs(base_config.name)
        assert [j["cron"] for j in jobs] == ["0 9 * * *", "0 21 * * *"]
        # No duplicate / stale id lingering in the aggregate job list.
        assert sched._jobs[base_config.name].count(f"cfg:{base_config.name}:0") == 1

    def test_resync_unbooted_agent_saves_descriptors_only(self, base_config: AgentConfig) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="m")]
        sched = OlanoScheduler()  # never registered → no runner
        ok, msg = sched.resync_agent_config_schedule(base_config)
        assert ok is False
        assert "not active" in msg
        assert len(sched.list_config_jobs(base_config.name)) == 1

    def test_cron_string_prefers_cron_over_split_fields(self) -> None:
        assert (
            OlanoScheduler._cron_string(self._sched(cron="0 10 * * *", message="m")) == "0 10 * * *"
        )
        assert (
            OlanoScheduler._cron_string(self._sched(minute="30", hour="9", message="m"))
            == "30 9 * * *"
        )

    def test_run_config_job_now_dispatches(self, base_config: AgentConfig) -> None:
        # Sync test → no running loop → _dispatch_now runs the coroutine inline,
        # so we can assert the runner was actually invoked.
        base_config.schedule = [self._sched(cron="0 10 * * *", message="do it")]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="ok")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched.register_agent(base_config, runner, workspace_root=None)
        with patch("olano.scheduler.cron._record_config_run") as rec:
            ok, _ = sched.run_config_job_now(f"cfg:{base_config.name}:0")
        assert ok is True
        runner.ainvoke.assert_awaited_once()
        # The entry's message arrives behind the autonomous directive and the
        # date stamp, like a DB task's (see TestAutonomousRunsKnowTheDate).
        sent = runner.ainvoke.call_args.args[0]
        assert sent.startswith("[Autonomous scheduled task")
        assert "Current date and time:" in sent
        assert sent.endswith("\n\ndo it")
        rec.assert_called_once()

    def test_run_config_job_now_unknown_id(self) -> None:
        sched = OlanoScheduler()
        ok, msg = sched.run_config_job_now("cfg:ghost:0")
        assert ok is False
        assert "not registered" in msg

    def test_run_config_job_now_agent_not_active(self, base_config: AgentConfig) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="m")]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched.register_agent(base_config, MagicMock(), workspace_root=None)
        sched._runners.pop(base_config.name, None)  # agent stopped, descriptor remains
        ok, msg = sched.run_config_job_now(f"cfg:{base_config.name}:0")
        assert ok is False
        assert "not active" in msg

    async def test_fire_records_run_history(self, base_config: AgentConfig) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="do it")]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, runner, workspace_root=None)
        run = next(
            c.args[0]
            for c in add_job.call_args_list
            if c.kwargs["id"] == f"cfg:{base_config.name}:0"
        )

        with patch("olano.scheduler.cron._record_config_run") as rec:
            await run()

        runner.ainvoke.assert_awaited_once()
        rec.assert_called_once()
        assert rec.call_args.args[0] == f"cfg:{base_config.name}:0"
        assert rec.call_args.kwargs["result"] == "done"


class TestConfigScheduleApiHelpers:
    def test_entry_cron_string(self) -> None:
        from olano.runtime.api import _entry_cron_string

        assert _entry_cron_string({"cron": "0 8 * * *"}) == "0 8 * * *"
        assert _entry_cron_string({"minute": "0", "hour": "9"}) == "0 9 * * *"
        assert _entry_cron_string({}) == "* * * * *"
        # A malformed cron falls back to the split fields, not the bad string.
        assert _entry_cron_string({"cron": "noon"}) == "* * * * *"

    def test_parse_config_job_id(self) -> None:
        from olano.runtime.api import _parse_config_job_id

        assert _parse_config_job_id("cfg:cindy:2") == ("cindy", 2)
        # Agent name containing a colon is tolerated (rpartition on the index).
        assert _parse_config_job_id("cfg:a:b:3") == ("a:b", 3)

    def test_parse_config_job_id_rejects_malformed(self) -> None:
        import pytest
        from fastapi import HTTPException

        from olano.runtime.api import _parse_config_job_id

        for bad in ("cfg:cindy", "cfg::0", "cfg:cindy:x"):
            with pytest.raises(HTTPException):
                _parse_config_job_id(bad)


async def test_fleet_daily_highlights_job_registered() -> None:
    """start() registers the fleet-wide daily work-journal highlights job."""
    sched = OlanoScheduler()
    sched.start()
    try:
        ids = {j["id"] for j in sched.list_jobs()}
        assert "fleet_daily_highlights" in ids
    finally:
        sched.stop()


async def test_fleet_jobs_are_coroutine_functions() -> None:
    """Every fleet job's callable must be a genuine coroutine function.

    APScheduler classifies a plain callable as a threadpool job and never awaits
    the coroutine it returns — a sync ``lambda: _rollup_report(...)`` wrapper
    meant the weekly/monthly reports were silently never executed."""
    import asyncio

    sched = OlanoScheduler()
    sched.start()
    try:
        fleet = [j for j in sched._scheduler.get_jobs() if j.id.startswith("fleet_")]
        assert {j.id for j in fleet} == {
            "fleet_daily_highlights",
            "fleet_weekly_report",
            "fleet_monthly_report",
        }
        for job in fleet:
            assert asyncio.iscoroutinefunction(job.func), (
                f"{job.id} is registered as a sync callable — APScheduler will "
                "never await the coroutine it returns"
            )
    finally:
        sched.stop()


class TestFreshRunThreads:
    """Scheduled jobs get a fresh LangGraph thread per firing.

    Every scheduled path used to pin one constant thread id, so run N appended to
    runs 1..N-1's conversation forever. LangGraph rewrites the whole ``messages``
    channel per step, so the per-step checkpoint grew to the size of the entire
    history (14 MB / 27 checkpoints on one production thread) and the
    (de)serialization runs on the event loop — one 2-hourly task held ~65% of a
    core for 26 minutes and starved the API.
    """

    @staticmethod
    def _sched(**kw):
        from olano.config.agent_config import ScheduleConfig

        return ScheduleConfig(**kw)

    def test_fresh_ids_share_base_and_never_repeat(self) -> None:
        """Distinctness is a guarantee, not a probability.

        The suffix used to be 4 random hex digits — 65 536 values, so even these
        few thousand ids in one second collide by the birthday bound (50 draws
        already flaked CI at ~1.8%). A collision is not merely a flaky test: two
        firings would share a thread, which is the bug this id exists to prevent.
        The monotonic half of the suffix makes the count irrelevant.
        """
        from olano.scheduler.cron import fresh_run_thread_id

        ids = {fresh_run_thread_id("scheduled_bob") for _ in range(5000)}
        assert len(ids) == 5000, "ids collided — a run would resume another run's thread"
        assert all(i.startswith("scheduled_bob@") for i in ids)

    def test_fresh_ids_never_repeat_across_threads(self) -> None:
        """Firings arrive from the scheduler loop and from API threads at once."""
        from concurrent.futures import ThreadPoolExecutor

        from olano.scheduler.cron import fresh_run_thread_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: fresh_run_thread_id("scheduled_bob"), range(2000)))

        assert len(set(ids)) == len(ids), "concurrent firings collided on one thread id"

    async def test_config_cron_firing_uses_a_new_thread_each_time(
        self, base_config: AgentConfig
    ) -> None:
        """Drive the registered closure twice; the two firings must not share a thread."""
        base_config.schedule = [self._sched(cron="0 10 * * *", message="hi")]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, MagicMock(), workspace_root=None)

        fire = add_job.call_args_list[0].args[0]
        seen: list[str] = []
        with patch.object(
            sched,
            "_run_config_message",
            new=AsyncMock(side_effect=lambda *a, **k: seen.append(a[3])),
        ):
            await fire()
            await fire()

        assert len(seen) == 2
        assert seen[0] != seen[1], "both firings reused one thread id"
        assert all(t.startswith(f"scheduled_{base_config.name}@") for t in seen)

    async def test_explicit_thread_id_stays_pinned(self, base_config: AgentConfig) -> None:
        """`thread_id` is documented opt-in memory continuity — never freshened."""
        base_config.schedule = [
            self._sched(cron="0 10 * * *", message="hi", thread_id="my-standing-thread")
        ]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, MagicMock(), workspace_root=None)

        fire = add_job.call_args_list[0].args[0]
        seen: list[str] = []
        with patch.object(
            sched,
            "_run_config_message",
            new=AsyncMock(side_effect=lambda *a, **k: seen.append(a[3])),
        ):
            await fire()
            await fire()

        assert seen == ["my-standing-thread", "my-standing-thread"]


class TestSkippedRunsAreRecorded:
    """A firing APScheduler drops must leave a trace a product surface can see.

    Before this, `max_instances` (previous run still going) and a missed
    misfire window both ended a run with nothing but an APScheduler WARNING in
    the process log: no history row, so the Scheduler view showed the task as
    healthy with a normal run count while runs were being dropped. Observed
    2026-08-14 on a `* * * * *` task whose agent turns ran past 60s.
    """

    def _event(self, job_id: str, code: int) -> MagicMock:
        event = MagicMock()
        event.job_id = job_id
        event.code = code
        return event

    def test_listener_is_registered_for_both_skip_events(self) -> None:
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

        scheduler = OlanoScheduler()
        with patch.object(scheduler._scheduler, "add_listener") as add_listener:
            scheduler._register_skip_listener()

        assert add_listener.call_count == 1
        _, mask = add_listener.call_args[0]
        assert mask & EVENT_JOB_MAX_INSTANCES
        assert mask & EVENT_JOB_MISSED

    def test_max_instances_skip_writes_history_for_a_db_task(self) -> None:
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES

        import olano.scheduler.db  # noqa: F401  (patch target must be imported)

        scheduler = OlanoScheduler()
        # The APScheduler id is truncated, so the listener needs the map to
        # reach the tasks/history tables at all.
        scheduler._db_task_ids["db_7e401adf"] = "7e401adf-full-task-id"

        db = MagicMock()
        with patch("olano.scheduler.db.SchedulerDB", return_value=db):
            scheduler._record_skipped_run(
                "db_7e401adf", "skipped: the previous run was still executing"
            )

        db.add_history.assert_called_once()
        assert db.add_history.call_args[0][0] == "7e401adf-full-task-id"
        assert "previous run" in db.add_history.call_args[1]["error"]
        assert EVENT_JOB_MAX_INSTANCES  # event constant exists

    def test_config_job_skip_records_under_its_synthetic_id(self) -> None:
        scheduler = OlanoScheduler()
        with patch("olano.scheduler.cron._record_config_run") as record:
            scheduler._record_skipped_run("cfg:maya-ops:0", "skipped: whatever")

        record.assert_called_once_with("cfg:maya-ops:0", error="skipped: whatever")

    def test_unknown_job_id_is_ignored_not_raised(self) -> None:
        scheduler = OlanoScheduler()
        # A job removed between the skip and the callback must not raise inside
        # the scheduler's own thread.
        scheduler._record_skipped_run("db_deadbeef", "skipped: gone")

    def test_removing_a_task_drops_its_id_mapping(self) -> None:
        scheduler = OlanoScheduler()
        scheduler._db_task_ids["db_7e401adf"] = "7e401adf-full-task-id"
        scheduler.remove_db_task("7e401adf-full-task-id")
        assert "db_7e401adf" not in scheduler._db_task_ids

    def test_max_instances_is_pinned_explicitly(self) -> None:
        # Same value as APScheduler's default — pinned so a future default
        # change can't silently allow two copies of one agent task to race.
        scheduler = OlanoScheduler()
        assert scheduler._scheduler._job_defaults["max_instances"] == 1


class TestPerTaskModel:
    """A scheduled job may pin its own model; the run is dispatched on it."""

    @staticmethod
    def _sched(**kw):
        from olano.config.agent_config import ScheduleConfig

        return ScheduleConfig(**kw)

    def test_schedule_entry_accepts_a_model(self) -> None:
        entry = self._sched(cron="0 9 * * *", message="m", model="olano:pro")
        assert entry.model == "olano:pro"
        assert self._sched(cron="0 9 * * *", message="m").model is None

    def test_descriptor_carries_the_entry_model(self, base_config: AgentConfig) -> None:
        sched = OlanoScheduler()
        plain = sched._describe_config_job(
            base_config.name, 0, self._sched(cron="* * * * *", message="m")
        )
        pinned = sched._describe_config_job(
            base_config.name, 1, self._sched(cron="* * * * *", message="m", model=" olano:max ")
        )
        assert plain["model"] == ""
        assert pinned["model"] == "olano:max"

    def test_config_job_runs_on_its_own_model(self, base_config: AgentConfig) -> None:
        """The entry's model reaches `ainvoke(override_model=…)`; an entry
        without one passes no override at all, so a runner that predates the
        kwarg (fakes, shims) keeps working."""
        base_config.schedule = [
            self._sched(cron="0 10 * * *", message="deep", model="olano:max"),
            self._sched(cron="0 11 * * *", message="plain"),
        ]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="ok")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched.register_agent(base_config, runner, workspace_root=None)
        with patch("olano.scheduler.cron._record_config_run"):
            ok, _ = sched.run_config_job_now(f"cfg:{base_config.name}:0")
            assert ok is True
            ok, _ = sched.run_config_job_now(f"cfg:{base_config.name}:1")
            assert ok is True
        pinned, plain = runner.ainvoke.call_args_list
        assert pinned.args[0].endswith("\n\ndeep")
        assert pinned.kwargs["override_model"] == "olano:max"
        assert plain.args[0].endswith("\n\nplain")
        assert "override_model" not in plain.kwargs

    async def test_cron_fire_passes_the_entry_model(self, base_config: AgentConfig) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="deep", model="olano:pro")]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, runner, workspace_root=None)
        run = next(
            c.args[0]
            for c in add_job.call_args_list
            if c.kwargs.get("id") == f"cfg:{base_config.name}:0"
        )
        with patch("olano.scheduler.cron._record_config_run"):
            await run()
        assert runner.ainvoke.call_args.kwargs["override_model"] == "olano:pro"

    async def test_db_task_runs_on_its_own_model(self, tmp_path: Path) -> None:
        from olano.scheduler.db import SchedulerDB

        db = SchedulerDB(db_path=tmp_path / "sched.db")
        pinned = db.create_task(
            "deep", "analyse", schedule="0 9 * * *", agent_name="a1", model="olano:max"
        )
        plain = db.create_task("digest", "summarise", schedule="0 2 * * *", agent_name="a1")
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        sched._runners["a1"] = runner

        await sched._execute_db_task(db.get_task(pinned), db)
        await sched._execute_db_task(db.get_task(plain), db)

        first, second = runner.ainvoke.call_args_list
        assert first.kwargs["override_model"] == "olano:max"
        assert "override_model" not in second.kwargs
        assert db.get_history(pinned)[0]["result"] == "done"

    async def test_db_task_on_an_unbuildable_model_records_the_reason(self, tmp_path: Path) -> None:
        """No silent fallback: the history row names the model that failed."""
        from olano.scheduler.db import SchedulerDB

        db = SchedulerDB(db_path=tmp_path / "sched.db")
        task_id = db.create_task(
            "deep", "analyse", schedule="0 9 * * *", agent_name="a1", model="ghost:x"
        )
        runner = MagicMock()
        runner.ainvoke = AsyncMock(
            side_effect=RuntimeError("Could not load model 'ghost:x': no key")
        )
        sched = OlanoScheduler()
        sched._runners["a1"] = runner

        await sched._execute_db_task(db.get_task(task_id), db)

        history = db.get_history(task_id)
        assert history and "ghost:x" in str(history[0]["error"])


class TestPerJobReasoningEffort:
    """A job's `reasoning_effort` reaches the runner as the per-turn override the
    chat's effort chip sets, on every dispatch path, beside its `model`; an unset
    one passes no kwarg at all (same rule as the model), so a runner that
    predates the kwarg keeps working."""

    @staticmethod
    def _sched(**kw: Any) -> Any:
        from olano.config.agent_config import ScheduleConfig

        return ScheduleConfig(**kw)

    def test_schedule_entry_accepts_an_effort(self) -> None:
        entry = self._sched(cron="0 9 * * *", message="m", reasoning_effort="high")
        assert entry.reasoning_effort == "high"
        assert self._sched(cron="0 9 * * *", message="m").reasoning_effort is None

    def test_descriptor_carries_the_entry_effort_lower_cased(
        self, base_config: AgentConfig
    ) -> None:
        sched = OlanoScheduler()
        plain = sched._describe_config_job(
            base_config.name, 0, self._sched(cron="* * * * *", message="m")
        )
        pinned = sched._describe_config_job(
            base_config.name,
            1,
            self._sched(cron="* * * * *", message="m", reasoning_effort=" HIGH "),
        )
        assert plain["reasoning_effort"] == ""
        assert pinned["reasoning_effort"] == "high"

    async def test_config_cron_fire_forwards_both_overrides(self, base_config: AgentConfig) -> None:
        base_config.schedule = [
            self._sched(
                cron="0 10 * * *", message="do it", model="olano:pro", reasoning_effort="high"
            )
        ]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, runner, workspace_root=None)
        fire = next(
            c.args[0]
            for c in add_job.call_args_list
            if c.kwargs.get("id") == f"cfg:{base_config.name}:0"
        )
        with patch("olano.scheduler.cron._record_config_run"):
            await fire()
        kwargs = runner.ainvoke.call_args.kwargs
        assert kwargs["override_model"] == "olano:pro"
        assert kwargs["override_reasoning_effort"] == "high"

    async def test_config_cron_fire_without_overrides_passes_no_kwargs(
        self, base_config: AgentConfig
    ) -> None:
        base_config.schedule = [self._sched(cron="0 10 * * *", message="do it")]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, runner, workspace_root=None)
        fire = next(
            c.args[0]
            for c in add_job.call_args_list
            if c.kwargs.get("id") == f"cfg:{base_config.name}:0"
        )
        with patch("olano.scheduler.cron._record_config_run"):
            await fire()
        kwargs = runner.ainvoke.call_args.kwargs
        assert "override_model" not in kwargs
        assert "override_reasoning_effort" not in kwargs

    def test_run_now_forwards_the_entry_effort(self, base_config: AgentConfig) -> None:
        base_config.schedule = [
            self._sched(cron="0 10 * * *", message="do it", reasoning_effort="low")
        ]
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="ok")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job"):
            sched.register_agent(base_config, runner, workspace_root=None)
        with patch("olano.scheduler.cron._record_config_run"):
            ok, _ = sched.run_config_job_now(f"cfg:{base_config.name}:0")
        assert ok is True
        kwargs = runner.ainvoke.call_args.kwargs
        assert "override_model" not in kwargs  # no model on the entry
        assert kwargs["override_reasoning_effort"] == "low"

    async def test_db_task_fire_forwards_the_row_effort(self, tmp_path: Path) -> None:
        from olano.scheduler.db import SchedulerDB

        db = SchedulerDB(db_path=tmp_path / "sched.db")
        task_id = db.create_task(
            "report",
            "write the report",
            schedule="0 9 * * 1",
            agent_name="a1",
            model="openai:gpt-6.1-sol",
            reasoning_effort="xhigh",
        )
        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="done")
        sched = OlanoScheduler()
        sched._runners["a1"] = runner
        await sched._execute_db_task(db.get_task(task_id), db)
        kwargs = runner.ainvoke.call_args.kwargs
        assert kwargs["override_model"] == "openai:gpt-6.1-sol"
        assert kwargs["override_reasoning_effort"] == "xhigh"
        assert db.get_history(task_id)[0]["result"] == "done"
