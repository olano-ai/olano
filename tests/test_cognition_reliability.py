"""Reliability guard rails for background runs: retry policy, failure streak,
fleet stagger and the scheduler's phase re-fire."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.cognition import reliability
from olano.cognition.orchestrator import PHASE_BY_ID, cadence_for
from olano.config.agent_config import AgentConfig, CognitionConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.scheduler.cron import OlanoScheduler, _stagger_cron, phase_cron_for


def _config(tmp_path: Path, name: str = "orch", **cog: object) -> AgentConfig:
    return AgentConfig(
        name=name,
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        cognition=CognitionConfig(**cog),  # type: ignore[arg-type]
    )


# ------------------------------------------------------------- retry policy


def test_retry_delays_default_are_minute_scale(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_COGNITION_RETRIES", raising=False)
    monkeypatch.delenv("LIONCLAW_COGNITION_BACKOFF", raising=False)
    delays = reliability.retry_delays()
    assert len(delays) == reliability.DEFAULT_RETRY_ATTEMPTS - 1
    assert all(d >= 60 for d in delays)


def test_retry_delays_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_COGNITION_RETRIES", "4")
    monkeypatch.setenv("LIONCLAW_COGNITION_BACKOFF", "10,20")
    assert reliability.retry_delays() == (10.0, 20.0, 20.0)
    monkeypatch.setenv("LIONCLAW_COGNITION_RETRIES", "1")
    assert reliability.retry_delays() == ()
    monkeypatch.setenv("LIONCLAW_COGNITION_RETRIES", "lots")
    monkeypatch.setenv("LIONCLAW_COGNITION_BACKOFF", "soon")
    assert (
        reliability.retry_delays()
        == reliability.DEFAULT_RETRY_BACKOFF_SECONDS[: reliability.DEFAULT_RETRY_ATTEMPTS - 1]
    )


def test_only_self_healing_codes_are_retryable() -> None:
    assert reliability.is_retryable_code("rate_limited")
    assert reliability.is_retryable_code("overloaded")
    for code in ("billing_limit", "timeout", "invalid_api_key", "step_budget_exhausted", "", None):
        assert not reliability.is_retryable_code(code)


def test_jitter_stays_within_a_quarter() -> None:
    for _ in range(50):
        assert 75.0 <= reliability.jittered(100.0) <= 125.0


# ----------------------------------------------------------- failure streak


def test_streak_records_and_clears(tmp_path: Path) -> None:
    assert reliability.failure_streak(tmp_path, "memory_garden") == 0
    assert reliability.record_failure(tmp_path, "memory_garden") == 1
    assert reliability.record_failure(tmp_path, "memory_garden") == 2
    assert reliability.failure_streak(tmp_path, "memory_garden") == 2
    assert reliability.failure_since(tmp_path, "memory_garden")
    # Other labels are independent.
    assert reliability.failure_streak(tmp_path, "pulse_agent") == 0
    reliability.clear_failures(tmp_path, "memory_garden")
    assert reliability.failure_streak(tmp_path, "memory_garden") == 0
    assert reliability.failure_since(tmp_path, "memory_garden") is None


def test_streak_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / ".olano" / "state" / "cognition" / "worker_failures.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert reliability.failure_streak(tmp_path, "x") == 0
    assert reliability.record_failure(tmp_path, "x") == 1


def test_holds_for_streak_rules(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_COGNITION_BELL_STREAK", raising=False)
    retry = {"kind": "retry", "summary": "Wait."}
    assert reliability.holds_for_streak("error", retry, 1) is True
    assert reliability.holds_for_streak("error", retry, 2) is False
    assert reliability.holds_for_streak("error", None, 1) is True
    # Anything the operator must fix rings on the first run.
    for kind in ("reconnect_provider", "set_api_key", "top_up_billing", "unlock_vault"):
        assert reliability.holds_for_streak("error", {"kind": kind}, 1) is False
    assert reliability.holds_for_streak("needs_setup", retry, 1) is False
    monkeypatch.setenv("LIONCLAW_COGNITION_BELL_STREAK", "3")
    assert reliability.holds_for_streak("error", retry, 2) is True
    assert reliability.holds_for_streak("error", retry, 3) is False


# ------------------------------------------------------------ fleet stagger


def test_stagger_is_deterministic_and_spreads_agents() -> None:
    a = _stagger_cron("30 4 * * *", "alpha:memory_garden")
    assert a == _stagger_cron("30 4 * * *", "alpha:memory_garden")
    b = _stagger_cron("30 4 * * *", "beta:memory_garden")
    minutes = {int(x.split()[0]) for x in (a, b)}
    assert len(minutes) == 2, "two agents must not share a minute"
    assert all(0 <= m < 60 for m in minutes)
    # Only the minute moves; the hour and the rest stay.
    assert a.split()[1:] == ["4", "*", "*", "*"]


def test_stagger_leaves_multi_valued_minute_fields_alone() -> None:
    for expr in ("*/5 * * * *", "0,30 4 * * *", "0-10 4 * * *", "* 4 * * *"):
        assert _stagger_cron(expr, "any") == expr
    assert _stagger_cron("not a cron", "any") == "not a cron"


def test_phase_cron_prefers_an_explicit_override_verbatim(tmp_path: Path) -> None:
    phase = PHASE_BY_ID["memory_garden"]
    cfg = _config(tmp_path, memory_garden_cron="7 3 * * *")
    assert phase_cron_for(cfg, phase, "aggressive") == "7 3 * * *"


def test_phase_cron_falls_through_to_the_intensity_preset(tmp_path: Path) -> None:
    """The bug this guards: a concrete default on the override field made the
    preset unreachable, so `intensity` never moved these phases."""
    phase = PHASE_BY_ID["pulse"]
    cfg = _config(tmp_path)
    for intensity in ("relaxed", "balanced", "aggressive"):
        got = phase_cron_for(cfg, phase, intensity)
        want = cadence_for(phase, intensity)
        assert got.split()[1:] == want.split()[1:], intensity
    assert phase_cron_for(cfg, phase, "relaxed") != phase_cron_for(cfg, phase, "aggressive")


def test_scheduler_registers_agents_on_different_minutes(tmp_path: Path) -> None:
    sched = OlanoScheduler()
    minutes = []
    for name in ("alpha", "beta", "gamma"):
        # Opted in: the engine is off by default and nothing registers without it.
        cfg = _config(tmp_path, name=name, enabled=True)
        sched._add_cognition_jobs(cfg, MagicMock(), tmp_path)
        job = sched._scheduler.get_job(f"{name}_cortex_memory_garden")
        minutes.append(str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")]))
    assert len(set(minutes)) == 3


# ---------------------------------------------------------- phase re-fire


def _job_ids(sched: OlanoScheduler) -> set[str]:
    return {j.id for j in sched._scheduler.get_jobs()}


def test_transient_failure_schedules_one_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_COGNITION_RETRIES", raising=False)
    monkeypatch.delenv("LIONCLAW_COGNITION_BACKOFF", raising=False)
    sched = OlanoScheduler()
    cfg = _config(tmp_path)
    made: list[tuple[str, int]] = []

    def make_job(pid: str, attempt: int = 0):
        made.append((pid, attempt))
        return lambda: None

    ok = sched._maybe_schedule_cognition_retry(
        cfg,
        "memory_garden",
        {"status": "error", "code": "rate_limited"},
        attempt=0,
        make_job=make_job,
    )
    assert ok is True
    assert made == [("memory_garden", 1)]
    assert "orch_cortex_memory_garden_retry" in _job_ids(sched)
    # A second failure on the same firing replaces, never stacks.
    sched._maybe_schedule_cognition_retry(
        cfg,
        "memory_garden",
        {"status": "error", "code": "rate_limited"},
        attempt=1,
        make_job=make_job,
    )
    assert [j for j in _job_ids(sched) if j.endswith("_retry")] == [
        "orch_cortex_memory_garden_retry"
    ]


def test_non_transient_failure_schedules_nothing(tmp_path: Path) -> None:
    sched = OlanoScheduler()
    cfg = _config(tmp_path)
    for result in (
        {"status": "error", "code": "billing_limit"},
        {"status": "error", "code": "invalid_api_key"},
        {"status": "completed", "code": ""},
        {"status": "skipped", "code": ""},
    ):
        assert (
            sched._maybe_schedule_cognition_retry(
                cfg, "memory_garden", result, attempt=0, make_job=lambda *a, **k: (lambda: None)
            )
            is False
        )
    assert not [j for j in _job_ids(sched) if j.endswith("_retry")]


def test_retries_give_up_after_the_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_COGNITION_RETRIES", "2")
    sched = OlanoScheduler()
    cfg = _config(tmp_path)
    result = {"status": "error", "code": "overloaded"}
    mk = lambda *a, **k: (lambda: None)  # noqa: E731
    assert sched._maybe_schedule_cognition_retry(cfg, "pulse", result, attempt=0, make_job=mk)
    assert not sched._maybe_schedule_cognition_retry(cfg, "pulse", result, attempt=1, make_job=mk)


def test_retry_yields_to_a_sooner_regular_firing(tmp_path: Path, monkeypatch) -> None:
    sched = OlanoScheduler()
    cfg = _config(tmp_path)
    soon = MagicMock()
    soon.next_run_time = datetime.now(timezone.utc) + timedelta(minutes=1)
    monkeypatch.setattr(sched._scheduler, "get_job", lambda _id: soon)
    ok = sched._maybe_schedule_cognition_retry(
        cfg,
        "memory_garden",
        {"status": "error", "code": "rate_limited"},
        attempt=0,
        make_job=lambda *a, **k: (lambda: None),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_registered_job_refires_then_stops(tmp_path: Path, monkeypatch) -> None:
    """End to end through the real job closure: fail → retry job → fail →
    second retry → fail → no third (default budget is 3 attempts)."""
    monkeypatch.delenv("LIONCLAW_COGNITION_RETRIES", raising=False)
    monkeypatch.delenv("LIONCLAW_COGNITION_BACKOFF", raising=False)
    sched = OlanoScheduler()
    cfg = _config(tmp_path, enabled=True)  # opted in: the engine is off by default
    sched._add_cognition_jobs(cfg, MagicMock(), tmp_path)
    failing = AsyncMock(return_value={"status": "error", "code": "rate_limited", "id": "x"})
    with patch("olano.cognition.orchestrator.run_phase", failing):
        await sched._scheduler.get_job("orch_cortex_memory_garden").func()
        retry_id = "orch_cortex_memory_garden_retry"
        assert retry_id in _job_ids(sched)
        await sched._scheduler.get_job(retry_id).func()
        assert retry_id in _job_ids(sched)  # replaced with attempt 2
        await sched._scheduler.get_job(retry_id).func()
        # attempt 2 of a 3-attempt budget: nothing further.
        assert failing.await_count == 3
    healed = AsyncMock(return_value={"status": "completed", "code": "", "id": "x"})
    with patch("olano.cognition.orchestrator.run_phase", healed):
        await sched._scheduler.get_job(retry_id).func()
    assert healed.await_count == 1


def test_unregister_drops_pending_retries(tmp_path: Path) -> None:
    sched = OlanoScheduler()
    cfg = _config(tmp_path)
    sched._jobs[cfg.name] = []
    sched._maybe_schedule_cognition_retry(
        cfg,
        "memory_garden",
        {"status": "error", "code": "rate_limited"},
        attempt=0,
        make_job=lambda *a, **k: (lambda: None),
    )
    assert "orch_cortex_memory_garden_retry" in _job_ids(sched)
    sched.unregister_agent(cfg.name)
    assert "orch_cortex_memory_garden_retry" not in _job_ids(sched)
