"""The Cortex funnel's autonomous behaviour: ratings, staged promotion, the
triage phase's decisions under each posture, self-scheduled steps, and what the
agent's prompt says about all of it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.cognition import cortex_items as ci
from olano.cognition import triage
from olano.cognition.audit import read_actions
from olano.cognition.orchestrator import PHASE_BY_ID, PHASES, THINK_CYCLE_ORDER
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.config.cortex_funnel import DEFAULT_PROMOTE_THRESHOLD
from olano.config.system_settings import (
    auto_discard_enabled,
    effective_max_promotions_per_run,
    effective_max_self_scheduled_tasks,
    effective_promote_threshold,
    load_system_settings,
    set_auto_approve,
)
from olano.security.request_context import push_request_context
from olano.tools import cortex_tools

# ───────────────────────────── ratings ───────────────────────────────────────


def test_set_rating_writes_a_uniform_block_and_clamps(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Rated", detail="x")
    out = ci.set_rating(
        tmp_path,
        item["id"],
        14,
        rationale="great",
        dimensions={"value": 9, "feasibility": "7", "bogus": "n/a"},
        recommendation="PROMOTE",
        next_step="build it",
    )
    assert out is not None
    rating = out["rating"]
    assert rating["score"] == 10.0
    assert rating["dimensions"] == {"value": 9.0, "feasibility": 7.0}
    assert rating["recommendation"] == "promote"
    assert rating["rated_by"] == "cortex" and rating["rated_at"]
    assert ci.rating_score(out) == 10.0
    # An unknown recommendation reads as keep; an unknown id is None.
    assert (
        ci.set_rating(tmp_path, item["id"], 5, recommendation="yolo")["rating"]["recommendation"]
        == "keep"
    )
    assert ci.set_rating(tmp_path, "nope", 5) is None
    assert ci.rating_score({"rating": {"score": None}}) is None
    assert ci.rating_score({}) is None
    # The mirror table carries the score.
    assert "5/10" in (tmp_path / "IDEAS.md").read_text(encoding="utf-8")


def test_funnel_order_and_next_kind() -> None:
    assert ci.FUNNEL == ("brainstorm", "idea", "prototype", "project")
    assert ci.next_kind("brainstorm") == "idea"
    assert ci.next_kind("idea") == "prototype"
    assert ci.next_kind("prototype") == "project"
    assert ci.next_kind("project") is None
    assert ci.next_kind("musing") is None


# ───────────────────────────── phase registry ───────────────────────────────


def test_triage_phase_is_registered_before_advance() -> None:
    ids = [p.id for p in PHASES]
    assert "triage" in ids
    assert ids.index("triage") < ids.index("advance")
    assert THINK_CYCLE_ORDER.index("triage") < THINK_CYCLE_ORDER.index("advance")
    assert PHASE_BY_ID["triage"].policy == "auto"


# ───────────────────────────── settings ──────────────────────────────────────


@pytest.fixture
def cfg(base_config: AgentConfig) -> AgentConfig:
    base_config.cognition.enabled = True
    return base_config


def test_funnel_knobs_resolve_two_tiers_and_clamp(cfg: AgentConfig) -> None:
    settings = load_system_settings()
    assert settings.promote_threshold == DEFAULT_PROMOTE_THRESHOLD
    assert effective_promote_threshold(cfg) == DEFAULT_PROMOTE_THRESHOLD
    assert effective_max_promotions_per_run(cfg) == 1
    assert effective_max_self_scheduled_tasks(cfg) == 6
    cfg.cognition.promote_threshold = 6
    cfg.cognition.max_promotions_per_run = 3
    cfg.cognition.max_self_scheduled_tasks = 99  # clamped by the resolver
    assert effective_promote_threshold(cfg) == 6
    assert effective_max_promotions_per_run(cfg) == 3
    assert effective_max_self_scheduled_tasks(cfg) == 20
    # Validation clamps a hand-edited agent value and keeps None as inherit.
    model = AgentConfig.model_validate(
        {
            "name": "k",
            "model": "fake:instant",
            "cognition": {
                "promote_threshold": 42,
                "max_promotions_per_run": "",
                "auto_discard": False,
            },
        }
    )
    assert model.cognition.promote_threshold == 10
    assert model.cognition.max_promotions_per_run is None
    assert model.cognition.auto_discard is False


def test_auto_discard_requires_fully_autonomous(cfg: AgentConfig) -> None:
    assert auto_discard_enabled(cfg) is True  # auto_approve defaults on
    set_auto_approve(False)
    assert auto_discard_enabled(cfg) is False
    cfg.cognition.auto_approve = True
    assert auto_discard_enabled(cfg) is True
    cfg.cognition.auto_discard = False
    assert auto_discard_enabled(cfg) is False


# ───────────────────────────── triage decisions ─────────────────────────────


def _rated(root: Path, kind: str, title: str, score: float, rec: str) -> dict:
    item = ci.add_item(root, kind=kind, title=title, detail="x")
    return ci.set_rating(root, item["id"], score, recommendation=rec, rationale="r") or item


async def test_fully_autonomous_promotes_the_best_candidate_within_the_budget(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    strong = _rated(tmp_workspace, "idea", "Strong", 9, "promote")
    stronger = _rated(tmp_workspace, "idea", "Stronger", 9.5, "promote")
    _rated(tmp_workspace, "idea", "Weak promote", 6, "promote")  # below threshold
    _rated(tmp_workspace, "project", "A project", 9, "promote")  # projects never move
    doomed = _rated(tmp_workspace, "brainstorm", "Doomed", 2, "discard")
    _rated(tmp_workspace, "brainstorm", "Low but keep", 2, "keep")

    promoted: list[str] = []

    async def _fake_promote(config, root, item, *, target=None, source="manual", budget_check=True):
        promoted.append(item["title"])
        created = ci.add_item(root, kind=target, title=item["title"], detail="y")
        ci.update_item(root, item["id"], status="promoted")
        return WorkerOutcome(status="completed"), created

    with (
        patch("olano.cognition.ideation.run_promote", new=AsyncMock(side_effect=_fake_promote)),
        patch("olano.cognition.orchestrator.deep_gate", return_value=(True, "")),
    ):
        parts, artifacts = await triage.apply_decisions(
            cfg, tmp_workspace, triage.active_items(tmp_workspace)
        )
    # Budget is 1 per run: only the highest score moved, the other waits.
    assert promoted == ["Stronger"]
    assert any("1 more promotion candidate" in p for p in parts)
    assert artifacts and artifacts[0].startswith("prototypes/")
    assert ci.get_item(tmp_workspace, strong["id"])["status"] == "active"
    assert ci.get_item(tmp_workspace, stronger["id"])["status"] == "promoted"
    # The discard recommendation at 2/10 was applied; the 'keep' at 2/10 was not.
    assert ci.get_item(tmp_workspace, doomed["id"])["status"] == "discarded"
    assert len([it for it in ci.list_items(tmp_workspace) if it["status"] == "discarded"]) == 1
    actions = [a["action"] for a in read_actions(tmp_workspace)]
    assert "cortex_promote" in actions and "cortex_discard" in actions


async def test_fully_autonomous_promotions_respect_the_deep_run_cap(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    _rated(tmp_workspace, "idea", "Strong", 9, "promote")
    with (
        patch("olano.cognition.ideation.run_promote", new=AsyncMock()) as promote,
        patch("olano.cognition.orchestrator.deep_gate", return_value=(False, "cap")),
    ):
        parts, _ = await triage.apply_decisions(
            cfg, tmp_workspace, triage.active_items(tmp_workspace)
        )
    promote.assert_not_called()
    assert any("deep-run cap" in p for p in parts)


async def test_approvals_mode_queues_a_card_instead_of_promoting(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane

    set_auto_approve(False)
    strong = _rated(tmp_workspace, "idea", "Strong", 9, "promote")
    _rated(tmp_workspace, "brainstorm", "Doomed", 1, "discard")  # advisory only now
    with patch("olano.cognition.ideation.run_promote", new=AsyncMock()) as promote:
        parts, _ = await triage.apply_decisions(
            cfg, tmp_workspace, triage.active_items(tmp_workspace)
        )
    promote.assert_not_called()
    assert any("wait for your approval" in p for p in parts)
    assert all(it["status"] == "active" for it in ci.list_items(tmp_workspace))
    engine = PulseEngine(cfg, tmp_workspace / WORKSPACE_STATE_DIRNAME)
    pending = engine.list_pending(lane=PulseLane.APPROVAL, max_items=10)
    assert len(pending) == 1
    card = pending[0]
    assert card.action == {"type": "cortex_promote", "item_id": strong["id"], "target": "prototype"}
    assert card.metadata["kind"] == "cortex_promotion"
    # The Fully Autonomous sweep never consumes this card on its own.
    from olano.cognition.orchestrator import _eligible_for_auto_approve

    assert _eligible_for_auto_approve(card, PulseLane.APPROVAL) is False


async def test_approving_the_card_runs_the_promotion(cfg: AgentConfig, tmp_workspace: Path) -> None:
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME
    from olano.pulse.engine import PulseEngine

    item = _rated(tmp_workspace, "idea", "Strong", 9, "promote")
    assert triage.queue_promotion_approval(cfg, tmp_workspace, item)
    # Opened after the card was filed: an engine reads the store once.
    engine = PulseEngine(cfg, tmp_workspace / WORKSPACE_STATE_DIRNAME)
    from olano.pulse.models import PulseLane

    card = engine.list_pending(lane=PulseLane.APPROVAL, max_items=10)[0]

    async def _fake_promote(config, root, it, *, target=None, source="manual", budget_check=True):
        created = ci.add_item(root, kind=target, title=it["title"], detail="y")
        return WorkerOutcome(status="completed", summary="done"), created

    with patch("olano.cognition.ideation.run_promote", new=AsyncMock(side_effect=_fake_promote)):
        ok, msg, outcome = await engine.execute_action_async(card)
    assert ok and outcome is not None
    assert outcome.artifacts and outcome.artifacts[0].startswith("prototypes/")
    # The sync approve path (dashboard click) enqueues it for the drainer.
    ok2, msg2 = engine.execute_action(card)
    assert ok2 and "job" in msg2


async def test_run_triage_writes_ratings_from_the_worker(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    a = ci.add_item(tmp_workspace, kind="idea", title="A", detail="x")
    b = ci.add_item(tmp_workspace, kind="prototype", title="B", detail="x")
    summary = (
        "Here you go:\n```json\n["
        f'{{"id": "{a["id"]}", "score": 8, "recommendation": "promote", "rationale": "good", '
        '"dimensions": {"value": 8}, "next_step": "build"},'
        f'{{"id": "{b["id"]}", "score": 4, "recommendation": "explore"}},'
        '{"id": "ghost", "score": 9, "recommendation": "promote"}'
        "]\n```"
    )
    with (
        patch(
            "olano.cognition.triage.run_worker",
            new=AsyncMock(return_value=WorkerOutcome(status="completed", summary=summary)),
        ),
        patch("olano.cognition.triage.apply_decisions", new=AsyncMock(return_value=(["ok."], []))),
    ):
        outcome, rated, _ = await triage.run_triage(cfg, tmp_workspace, budget_check=False)
    assert outcome.status == "completed" and rated == 2
    assert "Rated 2 item(s)." in outcome.summary and "ok." in outcome.summary
    assert ci.get_item(tmp_workspace, a["id"])["rating"]["next_step"] == "build"
    assert ci.get_item(tmp_workspace, b["id"])["rating"]["recommendation"] == "explore"
    assert "cortex_rate" in [x["action"] for x in read_actions(tmp_workspace)]


async def test_run_triage_is_empty_with_nothing_active(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    outcome, rated, _ = await triage.run_triage(cfg, tmp_workspace, budget_check=False)
    assert outcome.status == "empty" and rated == 0


# ───────────────────────────── execution posture ─────────────────────────────


def test_execute_tool_names_add_only_the_agents_own_outward_tools(cfg: AgentConfig) -> None:
    from olano.cognition.ideation import EXECUTE_TOOL_NAMES, execute_tool_names

    cfg.tools = [*cfg.tools, "gmail_send", "web_search"]
    names = execute_tool_names(cfg)
    assert names[: len(EXECUTE_TOOL_NAMES)] == list(EXECUTE_TOOL_NAMES)
    assert "gmail_send" in names
    assert "slack_send_message" not in names  # not held by this agent
    assert "cortex_schedule" in names and "pages_start" in names and "talk_to_agent" in names
    assert "scheduler_create" not in names  # the bounded tool is the only route


async def test_advance_widens_tools_and_playbook_only_when_fully_autonomous(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_advance_items

    ci.add_item(tmp_workspace, kind="project", title="P", detail="x")
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(status="completed", summary="ok")

    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(side_effect=_capture)):
        await run_advance_items(cfg, tmp_workspace, budget_check=False)
        set_auto_approve(False)
        await run_advance_items(cfg, tmp_workspace, budget_check=False)
    fully, approvals = calls
    assert "cortex_schedule" in fully["tools_allow"] and "pages_start" in fully["tools_allow"]
    assert "FULLY AUTONOMOUS MODE" in fully["playbook"] and "EXECUTE:" in fully["goal"]
    assert "Pending self-scheduled steps" in fully["context"]
    assert "cortex_schedule" not in approvals["tools_allow"]
    assert "FULLY AUTONOMOUS MODE" not in approvals["playbook"]


# ───────────────────────────── cortex_schedule ───────────────────────────────


def _bind(root: Path, agent: str = "test-agent"):
    from olano.tools.talk_to import current_agent

    class _Ctx:
        def __enter__(self):
            self.tok = current_agent.set(agent)
            self.rc = push_request_context(metadata={"workspace_root": str(root)})
            self.rc.__enter__()
            return self

        def __exit__(self, *exc):
            self.rc.__exit__(*exc)
            current_agent.reset(self.tok)

    return _Ctx()


def test_cortex_schedule_books_a_bounded_step(tmp_path: Path) -> None:
    from olano.scheduler.db import SchedulerDB

    project = ci.add_item(tmp_path, kind="project", title="Ship It", detail="x")
    idea = ci.add_item(tmp_path, kind="idea", title="Just an idea", detail="x")
    with _bind(tmp_path):
        bad_kind = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": idea["id"],
                "instruction": "do the thing in PLAN.md phase 1",
                "run_at": "2030-01-01T09:00:00",
            }
        )
        neither = cortex_tools.cortex_schedule.invoke(
            {"item_id": project["id"], "instruction": "do the thing in PLAN.md phase 1"}
        )
        too_often = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": project["id"],
                "instruction": "poll the feed and append to data.csv",
                "schedule": "*/5 * * * *",
            }
        )
        vague = cortex_tools.cortex_schedule.invoke(
            {"item_id": project["id"], "instruction": "continue", "run_at": "2030-01-01T09:00:00"}
        )
        first = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": project["id"],
                "instruction": "run scripts/ingest.py and record RESULTS",
                "run_at": "2030-01-01T09:00:00",
            }
        )
        second = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": project["id"],
                "instruction": "verify the ingest output against PLAN phase 1",
                "schedule": "0 9 * * 1-5",
            }
        )
        third = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": project["id"],
                "instruction": "a third step that should be refused by the per-item cap",
                "run_at": "2030-01-02T09:00:00",
            }
        )
    assert "Steps are scheduled on prototypes and projects" in bad_kind
    assert "exactly one of run_at" in neither
    assert "at most hourly" in too_often
    assert "specific instruction" in vague
    assert first.startswith("Scheduled step on project 'Ship It'")
    assert second.startswith("Scheduled step on project 'Ship It'")
    assert "pending steps" in third and "limit is 2 per item" in third

    tasks = SchedulerDB().list_tasks(enabled_only=True, agent_name="test-agent")
    assert len(tasks) == 2
    for task in tasks:
        assert task["created_by"] == "cortex:test-agent"
        assert cortex_tools.is_cortex_task(task)
        assert f"[cortex-item:{project['id']}]" in task["prompt"]
        assert "cortex_view" in task["prompt"] and str(tmp_path / project["path"]) in task["prompt"]
    assert "cortex_schedule" in [a["action"] for a in read_actions(tmp_path)]


def test_cortex_schedule_enforces_the_agent_wide_cap(tmp_path: Path) -> None:
    items = [ci.add_item(tmp_path, kind="prototype", title=f"P{i}", detail="x") for i in range(4)]
    outs = []
    with (
        _bind(tmp_path),
        patch("olano.config.system_settings.effective_max_self_scheduled_tasks", return_value=3),
    ):
        for i, it in enumerate(items):
            outs.append(
                cortex_tools.cortex_schedule.invoke(
                    {
                        "item_id": it["id"],
                        "instruction": f"run the prototype {i} and write RESULTS.md with the output",
                        "run_at": "2030-01-01T09:00:00",
                    }
                )
            )
    assert all(o.startswith("Scheduled step") for o in outs[:3])
    assert "limit is 3" in outs[3]


def test_cron_too_frequent_guard() -> None:
    assert cortex_tools._cron_too_frequent("* * * * *")
    assert cortex_tools._cron_too_frequent("*/15 * * * *")
    assert not cortex_tools._cron_too_frequent("*/60 * * * *")
    assert not cortex_tools._cron_too_frequent("0 9 * * 1-5")
    assert not cortex_tools._cron_too_frequent("0 */6 * * *")
    assert cortex_tools._cron_too_frequent("0,30 * * * *")
    assert not cortex_tools._cron_too_frequent("not a cron")


async def test_self_scheduled_steps_run_under_the_cognition_origin(tmp_path: Path) -> None:
    """A step the agent booked for itself must not inherit the blanket approval
    a human's scheduled task gets: HITL keeps approval_required on it."""
    from olano.scheduler.cron import OlanoScheduler
    from olano.scheduler.db import SchedulerDB
    from olano.security.request_context import current_origin

    db = SchedulerDB(db_path=tmp_path / "sched.db")
    own = db.create_task(
        "Cortex: x",
        "[cortex-item:ab] step",
        run_at="2030-01-01T09:00:00",
        agent_name="a1",
        created_by="cortex:a1",
    )
    human = db.create_task(
        "digest", "summarise", run_at="2030-01-01T09:00:00", agent_name="a1", created_by="a1"
    )
    seen: list[str] = []

    async def _ainvoke(*_a, **_k):
        seen.append(str(current_origin().get("root_source")))
        return "done"

    runner = MagicMock()
    runner.ainvoke = AsyncMock(side_effect=_ainvoke)
    sched = OlanoScheduler()
    sched._runners["a1"] = runner
    with patch.object(sched, "remove_db_task"):
        await sched._execute_db_task(db.get_task(own), db)
        await sched._execute_db_task(db.get_task(human), db)
    assert seen == ["cognition", "scheduled"]


