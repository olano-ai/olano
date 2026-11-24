"""Tests for Phase 8 orchestration: scheduler jobs, drainer, installer, config."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.config.agent_config import AgentConfig, CognitionConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.config.system_settings import set_cortex_master

# Everything here is about what the engine does once it runs, and it is off by
# default. The default itself is pinned by the tests that turn it back off.
pytestmark = pytest.mark.usefixtures("cortex_on")


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        name="orch",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )


def test_cognition_config_defaults() -> None:
    cfg = CognitionConfig()
    # Every on/off field is three-state: an agent that was never configured has
    # no opinion, so `enabled` is None (inherit), not True. The *resolved*
    # answer follows the global master switch, which is off by default: see
    # test_scheduler_registers_no_cognition_jobs_while_the_engine_is_off.
    assert cfg.enabled is None
    assert cfg.autonomous is None
    assert cfg.auto_approve is None
    # The four legacy cron fields are three-state too: None ⇒ the intensity
    # preset's cadence, staggered per agent. A concrete default here made the
    # preset unreachable and pinned every agent to the same minute.
    assert cfg.memory_garden_cron is None
    assert cfg.pulse_agent_cron is None
    assert cfg.workspace_optimize_cron is None
    assert cfg.skill_librarian_cron is None
    assert cfg.max_deep_runs_per_day == 12
    assert cfg.enable_skillsmith is True


def test_scheduler_registers_cognition_jobs(tmp_path: Path) -> None:
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    sched = OlanoScheduler()
    job_ids = sched._add_cognition_jobs(config, MagicMock(), tmp_path)
    # The autonomous Cortex engine registers one job per enabled phase.
    for phase in (
        "memory_garden",
        "pulse",
        "workspace_optimize",
        "skill_librarian",
        "brainstorm",
        "ideas",
        "prototype",
        "improve",
        "checks",
        "skills",
    ):
        assert any(f"cortex_{phase}" in j for j in job_ids), f"missing cortex job for {phase}"


def test_scheduler_registers_cognition_jobs_for_an_inheriting_agent(tmp_path: Path) -> None:
    """Regression: `register_agent` gated on a bare truthiness read of
    `config.cognition.enabled`. Once that field became three-state, None
    (inherit — which is every agent nobody has configured) read as falsy and
    the whole fleet silently lost its cognition jobs. It must go through
    `cortex_enabled`, which resolves inherit against the global."""
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    assert config.cognition.enabled is None, "this test is about the inherit case"

    sched = OlanoScheduler()
    sched.register_agent(config, MagicMock(), workspace_root=tmp_path)
    assert any("cortex_" in j for j in sched._jobs[config.name]), (
        "an agent that inherits an (on) global master switch must get cognition jobs"
    )

    # And the explicit per-agent off is still honoured.
    other = _config(tmp_path)
    other.name = f"{config.name}_off"
    other.cognition.enabled = False
    sched.register_agent(other, MagicMock(), workspace_root=tmp_path)
    assert not any("cortex_" in j for j in sched._jobs[other.name])


def test_scheduler_registers_no_cognition_jobs_while_the_engine_is_off(tmp_path: Path) -> None:
    """The default: a box that never turned the engine on schedules no Cortex
    work for any inheriting agent, while an agent that opts in still gets it."""
    from olano.scheduler.cron import OlanoScheduler

    set_cortex_master(False)
    config = _config(tmp_path)
    sched = OlanoScheduler()
    sched.register_agent(config, MagicMock(), workspace_root=tmp_path)
    assert not any("cortex_" in j for j in sched._jobs[config.name])

    pilot = _config(tmp_path)
    pilot.name = f"{config.name}_pilot"
    pilot.cognition.enabled = True
    sched.register_agent(pilot, MagicMock(), workspace_root=tmp_path)
    assert any("cortex_" in j for j in sched._jobs[pilot.name])


def test_scheduler_skips_cognition_when_feature_off(tmp_path: Path) -> None:
    from olano.config.system_settings import FEATURES
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    # Disable every per-agent feature override → no phase is eligible.
    for feature in FEATURES:
        setattr(config.cognition, feature, False)
    sched = OlanoScheduler()
    job_ids = sched._add_cognition_jobs(config, MagicMock(), tmp_path)
    assert job_ids == []


def test_scheduler_skips_cognition_when_autonomy_off(tmp_path: Path) -> None:
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    # Master autonomy off → manual-only, no background jobs (features still ON).
    config.cognition.autonomous = False
    sched = OlanoScheduler()
    job_ids = sched._add_cognition_jobs(config, MagicMock(), tmp_path)
    assert job_ids == []


def test_resync_agent_cognition_jobs_toggles_live(tmp_path: Path) -> None:
    """Flipping cognition.enabled and re-syncing removes/re-adds the agent's
    cortex jobs without a restart — the write path the dashboard toggle uses."""
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    sched = OlanoScheduler()
    sched.register_agent(config, MagicMock(), tmp_path)
    assert any("_cortex_" in j for j in sched._jobs["orch"]), "expected cortex jobs after register"

    # Disable Cortex for this agent → resync drops every cortex job it tracks.
    config.cognition.enabled = False
    ok, _msg = sched.resync_agent_cognition_jobs(config)
    assert ok is True
    assert not any("_cortex_" in j for j in sched._jobs["orch"])
    assert sched._scheduler.get_job("orch_cortex_pulse") is None

    # Re-enable → resync rebuilds them from the phase registry.
    config.cognition.enabled = True
    ok, _msg = sched.resync_agent_cognition_jobs(config)
    assert ok is True
    assert any("_cortex_" in j for j in sched._jobs["orch"])


def test_resync_agent_cognition_jobs_prunes_one_feature(tmp_path: Path) -> None:
    """A per-feature override re-sync drops only that phase's job."""
    from olano.scheduler.cron import OlanoScheduler

    config = _config(tmp_path)
    sched = OlanoScheduler()
    sched.register_agent(config, MagicMock(), tmp_path)
    assert any("cortex_brainstorm" in j for j in sched._jobs["orch"])

    config.cognition.brainstorming = False
    ok, _msg = sched.resync_agent_cognition_jobs(config)
    assert ok is True
    assert not any("cortex_brainstorm" in j for j in sched._jobs["orch"])
    # Other phases survive.
    assert any("cortex_pulse" in j for j in sched._jobs["orch"])


