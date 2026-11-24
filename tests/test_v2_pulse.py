from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from olano.channels.adapter import BaseChannelAdapter
from olano.channels.registry import ChannelRegistry
from olano.cli.commands import CommandContext, CommandDispatcher
from olano.config.agent_config import AgentConfig, ChannelConfig
from olano.config.workspace import Workspace
from olano.core.runner import AgentRunner
from olano.memory.markdown_memory import MarkdownMemory
from olano.memory.self_learning import SelfLearningService
from olano.pulse.delivery import PulseDelivery
from olano.pulse.engine import PulseEngine, load_pulse_preferences
from olano.pulse.generators import PulseGenerator
from olano.pulse.models import PulseLane, PulsePriority, PulseType


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """Pulse curation tests assume a reachable model; satisfy the readiness
    preflight with a dummy provider key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


class _DummyAdapter(BaseChannelAdapter):
    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, text: str, target: str, **kwargs):
        self.sent.append((text, target))
        return "msg-1"


def _make_config(workspace_root, **pulse_kwargs):
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="pulse-agent",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(workspace_root),
        backend=FilesystemBackendConfig(root_dir=str(workspace_root)),
    )
    for key, value in pulse_kwargs.items():
        setattr(config.pulse, key, value)
    return config


def test_pulse_md_parser_reads_frontmatter_and_sections(tmp_path) -> None:
    (tmp_path / "PULSE.md").write_text(
        """---
delivery: batch
quiet_hours:
  start: "23:00"
  end: "08:00"
timezone: America/Costa_Rica
---

## Interests
- Monitor AI news and model releases
- Track BTC and ETH price movements >5%

## Check Routines
- Check email inbox 3 times per day
- Review GitHub PRs every morning