# ───────────────────────────── the agent's prompt ───────────────────────────


def test_prompt_block_states_the_funnel_and_the_posture(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    pa = PromptAssembler(cfg, Workspace(root=tmp_workspace))
    block = pa._build_cortex_block()
    assert block.startswith("## Cortex")
    assert "brainstorm → idea → prototype → project" in block
    assert "cortex_schedule" in block and "cortex_promote" in block
    assert "FULLY AUTONOMOUS" in block and f"{DEFAULT_PROMOTE_THRESHOLD}/10" in block
    assert block in pa.build_l0()
    assert block in pa.stable_sections()["policies"]

    set_auto_approve(False)
    approvals = pa._build_cortex_block()
    assert "AUTONOMOUS with approvals" in approvals and "FULLY AUTONOMOUS" not in approvals

    cfg.cognition.autonomous = False
    assert "manual here" in pa._build_cortex_block()

    cfg.cognition.enabled = False
    assert pa._build_cortex_block() == ""


def test_cortex_schedule_files_the_step_under_the_parent_agent(tmp_path: Path) -> None:
    """Inside a Cortex worker the bound name is a throwaway `<parent>__cog__…`;
    the step must belong to the parent, or no runner would ever pick it up."""
    from olano.scheduler.db import SchedulerDB
    from olano.tools._credentials import agent_runtime_context

    project = ci.add_item(tmp_path, kind="project", title="Owned", detail="x")
    with (
        agent_runtime_context(
            "aurelius__cog__advance_items__ab12cd", {}, credential_scope="aurelius"
        ),
        push_request_context(metadata={"workspace_root": str(tmp_path)}),
    ):
        out = cortex_tools.cortex_schedule.invoke(
            {
                "item_id": project["id"],
                "instruction": "run scripts/step.py and record the output in JOURNAL.md",
                "run_at": "2030-01-01T09:00:00",
            }
        )
    assert out.startswith("Scheduled step on project 'Owned'")
    tasks = SchedulerDB().list_tasks(enabled_only=True)
    assert [t["agent_name"] for t in tasks] == ["aurelius"]
    assert tasks[0]["created_by"] == "cortex:aurelius"


# ───────────────────────────── the team ──────────────────────────────────────


class _Runner:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config


class _Runtime:
    def __init__(self, *configs: AgentConfig) -> None:
        self._runners = {c.name: _Runner(c) for c in configs}


@pytest.fixture
def team(tmp_path: Path):
    from olano.core import builder
    from olano.tools import talk_to as tt

    mailer = AgentConfig(
        name="mailer",
        description="Sends the owner's email and keeps the calendar.",
        model="fake:instant",
        tools=["gmail_send", "calendar_create_event", "slack_send_message", "web_search"],
    )
    quant = AgentConfig(
        name="quant",
        description="Markets desk.",
        model="fake:instant",
        tools=["alpaca_get_account"],
    )
    ghost = AgentConfig(name="ghost", description="internal", model="fake:instant", hidden=True)
    me = AgentConfig(
        name="lead",
        description="Team lead.",
        model="fake:instant",
        agent_visibility={"mode": "all"},
    )
    builder._MCP_SUMMARY["quant"] = [
        {
            "url": "https://mcp.linear.app/sse",
            "ok": True,
            "status": "connected",
            "tools": ["a", "b"],
        },
        {"url": "https://mcp.notion.com/mcp", "ok": False, "status": "auth_required", "tools": []},
    ]
    tt.bind_runtime(_Runtime(mailer, quant, ghost, me))
    try:
        yield me
    finally:
        tt.bind_runtime(None)
        builder._MCP_SUMMARY.pop("quant", None)


def test_peer_capabilities_name_services_not_plumbing(team: AgentConfig) -> None:
    from olano.tools import talk_to as tt

    info = {p["name"]: p for p in tt.list_peers_info()}
    assert info["mailer"]["toolkits"] == ["gmail", "calendar", "slack"]  # web_search is infra
    assert info["quant"]["toolkits"] == ["alpaca"]
    assert [m["name"] for m in info["quant"]["mcp"]] == ["mcp.linear.app", "mcp.notion.com"]
    assert info["quant"]["mcp"][0]["tools"] == 2


def test_team_digest_lists_messageable_peers_with_capabilities(team: AgentConfig) -> None:
    from olano.cognition.fleet import capability_summary, team_digest

    digest = team_digest(team)
    assert digest.startswith("Team:")
    assert "talk_to_agent(" in digest
    assert "`mailer`" in digest and "Can: gmail, calendar, slack" in digest
    assert (
        "`quant`" in digest
        and "MCP: mcp.linear.app (2 tools), mcp.notion.com (needs authorization)" in digest
    )
    assert "ghost" not in digest and "`lead`" not in digest
    assert capability_summary({"toolkits": [], "mcp": []}) == ""


def test_team_digest_respects_visibility(team: AgentConfig) -> None:
    from olano.cognition.fleet import team_digest

    team.agent_visibility.mode = "none"
    assert team_digest(team) == ""
    team.agent_visibility.mode = "list"
    team.agent_visibility.agents = ["quant"]
    digest = team_digest(team)
    assert "`quant`" in digest and "`mailer`" not in digest
    # Without the tool the peers are visible but not messageable.
    team.agent_visibility.mode = "all"
    team.tools = [t for t in team.tools if t != "talk_to_agent"]
    digest = team_digest(team)
    assert "talk_to_agent(" not in digest and "visible only" in digest


def test_chat_roster_line_carries_the_capabilities(team: AgentConfig, tmp_workspace: Path) -> None:
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    block = PromptAssembler(team, Workspace(root=tmp_workspace))._build_agent_roster_block()
    assert "`mailer`" in block and "[can: gmail, calendar, slack; shell]" in block
    assert "ghost" not in block


async def test_execution_and_triage_contexts_carry_the_team(
    team: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_advance_items, run_promote_to_project

    team.cognition.enabled = True
    ci.add_item(tmp_workspace, kind="project", title="P", detail="x")
    idea = ci.add_item(tmp_workspace, kind="idea", title="I", detail="x")
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(status="completed", summary="[]")

    with (
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(side_effect=_capture)),
        patch("olano.cognition.triage.run_worker", new=AsyncMock(side_effect=_capture)),
    ):
        await run_advance_items(team, tmp_workspace, budget_check=False)
        await run_promote_to_project(team, tmp_workspace, idea, budget_check=False)
        await triage.run_triage(team, tmp_workspace, budget_check=False)
    for call in calls:
        assert "Team: the other agents" in call["context"]
        assert "`mailer`" in call["context"]


async def test_talk_to_agent_speaks_as_the_worker_parent_and_never_to_itself(
    team: AgentConfig,
) -> None:
    from olano.tools import talk_to as tt
    from olano.tools._credentials import agent_runtime_context

    with (
        agent_runtime_context("lead__cog__advance_items__ab12cd", {}, credential_scope="lead"),
        patch.object(
            tt, "_peer_policy_error", return_value="[talk_to_agent error] blocked"
        ) as gate,
    ):
        itself = await tt.talk_to_agent.ainvoke({"agent_name": "lead", "message": "hi"})
        other = await tt.talk_to_agent.ainvoke({"agent_name": "mailer", "message": "send it"})
    assert "is you" in itself
    assert other == "[talk_to_agent error] blocked"
    gate.assert_called_once_with("lead", "mailer")


# ───────────────────────────── duplicates ────────────────────────────────────


def test_content_similarity_sees_past_the_title() -> None:
    same = ci.content_similarity(
        "Solar lumber kiln",
        "Dry lumber with passive solar heat in a small shed.",
        "Passive solar drying shed",
        "A shed that dries lumber using solar heat, no electricity.",
    )
    different = ci.content_similarity(
        "Solar lumber kiln",
        "Dry lumber with passive solar heat in a small shed.",
        "Weekly invoice reminder",
        "Email clients whose invoices are overdue every Monday.",
    )
    assert same >= ci.SIMILAR_CONTENT_THRESHOLD
    assert different < ci._SEMANTIC_CHECK_FLOOR
    # Two short titles sharing a word are not one item.
    assert ci.content_similarity("Idea A", "a", "Idea B", "b") < ci.SIMILAR_CONTENT_THRESHOLD
    assert (
        ci.content_similarity("Weekly Review", "", "Weekly review", "")
        >= ci.SIMILAR_CONTENT_THRESHOLD
    )


def test_find_similar_any_matches_across_stages_and_statuses(tmp_path: Path) -> None:
    project = ci.add_item(
        tmp_path,
        kind="project",
        title="Solar lumber kiln",
        summary="Dry lumber with solar heat.",
        detail="x",
    )
    dropped = ci.add_item(
        tmp_path,
        kind="brainstorm",
        title="Invoice chasing",
        summary="Chase overdue invoices.",
        detail="x",
    )
    ci.set_status(tmp_path, dropped["id"], "discarded")
    hit = ci.find_similar_any(tmp_path, "Passive solar drying shed", "Dry lumber using solar heat.")
    assert hit is not None and hit[0]["id"] == project["id"]
    hit2 = ci.find_similar_any(tmp_path, "Chase overdue invoices automatically", "")
    assert hit2 is not None and hit2[0]["id"] == dropped["id"]
    assert (
        ci.find_similar_any(tmp_path, "Garden moss wall", "A living wall of moss indoors.") is None
    )
    assert ci.find_similar_any(tmp_path, "", "") is None


def test_find_similar_any_uses_embeddings_in_the_grey_band(tmp_path: Path, monkeypatch) -> None:
    ci.add_item(
        tmp_path, kind="idea", title="Kiln for lumber", summary="solar drying of planks", detail="x"
    )
    monkeypatch.setenv("LIONCLAW_CORTEX_SEMANTIC_DEDUP", "1")
    monkeypatch.setattr(ci, "_SEMANTIC_DISABLED", False)
    with patch.object(ci, "_semantic_similarity", return_value=0.95) as sem:
        hit = ci.find_similar_any(tmp_path, "Wood drying with sunlight", "planks dried by the sun")
    assert hit is not None and hit[1] == 0.95
    sem.assert_called()
    with patch.object(ci, "_semantic_similarity", return_value=0.4):
        assert (
            ci.find_similar_any(tmp_path, "Wood drying with sunlight", "planks dried by the sun")
            is None
        )


def test_portfolio_digest_lists_every_stage_and_status(tmp_path: Path) -> None:
    ci.add_item(tmp_path, kind="brainstorm", title="Energy", summary="angles", detail="x")
    idea = ci.add_item(tmp_path, kind="idea", title="Solar kiln", summary="dry lumber", detail="x")
    ci.update_item(tmp_path, idea["id"], status="promoted")
    bad = ci.add_item(tmp_path, kind="prototype", title="Bad proto", detail="x")
    ci.set_status(tmp_path, bad["id"], "discarded")
    digest = ci.portfolio_digest(tmp_path)
    assert "EVERYTHING ALREADY CAPTURED" in digest
    assert "Brainstorms (1):" in digest and "Ideas (1):" in digest and "Prototypes (1):" in digest
    assert "Solar kiln [promoted]: dry lumber" in digest
    assert "Bad proto [DISCARDED: do not resurrect]" in digest
    assert ci.portfolio_digest(tmp_path, limit=1).count("(list truncated)") == 1
    assert "Nothing captured yet" in ci.portfolio_digest(tmp_path / "empty")


async def test_idea_generation_skips_restatements_and_improves_same_kind(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_idea_generation

    ci.add_item(
        tmp_workspace,
        kind="project",
        title="Solar lumber kiln",
        summary="Dry lumber with solar heat.",
        detail="x",
    )
    dropped = ci.add_item(
        tmp_workspace,
        kind="brainstorm",
        title="Invoice chasing",
        summary="Chase overdue invoices by email.",
        detail="x",
    )
    ci.set_status(tmp_workspace, dropped["id"], "discarded")
    existing = ci.add_item(
        tmp_workspace,
        kind="idea",
        title="Moss wall",
        summary="An indoor living moss wall.",
        detail="x",
    )
    payload = (
        '[{"title": "Passive solar drying shed", "summary": "Dry lumber using solar heat."},'
        ' {"title": "Chase overdue invoices automatically", "summary": "Email clients with overdue invoices."},'
        ' {"title": "Living moss feature wall", "summary": "An indoor living moss wall with a drip feed."},'
        ' {"title": "Bike repair pop-up", "summary": "A weekend bike repair stand at the market."},'
        ' {"title": "Weekend bicycle repair stall", "summary": "A bike repair stand at the weekend market."}]'
    )
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(status="completed", summary=payload)

    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(side_effect=_capture)):
        outcome, count, _ = await run_idea_generation(cfg, tmp_workspace, budget_check=False)
    assert "EVERYTHING ALREADY CAPTURED" in calls[0]["context"]
    assert count == 1  # only the bike stand is new
    ideas = {i["title"]: i for i in ci.list_items(tmp_workspace, kind="idea")}
    assert set(ideas) == {"Moss wall", "Bike repair pop-up"}
    assert (
        ideas["Moss wall"]["summary"] == "An indoor living moss wall with a drip feed."
    )  # improved
    assert existing["id"] == ideas["Moss wall"]["id"]
    assert "Skipped 3 near-duplicate(s)" in outcome.summary
    assert "already promoted" not in outcome.summary and "discarded brainstorm" in outcome.summary
    assert "repeats another idea in this batch" in outcome.summary


async def test_brainstorm_that_restates_the_portfolio_is_dropped(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_brainstorm

    ci.add_item(
        tmp_workspace,
        kind="idea",
        title="Solar lumber kiln",
        summary="Dry lumber with solar heat.",
        detail="x",
    )
    payload = '{"title": "Solar heat for drying lumber", "summary": "Dry lumber with solar heat.", "detail": "- angles"}'
    with patch(
        "olano.cognition.ideation.run_worker",
        new=AsyncMock(return_value=WorkerOutcome(status="completed", summary=payload)),
    ):
        outcome, count, _ = await run_brainstorm(cfg, tmp_workspace, budget_check=False)
    assert count == 0 and outcome.status == "empty"
    assert "restates the existing idea 'Solar lumber kiln'" in outcome.summary
    assert ci.counts_by_kind(tmp_workspace)["brainstorm"] == 0


async def test_triage_links_duplicates_the_reviewer_names(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    a = ci.add_item(tmp_workspace, kind="idea", title="A", detail="x")
    b = ci.add_item(tmp_workspace, kind="idea", title="A again", detail="x")
    summary = (
        f'[{{"id": "{a["id"]}", "score": 7, "recommendation": "keep"}},'
        f'{{"id": "{b["id"]}", "score": 2, "recommendation": "discard", "duplicate_of": "{a["id"]}"}}]'
    )
    with (
        patch(
            "olano.cognition.triage.run_worker",
            new=AsyncMock(return_value=WorkerOutcome(status="completed", summary=summary)),
        ),
        patch("olano.cognition.triage.apply_decisions", new=AsyncMock(return_value=([], []))),
    ):
        await triage.run_triage(cfg, tmp_workspace, budget_check=False)
    assert ci.get_item(tmp_workspace, b["id"])["links"]["duplicate_of"] == a["id"]
    assert "duplicate_of" not in ci.get_item(tmp_workspace, a["id"])["links"]