async def test_drainer_runs_queued_job_and_posts_pulse(tmp_path: Path) -> None:
    from olano.cognition.worker import WorkerOutcome
    from olano.core.runner import AgentRunner
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane
    from olano.runtime.server import RuntimeServer

    config = _config(tmp_path)
    # Seed a queued job descriptor.
    jobs_dir = tmp_path / ".olano" / "state" / "cognition" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "job1.json").write_text(
        json.dumps(
            {
                "id": "job1",
                "agent": "orch",
                "item_id": "x",
                "action": {"type": "run_worker", "playbook": "idea_expand", "goal": "g"},
                "status": "queued",
            }
        ),
        encoding="utf-8",
    )

    server = RuntimeServer(agent_names=[])
    server._runners["orch"] = AgentRunner(MagicMock(), config)

    outcome = WorkerOutcome(
        status="completed", summary="wrote it", artifacts=["projects/ideas/x.md"]
    )
    with patch(
        "olano.cognition.pulse_agent.run_pulse_worker_action",
        new=AsyncMock(return_value=(outcome, "backup-1")),
    ):
        await server._drain_cognition_jobs()

    descriptor = json.loads((jobs_dir / "job1.json").read_text())
    assert descriptor["status"] == "done"
    # A follow-up FINDING pulse was posted with the artifact reference + undo action.
    engine = PulseEngine(config, tmp_path / ".olano")
    items = engine.list_pending(lane=PulseLane.PULSE)
    assert any("projects/ideas/x.md" in (it.references or []) for it in items)
    assert any((it.action or {}).get("type") == "restore_backup" for it in items)