## Delivery Preferences
- Urgent: immediate via Telegram
- Normal: daily digest at 9am
""",
        encoding="utf-8",
    )

    prefs = load_pulse_preferences(tmp_path)

    assert prefs.delivery == "batch"
    assert prefs.quiet_hours_start == "23:00"
    assert prefs.quiet_hours_end == "08:00"
    assert prefs.timezone == "America/Costa_Rica"
    assert prefs.interests == [
        "Monitor AI news and model releases",
        "Track BTC and ETH price movements >5%",
    ]
    assert prefs.check_routines == [
        "Check email inbox 3 times per day",
        "Review GitHub PRs every morning",
    ]
    assert prefs.delivery_preferences == [
        "Urgent: immediate via Telegram",
        "Normal: daily digest at 9am",
    ]


async def test_approvals_command_approve_executes_action(
    tmp_workspace, base_config, mock_agent
) -> None:
    Workspace(root=tmp_workspace).init()
    (tmp_workspace / "SOUL.md").write_text("# Soul\n\nExisting note.\n", encoding="utf-8")

    engine = PulseEngine(base_config, tmp_workspace / ".olano")
    item = engine.add(
        content="Review proposed SOUL.md updates",
        lane=PulseLane.APPROVAL,
        type=PulseType.PROPOSAL,
        priority=PulsePriority.NORMAL,
        action={
            "type": "append_lines",
            "path": "SOUL.md",
            "heading": "Self-Improvement",
            "lines": ["Be more concise in explanations"],
        },
    )

    runner = AgentRunner(mock_agent, base_config)
    ctx = CommandContext(config=base_config, runner=runner, thread_id="pulse-thread")
    dispatcher = CommandDispatcher()

    result = await dispatcher.dispatch(f"/approvals approve {item.id}", ctx)

    assert "Updated SOUL.md." in result.output
    updated = (tmp_workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "Be more concise in explanations" in updated
    reloaded = PulseEngine(base_config, tmp_workspace / ".olano")
    assert reloaded.get_item(item.id).status.value == "approved"


async def test_pulse_command_accept_marks_item_approved(
    tmp_workspace, base_config, mock_agent
) -> None:
    Workspace(root=tmp_workspace).init()
    engine = PulseEngine(base_config, tmp_workspace / ".olano")
    item = engine.add(
        content="Draft the Phase 3.1 spec before more UI work.",
        title="Write the Phase 3.1 spec",
        priority=PulsePriority.NORMAL,
    )

    runner = AgentRunner(mock_agent, base_config)
    ctx = CommandContext(config=base_config, runner=runner, thread_id="pulse-thread")
    dispatcher = CommandDispatcher()

    result = await dispatcher.dispatch(f"/pulse accept {item.id}", ctx)

    assert f"Accepted PULSE item {item.id}." in result.output
    reloaded = PulseEngine(base_config, tmp_workspace / ".olano")
    assert reloaded.get_item(item.id).status.value == "approved"


async def test_pulse_command_list_returns_rich_renderable(
    tmp_workspace, base_config, mock_agent
) -> None:
    Workspace(root=tmp_workspace).init()
    engine = PulseEngine(base_config, tmp_workspace / ".olano")
    engine.add(
        content="You are actively reshaping the curated PULSE experience.",
        title="Polish the curated PULSE cards",
        why_now="The current command output is dense and hard to scan.",
        suggested_next_step="Render each PULSE item as a Rich card with badges and spacing.",
        references=["README.md", "LIONCLAW_2.0_FINAL_ARCHITECTURE_PHASE_3_1.md"],
        priority=PulsePriority.NORMAL,
    )

    runner = AgentRunner(mock_agent, base_config)
    ctx = CommandContext(config=base_config, runner=runner, thread_id="pulse-thread")
    dispatcher = CommandDispatcher()

    result = await dispatcher.dispatch("/pulse list", ctx)

    assert result.renderable is not None
    assert "PULSE (1 items):" in result.output

    console = Console(record=True, width=100, highlight=False)
    console.print(result.renderable)
    text = console.export_text()
    assert "Polish the curated PULSE cards" in text
    assert "Why now" in text
    assert "Next step" in text
    assert "Actions" in text


async def test_channel_delivery_respects_quiet_hours(tmp_path) -> None:
    Workspace(root=tmp_path).init()
    config = _make_config(
        tmp_path,
        in_channels=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    )
    # A real Telegram chat id is numeric — the adapter parses it with int(),
    # and target resolution now refuses a name-shaped one rather than letting
    # it die there.
    config.channels = [ChannelConfig(type="telegram", chat_id="123456789")]

    engine = PulseEngine(config, tmp_path / ".olano")
    engine.add("Check tomorrow morning's digest", priority=PulsePriority.NORMAL)
    engine._is_quiet_hours = lambda: True  # type: ignore[method-assign]

    registry = ChannelRegistry()
    adapter = _DummyAdapter()
    registry.register(config.name, "telegram", adapter)

    delivered = await PulseDelivery(engine).deliver_to_channels(registry, config)

    assert delivered == 0
    assert adapter.sent == []


async def test_channel_delivery_formats_and_marks_delivered(tmp_path) -> None:
    Workspace(root=tmp_path).init()
    config = _make_config(tmp_path, in_channels=True)
    # A real Telegram chat id is numeric — the adapter parses it with int(),
    # and target resolution now refuses a name-shaped one rather than letting
    # it die there.
    config.channels = [ChannelConfig(type="telegram", chat_id="123456789")]

    engine = PulseEngine(config, tmp_path / ".olano")
    item = engine.add("Watch for heartbeat anomalies", priority=PulsePriority.NORMAL)

    registry = ChannelRegistry()
    adapter = _DummyAdapter()
    registry.register(config.name, "telegram", adapter)

    delivered = await PulseDelivery(engine).deliver_to_channels(registry, config)

    assert delivered == 1
    assert adapter.sent
    assert "*PULSE*" in adapter.sent[0][0]
    assert engine.get_item(item.id).status.value == "delivered"


@pytest.mark.usefixtures("cortex_on")  # self-improve is Cortex work; the engine is off by default
async def test_self_improve_routes_changes_through_pulse(tmp_path) -> None:
    # With Fully Autonomous OFF, self-improve proposals must wait in the APPROVAL
    # lane (SOUL/AGENTS untouched until a human approves).
    from olano.config.system_settings import set_auto_approve

    set_auto_approve(False)

    Workspace(root=tmp_path).init()
    (tmp_path / "SOUL.md").write_text("# Soul\n\nSteady.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Instructions\n\nCheck files first.\n", encoding="utf-8"
    )
    (tmp_path / "MEMORY.md").write_text(
        "## User Preferences\n- Prefers Python\n- Works in UTC\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    svc = SelfLearningService(
        agent_config=config,
        workspace_root=tmp_path,
        markdown_memory=MarkdownMemory(tmp_path),
    )
    from olano.cognition.worker import WorkerOutcome

    outcome = WorkerOutcome(
        status="completed",
        summary=(
            '{"soul_additions":["Be concise."],'
            '"agents_additions":["Always inspect the workspace before answering."],'
            '"summary":"Record brevity and workspace-awareness as defaults."}'
        ),
    )

    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        result = await svc.self_improve()

    assert "Queued PULSE proposal items" in result
    assert "Be concise." not in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert "Always inspect the workspace before answering." not in (
        tmp_path / "AGENTS.md"
    ).read_text(encoding="utf-8")

    engine = PulseEngine(config, tmp_path / ".olano")
    proposals = [
        item
        for item in engine.list_pending(max_items=10, lane=PulseLane.APPROVAL)
        if item.type == PulseType.PROPOSAL
    ]
    assert len(proposals) >= 2

    soul_item = next(item for item in proposals if item.metadata.get("target_file") == "SOUL.md")
    ok, message = engine.approve(soul_item.id)
    assert ok is True
    assert "Updated SOUL.md." in message
    assert "Be concise." in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


def _seed_identity_workspace(tmp_path) -> None:
    Workspace(root=tmp_path).init()
    (tmp_path / "SOUL.md").write_text("# Soul\n\nSteady.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Instructions\n\nCheck files first.\n", encoding="utf-8"
    )
    (tmp_path / "MEMORY.md").write_text(
        "## User Preferences\n- Prefers Python\n- Works in UTC\n",
        encoding="utf-8",
    )


async def _run_self_improve(config, tmp_path) -> None:
    from olano.cognition.worker import WorkerOutcome

    svc = SelfLearningService(
        agent_config=config,
        workspace_root=tmp_path,
        markdown_memory=MarkdownMemory(tmp_path),
    )
    outcome = WorkerOutcome(
        status="completed",
        summary=(
            '{"soul_additions":["Be concise."],'
            '"agents_additions":["Always inspect the workspace before answering."],'
            '"summary":"Record brevity and workspace-awareness as defaults."}'
        ),
    )
    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        await svc.self_improve()


@pytest.mark.usefixtures("cortex_on")
async def test_self_improve_holds_identity_proposals_when_fully_autonomous(tmp_path) -> None:
    # Fully Autonomous ON (the default) sweeps the queue after self-improve —
    # but SOUL.md / AGENTS.md proposals are `identity_edit`, which the default
    # `cognition.approval_required` keeps for a human whatever the switch says.
    # So they stay pending and the files are untouched. (They used to apply on
    # the spot: the field was documented and never read.)
    from olano.config.system_settings import set_auto_approve

    set_auto_approve(True)
    _seed_identity_workspace(tmp_path)
    config = _make_config(tmp_path)
    await _run_self_improve(config, tmp_path)

    assert "Be concise." not in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert "Always inspect the workspace" not in (tmp_path / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    engine = PulseEngine(config, tmp_path / ".olano")
    pending = [
        item
        for item in engine.list_pending(max_items=10, lane=PulseLane.APPROVAL)
        if item.type == PulseType.PROPOSAL
    ]
    assert {item.metadata.get("target_file") for item in pending} == {"SOUL.md", "AGENTS.md"}
    assert all(item.metadata.get("auto_approved") is not True for item in pending)
    # A human click still applies it, exactly as before.
    soul_item = next(item for item in pending if item.metadata.get("target_file") == "SOUL.md")
    ok, _ = engine.approve(soul_item.id)
    assert ok is True
    assert "Be concise." in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


@pytest.mark.usefixtures("cortex_on")
async def test_self_improve_auto_applies_when_owner_lifts_identity_hold(tmp_path) -> None:
    # The owner's opt-out: `approval_required: []` makes Fully Autonomous apply
    # identity proposals immediately — no pending approval is left for a human.
    from olano.config.agent_config import CognitionConfig
    from olano.config.system_settings import set_auto_approve

    set_auto_approve(True)
    _seed_identity_workspace(tmp_path)
    config = _make_config(tmp_path)
    config.cognition = CognitionConfig(approval_required=[])
    await _run_self_improve(config, tmp_path)

    assert "Be concise." in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert "Always inspect the workspace" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    engine = PulseEngine(config, tmp_path / ".olano")
    pending = [
        item
        for item in engine.list_pending(max_items=10, lane=PulseLane.APPROVAL)
        if item.type == PulseType.PROPOSAL
    ]
    assert pending == []


def test_runtime_signal_path_generates_pulse_items(tmp_path) -> None:
    Workspace(root=tmp_path).init()
    (tmp_path / "MEMORY.md").write_text("A" * 25_000, encoding="utf-8")
    config = _make_config(tmp_path)

    generator = PulseGenerator(config, tmp_path)
    items = generator.capture_runtime_signals(
        usage_meta={"total_tokens": 90_000},
        tool_failures=["shell: permission denied"],
    )

    contents = [item.content for item in items]
    assert items
    assert all(item.lane_type == PulseLane.OPS for item in items)
    assert any("90,000 tokens" in content for content in contents)
    assert any("tool failure" in content.lower() for content in contents)
    assert any("MEMORY.md is large" in content for content in contents)


async def test_curated_pulse_generation_creates_editorial_items(tmp_path, lionclaw_home) -> None:
    Workspace(root=tmp_path).init()
    (tmp_path / "IDENTITY.md").write_text("# Identity\nProduct strategist.\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\nSeek leverage and clarity.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text(
        "# User\nInterested in product design and AI.\n", encoding="utf-8"
    )
    (tmp_path / "MEMORY.md").write_text(
        "# Memory\n- Working on PULSE redesign.\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Instructions\nStay grounded in the workspace.\n", encoding="utf-8"
    )
    sessions_dir = lionclaw_home / "sessions" / "pulse-agent"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "thread-1.jsonl").write_text(
        "\n".join(
            [
                '{"role":"user","content":"We need a better PULSE feature."}',
                '{"role":"assistant","content":"I can redesign it around stronger insights."}',
                '{"role":"user","content":"Make it feel editorial and proactive."}',
                '{"role":"assistant","content":"I will separate insights from ops noise."}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    config.pulse.min_turns_for_generation = 1
    config.pulse.generated_items_per_cycle = 2
    config.pulse.focus_topics = ["product strategy", "agent UX"]

    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = MagicMock(
        content=(
            '{"items":[{"type":"insight","priority":"normal","title":"PULSE needs a stronger editorial lens",'
            '"summary":"Recent work keeps circling back to the difference between insight and system status.",'
            '"why_now":"You are actively redesigning PULSE semantics and delivery right now.",'
            '"suggested_next_step":"Draft the Phase 3.1 specification before more implementation churn.",'
            '"references":["LIONCLAW_2.0_FINAL_ARCHITECTURE_PHASE_3.md"]}]}'
        )
    )

    with patch("olano.models.build_model", return_value=mock_model):
        items = await PulseGenerator(config, tmp_path).capture_agent_curated_pulse(
            thread_id="thread-1",
            force=True,
        )

    assert len(items) == 1
    item = items[0]
    assert item.lane_type == PulseLane.PULSE
    assert item.type == PulseType.INSIGHT
    assert item.title == "PULSE needs a stronger editorial lens"
    assert "Phase 3.1 specification" in item.suggested_next_step


def test_add_curated_items_tolerates_renamed_schema_fields(tmp_path) -> None:
    """Curation models routinely paraphrase the JSON schema (e.g. return
    ``why``/``action``/``grounding`` instead of ``summary``/``why_now``/
    ``references``). Such items must still surface, not be silently dropped — this
    was the cause of the systemic "Curated 0 pulse item(s)" across all agents."""
    Workspace(root=tmp_path).init()
    config = _make_config(tmp_path)

    raw = [
        {
            "title": "Finance pulse: AMD snapshot",
            "why": "User tracks AMD and wants current data.",
            "action": "Pull the latest AMD price plus recent news and summarize.",
            "grounding": ["Interest: AMD stock"],
        },
        # Even an item with NO title still surfaces (title derived from summary).
        {"summary": "A fresh idea worth exploring with no explicit title."},
    ]

    items = PulseGenerator(config, tmp_path).add_curated_items(raw, source="agent")

    assert len(items) == 2
    first = items[0]
    assert first.title == "Finance pulse: AMD snapshot"
    # `action` text backfills the (missing) summary; `why` → why_now; `grounding` → references.
    assert "AMD price" in first.content
    assert "tracks AMD" in first.why_now
    assert first.references == ["Interest: AMD stock"]
    assert items[1].title  # derived from summary, non-empty
