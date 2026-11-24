"""End to end, offline: the autonomous Cortex loop produces real results.

Nothing here is mocked below the model. Every phase runs through
``orchestrator.run_phase`` exactly as the scheduler fires it, every worker is a
real agent graph built by ``build_agent_async`` with its real tools, every file
lands on disk through those tools, the self-scheduled step is written to the
real scheduler database and fired through the real ``OlanoScheduler``, and the
approval card is approved through the real Pulse engine. Only the language
model is the deterministic ``fake:`` provider, scripted per worker (keyed by a
sentence of each worker's own playbook), so the test asserts what the runtime
DOES with a model's answers rather than what a mock said it would do.

Scenario (one agent, Fully Autonomous):

1. Portfolio: a brainstorm, an idea with a summary, a project with a PLAN.
2. **Rate & Promote** runs. The reviewer rates the idea 9 "promote", the
   project 7 "execute", the brainstorm 2 "discard". The runtime then builds a
   promotion worker that scaffolds ``prototypes/solar-kiln/`` through
   ``write_file``; the idea is marked promoted and linked; the brainstorm is
   discarded (reversibly); every decision is in the audit log.
3. **Advance Work** runs with the execution tool set. The worker reads the
   project with ``cortex_view``, writes ``STATUS.md`` for real, and books a
   follow-up step with ``cortex_schedule``; the task lands in the scheduler
   database under the agent's own name, created by ``cortex:<agent>``.
4. The scheduled step **fires** through ``OlanoScheduler._execute_db_task``
   with a real ``AgentRunner``; the turn runs under the cognition origin and
   appends to ``JOURNAL.md``.
5. With Fully Autonomous **off**, the same review queues an approval card
   instead, nothing moves, and approving the card runs the real promotion.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from olano.cognition import cortex_items as ci
from olano.cognition import orchestrator
from olano.cognition.audit import read_actions
from olano.config.agent_config import AgentConfig
from olano.config.system_settings import set_auto_approve
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

RATINGS_KEY = "portfolio reviewer"
PROTOTYPE_KEY = "scaffold a small, runnable prototype"
EXECUTE_KEY = "FULLY AUTONOMOUS MODE"
STEP_KEY = "[cortex-item:"


@pytest.fixture
def agent(base_config: AgentConfig, tmp_workspace: Path, monkeypatch) -> AgentConfig:
    """A Fully Autonomous agent on the offline model, every tier."""
    base_config.model = "fake:instant"
    base_config.cognition.enabled = True
    base_config.cognition.fast_model = "fake:instant"
    base_config.cognition.deep_model = "fake:instant"
    base_config.cognition.auto_approve = True
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "6")
    # The workspace is the one the tools write into; the prompt names it.
    assert Path(base_config.workspace or "") == tmp_workspace
    return base_config


def _seed(root: Path) -> dict[str, dict]:
    brainstorm = ci.add_item(
        root, kind="brainstorm", title="Cheap energy", summary="angles on cheap energy", detail="x"
    )
    idea = ci.add_item(
        root,
        kind="idea",
        title="Solar kiln",
        summary="Dry lumber in a passive solar shed.",
        detail="# Solar kiln\n\nA shed that dries lumber with the sun.",
    )
    project = ci.add_item(
        root, kind="project", title="Ship It", detail="# Ship It", write_detail=True
    )
    (root / project["path"] / "PLAN.md").write_text(
        "## Phase 1\n- [ ] write the status\n- [ ] run the ingest\n", encoding="utf-8"
    )
    return {"brainstorm": brainstorm, "idea": idea, "project": project}


def _script(monkeypatch, script: dict) -> None:
    monkeypatch.setenv("LIONCLAW_FAKE_MODEL_SCRIPT", json.dumps(script))


def _ratings(items: dict[str, dict]) -> str:
    return json.dumps(
        [
            {
                "id": items["idea"]["id"],
                "score": 9,
                "dimensions": {"value": 9, "feasibility": 8},
                "rationale": "Cheap, concrete, and the owner keeps asking about lumber.",
                "recommendation": "promote",
                "next_step": "Build a sizing calculator.",
            },
            {
                "id": items["project"]["id"],
                "score": 7,
                "rationale": "Phase 1 has a clear next step.",
                "recommendation": "execute",
                "next_step": "Write STATUS.md and run the ingest.",
            },
            {
                "id": items["brainstorm"]["id"],
                "score": 2,
                "rationale": "Vague and superseded by the kiln idea.",
                "recommendation": "discard",
            },
        ]
    )


def _prototype_steps(root: Path) -> list[dict]:
    folder = root / "prototypes" / "solar-kiln"
    return [
        {
            "tool": "write_file",
            "args": {
                "file_path": str(folder / "README.md"),
                "content": "# Solar kiln\n\nA tiny sizing calculator.\n\n## How to run\n\npython3 size.py\n",
            },
        },
        {
            "tool": "write_file",
            "args": {"file_path": str(folder / "size.py"), "content": "print(42)\n"},
        },
        {
            "tool": "write_file",
            "args": {
                "file_path": str(folder / "RESULTS.md"),
                "content": "Ran `python3 size.py`: printed 42. Verdict: works.\n",
            },
        },
        {
            "text": (
                "Scaffolded and ran the prototype.\n"
                "ARTIFACTS: prototypes/solar-kiln/README.md, prototypes/solar-kiln/size.py, "
                "prototypes/solar-kiln/RESULTS.md"
            )
        },
    ]


async def test_fully_autonomous_rates_promotes_executes_and_schedules(
    agent: AgentConfig, tmp_workspace: Path, monkeypatch
) -> None:
    root = tmp_workspace
    items = _seed(root)
    project = items["project"]
    project_dir = root / project["path"]

    # ── 1. Rate & Promote: ratings land, the idea becomes a prototype ON DISK,
    # the brainstorm is discarded, every step is audited.
    _script(
        monkeypatch,
        {
            RATINGS_KEY: [{"text": "Here are the ratings:\n" + _ratings(items)}],
            PROTOTYPE_KEY: _prototype_steps(root),
            "default": [{"text": "ok"}],
        },
    )
    result = await orchestrator.run_phase("triage", agent, root, source="autonomous")
    assert result["status"] == "completed", result
    assert result["count"] == 3
    assert "Promoted: Solar kiln → prototype" in result["summary"]
    assert "Discarded (reversible): Cheap energy (2/10)" in result["summary"]

    idea = ci.get_item(root, items["idea"]["id"])
    assert idea is not None and idea["status"] == "promoted"
    assert (
        idea["rating"]["score"] == 9 and idea["rating"]["next_step"] == "Build a sizing calculator."
    )
    assert idea["links"]["promoted_to"] == "prototypes/solar-kiln"
    protos = ci.list_items(root, kind="prototype")
    assert len(protos) == 1
    proto = protos[0]
    assert proto["title"] == "Solar kiln" and proto["links"]["from_item"] == idea["id"]
    assert (root / proto["path"] / "RESULTS.md").read_text(encoding="utf-8").startswith("Ran ")
    assert (root / proto["path"] / "size.py").is_file()
    assert ci.get_item(root, items["brainstorm"]["id"])["status"] == "discarded"
    assert ci.get_item(root, project["id"])["rating"]["recommendation"] == "execute"
    actions = [a["action"] for a in read_actions(root)]
    assert {"cortex_rate", "cortex_promote", "cortex_discard", "cortex_triage"} <= set(actions)
    promote_entry = next(a for a in read_actions(root) if a["action"] == "cortex_promote")
    assert promote_entry["detail"]["autonomous"] is True
    assert promote_entry["detail"]["to_kind"] == "prototype"
    # Two deep runs were counted: the review and the promotion.
    from olano.cognition.router import deep_runs_today

    assert deep_runs_today(root) == 2

    # ── 2. Advance Work executes: a real STATUS.md and a real scheduled step.
    status_path = project_dir / "STATUS.md"
    _script(
        monkeypatch,
        {
            EXECUTE_KEY: [
                {"tool": "cortex_view", "args": {"item_id": project["id"]}},
                {
                    "tool": "write_file",
                    "args": {
                        "file_path": str(status_path),
                        "content": "Phase 1 in progress. Next: run the ingest (scheduled).\n",
                    },
                },
                {
                    "tool": "cortex_schedule",
                    "args": {
                        "item_id": project["id"],
                        "instruction": "Run scripts/ingest.py and record the output in JOURNAL.md",
                        "run_at": "2030-01-01T09:00:00",
                    },
                },
                {
                    "text": (
                        "Advanced Ship It.\nSaved: " + str(status_path) + "\n"
                        "ARTIFACTS: projects/ship-it/STATUS.md"
                    )
                },
            ],
            "default": [{"text": "ok"}],
        },
    )
    result = await orchestrator.run_phase("advance", agent, root, source="autonomous")
    assert result["status"] == "completed", result
    assert result["artifacts"] == ["projects/ship-it/STATUS.md"]
    assert status_path.read_text(encoding="utf-8").startswith("Phase 1 in progress")

    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB()
    tasks = db.list_tasks(enabled_only=True, agent_name=agent.name)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["created_by"] == f"cortex:{agent.name}"
    assert task["run_at"] == "2030-01-01T09:00:00"
    assert f"[cortex-item:{project['id']}]" in task["prompt"]
    assert "cortex_schedule" in [a["action"] for a in read_actions(root)]

    # ── 3. The step fires through the real scheduler as a real turn of the
    # agent, under the cognition origin, and changes the project.
    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner
    from olano.scheduler.cron import OlanoScheduler
    from olano.security import request_context as rc

    journal = project_dir / "JOURNAL.md"
    _script(
        monkeypatch,
        {
            STEP_KEY: [
                {
                    "tool": "write_file",
                    "args": {
                        "file_path": str(journal),
                        "content": "2030-01-01: ran scripts/ingest.py, 3 rows ingested.\n",
                    },
                },
                {"text": "Ingest done.\nSaved: " + str(journal)},
            ],
            "default": [{"text": "ok"}],
        },
    )
    origins: list[str] = []
    real_make_origin = rc.make_origin

    def _spy(root_source: str, *a, **k):
        origins.append(root_source)
        return real_make_origin(root_source, *a, **k)

    graph = await build_agent_async(agent)
    runner = AgentRunner(graph, agent)
    sched = OlanoScheduler()
    sched._runners[agent.name] = runner
    with patch.object(rc, "make_origin", side_effect=_spy), patch.object(sched, "remove_db_task"):
        await sched._execute_db_task(db.get_task(task["id"]), db)
    assert "cognition" in origins
    assert journal.read_text(encoding="utf-8").startswith("2030-01-01: ran scripts/ingest.py")
    history = db.get_history(task["id"])
    assert history and not history[0]["error"]
    assert "Ingest done" in str(history[0]["result"])
    # A one-shot step retires itself after running.
    assert db.get_task(task["id"])["enabled"] in (0, False)


async def test_with_approvals_the_review_queues_a_card_and_approval_runs_the_promotion(
    agent: AgentConfig, tmp_workspace: Path, monkeypatch
) -> None:
    from olano.pulse.engine import PulseEngine
    from olano.pulse.models import PulseLane

    set_auto_approve(False)
    agent.cognition.auto_approve = None  # inherit the fleet switch
    root = tmp_workspace
    items = _seed(root)
    _script(
        monkeypatch,
        {
            RATINGS_KEY: [{"text": _ratings(items)}],
            PROTOTYPE_KEY: _prototype_steps(root),
            "default": [{"text": "ok"}],
        },
    )
    result = await orchestrator.run_phase("triage", agent, root, source="autonomous")
    assert result["status"] == "completed", result
    assert "wait for your approval" in result["summary"]
    # Ratings are advisory: nothing moved, nothing was discarded.
    assert ci.get_item(root, items["idea"]["id"])["status"] == "active"
    assert ci.get_item(root, items["brainstorm"]["id"])["status"] == "active"
    assert ci.list_items(root, kind="prototype") == []

    engine = PulseEngine(agent, root / WORKSPACE_STATE_DIRNAME)
    cards = engine.list_pending(lane=PulseLane.APPROVAL, max_items=10)
    assert len(cards) == 1
    card = cards[0]
    assert card.action == {
        "type": "cortex_promote",
        "item_id": items["idea"]["id"],
        "target": "prototype",
    }
    assert "Rated 9/10" in card.content

    # A person approves: the real promotion worker runs and the files appear.
    ok, message = await engine.approve_async(card.id)
    assert ok, message
    protos = ci.list_items(root, kind="prototype")
    assert len(protos) == 1 and (root / protos[0]["path"] / "RESULTS.md").is_file()
    assert ci.get_item(root, items["idea"]["id"])["status"] == "promoted"
    assert engine.get_item(card.id).status.value == "approved"