def test_installer_scaffolds_cognition_skills(tmp_path: Path) -> None:
    from olano.archetypes.installer import _scaffold_system_static_files

    _scaffold_system_static_files(tmp_path, overwrite=True)
    core = tmp_path / "system" / "skills" / "core"
    for name in ("memory-gardener", "pulse-agent", "skillsmith", "prototyper"):
        skill = core / name / "SKILL.md"
        assert skill.exists(), f"{name} not scaffolded"
        assert "tier: subagent" in skill.read_text(encoding="utf-8")


# ───────────────────────── Fully Autonomous (auto-approve) ───────────────────


def test_auto_approve_enabled_defaults_on(tmp_path: Path) -> None:
    from olano.config.system_settings import auto_approve_enabled

    # Engine on, nothing else configured → auto-approve defaults ON.
    assert auto_approve_enabled(_config(tmp_path)) is True


def test_auto_approve_is_off_while_the_engine_is(tmp_path: Path) -> None:
    from olano.config.system_settings import auto_approve_enabled

    # A fresh home: the engine is off, so there is nothing to auto-approve.
    set_cortex_master(False)
    assert auto_approve_enabled(_config(tmp_path)) is False


def test_set_auto_approve_round_trip(tmp_path: Path) -> None:
    from olano.config.system_settings import auto_approve_enabled, set_auto_approve

    set_auto_approve(False)
    assert auto_approve_enabled(_config(tmp_path)) is False
    set_auto_approve(True)
    assert auto_approve_enabled(_config(tmp_path)) is True


def test_auto_approve_respects_per_agent_override(tmp_path: Path) -> None:
    from olano.config.system_settings import auto_approve_enabled

    cfg = AgentConfig(
        name="orch",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        cognition=CognitionConfig(auto_approve=False),
    )
    assert auto_approve_enabled(cfg) is False  # per-agent off overrides global on


def test_auto_approve_cognition_items_keeps_learned_skill(tmp_path: Path) -> None:
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane, PulseStatus, PulseType

    set_auto_approve(True)
    cfg = _config(tmp_path)
    engine = PulseEngine(cfg, tmp_path / ".olano")
    engine.add(
        content="Learned a new skill.",
        source="self_learning",
        lane=PulseLane.APPROVAL,
        type=PulseType.PROPOSAL,
        title="New skill: foo",
        metadata={"kind": "learned_skill", "skill_name": "foo"},
        dedup_window_hours=0.0,
    )
    approved = auto_approve_cognition_items(cfg, tmp_path)
    assert approved == 1
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert fresh.list_pending(lane=PulseLane.APPROVAL) == []
    kept = fresh.list_by_status(PulseStatus.APPROVED, lane=PulseLane.APPROVAL)
    assert kept and kept[0].metadata.get("auto_approved") is True


def test_auto_approve_noop_when_disabled(tmp_path: Path) -> None:
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane, PulseType

    set_auto_approve(False)
    cfg = _config(tmp_path)
    engine = PulseEngine(cfg, tmp_path / ".olano")
    engine.add(
        content="Learned a new skill.",
        source="self_learning",
        lane=PulseLane.APPROVAL,
        type=PulseType.PROPOSAL,
        title="New skill: bar",
        metadata={"kind": "learned_skill"},
        dedup_window_hours=0.0,
    )
    assert auto_approve_cognition_items(cfg, tmp_path) == 0
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert len(fresh.list_pending(lane=PulseLane.APPROVAL)) == 1  # still waiting


def test_auto_approve_leaves_memory_reorganization_for_human(tmp_path: Path) -> None:
    # A cognition-sourced APPROVAL item with a NON-whitelisted kind and no action
    # must NOT be silently auto-approved (it would just be consumed as a noop).
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane, PulseType

    set_auto_approve(True)
    cfg = _config(tmp_path)
    engine = PulseEngine(cfg, tmp_path / ".olano")
    engine.add(
        content="Reorganize memory files.",
        source="self_learning",
        lane=PulseLane.APPROVAL,
        type=PulseType.PROPOSAL,
        title="Review memory organization proposal",
        metadata={"kind": "memory_reorganization"},  # not in the auto-approve whitelist
        dedup_window_hours=0.0,
    )
    assert auto_approve_cognition_items(cfg, tmp_path) == 0
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert len(fresh.list_pending(lane=PulseLane.APPROVAL)) == 1  # still waiting for a human


# ───────────────── cognition.approval_required — the sweep's hard stop ────────


def _self_improvement(engine, *, path: str, title: str = "Improve") -> None:
    """A self-improve proposal shaped exactly like ``generators`` emits them:
    an APPROVAL-lane ``append_lines`` on ``path`` with a ``target_file`` hint."""
    from olano.pulse.models import PulseLane, PulseType

    engine.add(
        content=f"Proposed addition to {path}.",
        source="self_learning",
        lane=PulseLane.APPROVAL,
        type=PulseType.PROPOSAL,
        title=title,
        action={"type": "append_lines", "path": path, "lines": [f"- Learned for {title}"]},
        metadata={"kind": "self_improvement", "target_file": path},
        dedup_window_hours=0.0,
    )


def test_default_approval_required_matches_the_guard_vocabulary() -> None:
    # The config default, the classifier's vocabulary and the user guide name
    # the same three categories; a drift here is a docs lie.
    from olano.cognition.approval_guard import KNOWN_CATEGORIES
    from olano.config.agent_config import DEFAULT_APPROVAL_REQUIRED

    assert set(DEFAULT_APPROVAL_REQUIRED) == set(KNOWN_CATEGORIES)
    assert CognitionConfig().approval_required == list(DEFAULT_APPROVAL_REQUIRED)
    guide = (
        Path(__file__).resolve().parents[1] / "olano" / "archetypes" / "knowledge_user_guide.md"
    ).read_text(encoding="utf-8")
    assert "approval_required" in guide
    for name in DEFAULT_APPROVAL_REQUIRED:
        assert name in guide


def test_approval_required_categories_resolves_default_override_and_malformed(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from olano.config.agent_config import DEFAULT_APPROVAL_REQUIRED
    from olano.config.system_settings import approval_required_categories

    assert approval_required_categories(None) == frozenset(DEFAULT_APPROVAL_REQUIRED)
    assert approval_required_categories(_config(tmp_path)) == frozenset(DEFAULT_APPROVAL_REQUIRED)
    narrowed = AgentConfig(
        name="orch",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        cognition=CognitionConfig(approval_required=[" Identity_Edit "]),
    )
    assert approval_required_categories(narrowed) == {"identity_edit"}
    opted_out = AgentConfig(
        name="orch",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        cognition=CognitionConfig(approval_required=[]),
    )
    assert approval_required_categories(opted_out) == frozenset()
    # A malformed value (a bare string reached the field somehow) fails safe to
    # the default rather than to "hold nothing".
    malformed = SimpleNamespace(cognition=SimpleNamespace(approval_required="identity_edit"))
    assert approval_required_categories(malformed) == frozenset(DEFAULT_APPROVAL_REQUIRED)


def test_auto_approve_holds_identity_edit_by_default(tmp_path: Path) -> None:
    # Fully Autonomous ON, default approval_required: a self-improve proposal
    # that would append to SOUL.md stays in the queue for a human and SOUL.md
    # is untouched. Before the guard was wired this was applied on the spot.
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane

    set_auto_approve(True)
    cfg = _config(tmp_path)
    (tmp_path / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    engine = PulseEngine(cfg, tmp_path / ".olano")
    _self_improvement(engine, path="SOUL.md", title="soul")
    assert auto_approve_cognition_items(cfg, tmp_path) == 0
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    pending = fresh.list_pending(lane=PulseLane.APPROVAL)
    assert len(pending) == 1
    assert pending[0].metadata.get("auto_approved") is not True
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "# Soul\n"


def test_auto_approve_applies_memory_self_improvement(tmp_path: Path) -> None:
    # MEMORY.md is memory, not identity: the same proposal shape targeting it is
    # swept and executed — the line lands in the file.
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane

    set_auto_approve(True)
    cfg = _config(tmp_path)
    (tmp_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    engine = PulseEngine(cfg, tmp_path / ".olano")
    _self_improvement(engine, path="MEMORY.md", title="memory")
    assert auto_approve_cognition_items(cfg, tmp_path) == 1
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert fresh.list_pending(lane=PulseLane.APPROVAL) == []
    assert "Learned for memory" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_auto_approve_empty_approval_required_applies_identity_edit(tmp_path: Path) -> None:
    # The owner's opt-out: approval_required: [] lets Fully Autonomous rewrite
    # identity files too.
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane

    set_auto_approve(True)
    cfg = AgentConfig(
        name="orch",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        cognition=CognitionConfig(approval_required=[]),
    )
    (tmp_path / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    engine = PulseEngine(cfg, tmp_path / ".olano")
    _self_improvement(engine, path="SOUL.md", title="soul")
    assert auto_approve_cognition_items(cfg, tmp_path) == 1
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert fresh.list_pending(lane=PulseLane.APPROVAL) == []
    assert "Learned for soul" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


def test_auto_approve_holds_composite_that_touches_identity(tmp_path: Path) -> None:
    # A PULSE-lane composite is executable, so the sweep would apply it — unless
    # any step writes an identity file, in which case the whole item waits.
    from olano.cognition.orchestrator import auto_approve_cognition_items
    from olano.config.system_settings import set_auto_approve
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane, PulseType

    set_auto_approve(True)
    cfg = _config(tmp_path)
    (tmp_path / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    engine = PulseEngine(cfg, tmp_path / ".olano")
    engine.add(
        content="Two-step tidy-up.",
        source="cognition",
        lane=PulseLane.PULSE,
        type=PulseType.SUGGESTION,
        title="Tidy",
        action={
            "type": "composite",
            "steps": [
                {"type": "append_text", "path": "MEMORY.md", "text": "note\n"},
                {"type": "append_lines", "path": "./IDENTITY.md", "lines": ["- be terse"]},
            ],
        },
        dedup_window_hours=0.0,
    )
    assert auto_approve_cognition_items(cfg, tmp_path) == 0
    fresh = PulseEngine(cfg, tmp_path / ".olano")
    assert len(fresh.list_pending(lane=PulseLane.PULSE)) == 1
    assert (tmp_path / "IDENTITY.md").read_text(encoding="utf-8") == "# Identity\n"
    assert not (tmp_path / "MEMORY.md").exists()  # nothing partial either


def test_phase_running_registry() -> None:
    from olano.cognition.orchestrator import phase_running, running_phases

    assert running_phases("agentA") == []
    with phase_running("agentA", "pulse"):
        assert running_phases("agentA") == ["pulse"]
        with phase_running("agentA", "ideas"):
            assert running_phases("agentA") == ["ideas", "pulse"]  # sorted
        assert running_phases("agentA") == ["pulse"]  # inner cleared
    assert running_phases("agentA") == []  # all cleared, key removed


def test_phase_running_clears_on_exception() -> None:
    from olano.cognition.orchestrator import phase_running, running_phases

    try:
        with phase_running("agentB", "improve"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert running_phases("agentB") == []  # finally cleared it


def test_phase_running_refcounts_overlapping_same_phase() -> None:
    # Two overlapping runs of the SAME phase (e.g. scheduled + manual) must keep
    # the flag lit until the LAST one finishes — not the first.
    from olano.cognition.orchestrator import phase_running, running_phases

    with phase_running("agentC", "workspace_optimize"):
        assert running_phases("agentC") == ["workspace_optimize"]
        with phase_running("agentC", "workspace_optimize"):
            assert running_phases("agentC") == ["workspace_optimize"]
        # inner finished, outer still running → still lit
        assert running_phases("agentC") == ["workspace_optimize"]
    assert running_phases("agentC") == []


# ── failure → actionable status + remediation ────────────────────────────────


def test_classify_cognition_error_full_keeps_vault_remediation(tmp_path: Path) -> None:
    from olano.cognition.orchestrator import classify_cognition_error_full

    code, msg, rem = classify_cognition_error_full(
        "Vault is locked; unlock to use DEEPSEEK_API_KEY (a credential)", None
    )
    assert code == "vault_locked"
    assert rem and rem["action"]["type"] == "unlock_vault"
    assert "DEEPSEEK_API_KEY" in rem["summary"]


def test_classify_cognition_error_full_routes_oauth_to_reconnect(tmp_path: Path) -> None:
    """An expired OpenAI-Codex sign-in → reconnect remediation (one-click)."""
    from olano.cognition.orchestrator import classify_cognition_error_full

    cfg = _config(tmp_path).model_copy(update={"model": "openai-codex:gpt-5.4-mini"})
    code, msg, rem = classify_cognition_error_full(
        "Error: your authentication token has been invalidated, please try signing in again",
        cfg,
    )
    assert code == "provider_auth_expired"
    assert rem and rem["action"]["type"] == "reconnect_provider"
    assert rem["action"]["provider"] == "openai-codex"


def test_classify_cognition_error_full_recursion_limit_is_actionable(tmp_path: Path) -> None:
    """The LangGraph step-budget message classifies (via the unified
    actionable-errors rule) instead of reaching the Think panel verbatim with
    its dead-end `recursion_limit` config-key advice and no remediation."""
    from olano.cognition.orchestrator import classify_cognition_error_full

    code, msg, rem = classify_cognition_error_full(
        "Recursion limit of 12 reached without hitting a stop condition. "
        "You can increase the limit by setting the `recursion_limit` config key.",
        _config(tmp_path),
    )
    assert code == "step_budget_exhausted"
    assert "recursion_limit" not in msg
    # The remediation must point at a control the reader can reach — the Cortex
    # → Autonomy step-budget rows, not the agent.json5 key it used to name (which
    # meant SSH or nothing).
    assert rem is not None
    assert "step budget" in rem["summary"]
    assert "Cortex" in rem["summary"]


async def test_run_phase_pulse_failure_surfaces_status_and_remediation(tmp_path: Path) -> None:
    """A failed Pulse run reports status=error + code + remediation (not a silent
    'completed/0'), AND drops an OPS alert carrying the fix-it path."""
    from olano.cognition import orchestrator as orch
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import CurationResult, PulseLane

    cfg = _config(tmp_path).model_copy(update={"model": "openai-codex:gpt-5.4-mini"})

    failed = CurationResult(
        [],
        status="error",
        reason="The pulse curation worker could not complete.",
        error="401 unauthorized: your sign-in has expired, please sign in again",
    )
    gen = MagicMock()
    gen.capture_pulse_agent = AsyncMock(return_value=failed)

    with (
        patch.object(orch, "feature_enabled", return_value=True),
        patch.object(orch, "deep_gate", return_value=(True, "")),
        patch("olano.pulse.generators.PulseGenerator", return_value=gen),
    ):
        result = await orch.run_phase("pulse", cfg, tmp_path, source="manual", force=True)

    assert result["status"] == "error"
    assert result["code"] == "provider_auth_expired"
    assert result["remediation"]["action"]["type"] == "reconnect_provider"

    # The failure is visible in the OPS lane with the remediation attached.
    ops = PulseEngine(cfg, tmp_path / ".olano").list_pending(lane=PulseLane.OPS, max_items=20)
    err = [i for i in ops if (i.metadata or {}).get("kind") == "cortex_error"]
    assert err, "expected a cortex_error OPS alert"
    assert err[0].metadata["remediation"]["action"]["type"] == "reconnect_provider"


def _queue_job(tmp_path: Path, job_id: str, goal: str) -> Path:
    jobs_dir = tmp_path / ".olano" / "state" / "cognition" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": job_id,
                "agent": "orch",
                "item_id": f"item-{job_id}",
                "action": {"type": "run_worker", "playbook": "idea_expand", "goal": goal},
                "status": "queued",
            }
        ),
        encoding="utf-8",
    )
    return path


async def test_drainer_stop_ends_one_job_not_the_drainer(tmp_path: Path) -> None:
    """Stop on an approved task (the Runs panel) cancels the task that job runs
    in. That used to be the drainer loop itself, so one Stop silently ended
    every agent's approved work until a restart. Each job now has its own task:
    the stopped one is marked, the next queued one still runs, and the drain
    returns normally."""
    import asyncio

    from olano.cognition import jobs as cortex_jobs
    from olano.cognition.worker import WorkerOutcome
    from olano.core.runner import AgentRunner
    from olano.runtime.server import RuntimeServer

    cortex_jobs.REGISTRY.reset()
    config = _config(tmp_path)
    first = _queue_job(tmp_path, "job1", "first")
    second = _queue_job(tmp_path, "job2", "second")
    server = RuntimeServer(agent_names=[])
    server._runners["orch"] = AgentRunner(MagicMock(), config)

    started = asyncio.Event()
    ran: list[str] = []

    async def fake_action(cfg, root, action):
        ran.append(action["goal"])
        if action["goal"] == "first":
            started.set()
            await asyncio.sleep(60)  # until stopped
        return WorkerOutcome(status="completed", summary="ok"), ""

    with patch("olano.cognition.pulse_agent.run_pulse_worker_action", new=fake_action):
        drain = asyncio.create_task(server._drain_cognition_jobs())
        await asyncio.wait_for(started.wait(), 5)
        live = cortex_jobs.REGISTRY.find_live("orch", "pulse_task", "item-job1")
        assert live is not None
        assert cortex_jobs.REGISTRY.stop(live.id)
        await asyncio.wait_for(drain, 10)  # the drain finishes on its own

    assert ran == ["first", "second"]
    assert json.loads(first.read_text())["status"] == "cancelled"
    assert json.loads(second.read_text())["status"] == "done"
    assert live.status == cortex_jobs.STOPPED
    cortex_jobs.REGISTRY.reset()


async def test_drainer_shutdown_still_cancels_a_running_job(tmp_path: Path) -> None:
    """The other direction: cancelling the drain (shutdown) must still cancel
    the job it is running and propagate, exactly as before."""
    import asyncio

    from olano.cognition import jobs as cortex_jobs
    from olano.cognition.worker import WorkerOutcome
    from olano.core.runner import AgentRunner
    from olano.runtime.server import RuntimeServer

    cortex_jobs.REGISTRY.reset()
    config = _config(tmp_path)
    path = _queue_job(tmp_path, "job1", "slow")
    server = RuntimeServer(agent_names=[])
    server._runners["orch"] = AgentRunner(MagicMock(), config)
    started = asyncio.Event()

    async def fake_action(cfg, root, action):
        started.set()
        await asyncio.sleep(60)
        return WorkerOutcome(status="completed"), ""

    with patch("olano.cognition.pulse_agent.run_pulse_worker_action", new=fake_action):
        drain = asyncio.create_task(server._drain_cognition_jobs())
        await asyncio.wait_for(started.wait(), 5)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
    assert json.loads(path.read_text()) == {
        **json.loads(path.read_text()),
        "status": "cancelled",
        "error": "Interrupted by shutdown",
    }
    assert cortex_jobs.REGISTRY.find_live("orch", "pulse_task", "item-job1") is None
    cortex_jobs.REGISTRY.reset()
