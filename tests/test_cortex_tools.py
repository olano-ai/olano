"""Tests for the agent-facing cortex_* tools (Cortex items driven from chat)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from olano.cognition import cortex_items as ci
from olano.cognition.audit import read_actions
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.config.ops_defaults import CORTEX_TOOL_NAMES
from olano.security.request_context import push_request_context
from olano.tools import cortex_tools


def _ctx(tmp_path: Path):
    return push_request_context(metadata={"workspace_root": str(tmp_path)})


# ───────────────────────────── CRUD tools ────────────────────────────────────


def test_cortex_add_and_list(tmp_path: Path) -> None:
    with _ctx(tmp_path):
        out = cortex_tools.cortex_add.invoke(
            {
                "kind": "idea",
                "title": "Solar Scheduler",
                "summary": "schedule by sunrise",
                "detail": "## Why\nCron is rigid.",
                "tags": "scheduling, energy",
            }
        )
        assert "Created idea 'Solar Scheduler'" in out
        listed = cortex_tools.cortex_list.invoke({})
    assert "Solar Scheduler" in listed
    assert "[idea/active]" in listed
    items = ci.list_items(tmp_path, kind="idea")
    assert items[0]["tags"] == ["scheduling", "energy"]
    # Mirror regenerated
    assert "Solar Scheduler" in (tmp_path / "IDEAS.md").read_text(encoding="utf-8")


def test_cortex_add_rejects_unknown_kind(tmp_path: Path) -> None:
    with _ctx(tmp_path):
        out = cortex_tools.cortex_add.invoke({"kind": "musing", "title": "x"})
    assert "Unknown kind" in out


def test_cortex_add_improves_near_duplicate(tmp_path: Path) -> None:
    with _ctx(tmp_path):
        cortex_tools.cortex_add.invoke({"kind": "idea", "title": "Weekly Digest"})
        out = cortex_tools.cortex_add.invoke(
            {"kind": "idea", "title": "Weekly Digest", "summary": "a much longer summary"}
        )
    assert "improved it instead" in out
    assert len(ci.list_items(tmp_path, kind="idea")) == 1


def test_cortex_list_filters(tmp_path: Path) -> None:
    a = ci.add_item(tmp_path, kind="idea", title="Keep", detail="x")
    b = ci.add_item(tmp_path, kind="brainstorm", title="Toss", detail="y")
    ci.set_status(tmp_path, b["id"], "discarded")
    with _ctx(tmp_path):
        active = cortex_tools.cortex_list.invoke({"include_discarded": False})
        only_discarded = cortex_tools.cortex_list.invoke({"status": "discarded"})
        bad = cortex_tools.cortex_list.invoke({"kind": "musing"})
    assert a["title"] in active and "Toss" not in active
    assert "Toss" in only_discarded
    assert "Unknown kind" in bad


def test_cortex_view_shows_detail_and_files(tmp_path: Path) -> None:
    item = ci.add_item(
        tmp_path, kind="idea", title="Deep Dive", summary="s", detail="## Body\ncontent here"
    )
    with _ctx(tmp_path):
        out = cortex_tools.cortex_view.invoke({"item_id": item["id"]})
        missing = cortex_tools.cortex_view.invoke({"item_id": "nope1234"})
    assert "Deep Dive" in out
    assert "content here" in out
    assert item["detail_file"] in out
    assert "not found" in missing


def test_cortex_view_project_plan_progress(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="project", title="Ship It", detail="# Ship It")
    plan = tmp_path / item["path"] / "PLAN.md"
    plan.write_text("- [x] one\n- [ ] two\n- [ ] three\n", encoding="utf-8")
    with _ctx(tmp_path):
        out = cortex_tools.cortex_view.invoke({"item_id": item["id"]})
    assert "Plan progress: 1/3" in out


def test_cortex_edit_updates_fields_and_detail(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Old Title", detail="old body")
    with _ctx(tmp_path):
        out = cortex_tools.cortex_edit.invoke(
            {
                "item_id": item["id"],
                "title": "New Title",
                "summary": "fresh",
                "tags": "a,b",
                "detail": "brand new body",
            }
        )
        noop = cortex_tools.cortex_edit.invoke({"item_id": item["id"]})
    assert "Updated" in out
    updated = ci.get_item(tmp_path, item["id"])
    assert updated is not None
    assert updated["title"] == "New Title"
    assert updated["summary"] == "fresh"
    assert updated["tags"] == ["a", "b"]
    body = (tmp_path / item["detail_file"]).read_text(encoding="utf-8")
    assert "brand new body" in body
    assert body.startswith("# New Title")  # header synthesized from the new title
    assert "Nothing to change" in noop


def test_cortex_discard_and_restore(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="brainstorm", title="Meh", detail="x")
    with _ctx(tmp_path):
        out = cortex_tools.cortex_discard.invoke({"item_id": item["id"], "reason": "stale"})
        again = cortex_tools.cortex_restore.invoke({"item_id": item["id"]})
        not_discarded = cortex_tools.cortex_restore.invoke({"item_id": item["id"]})
    assert "Discarded brainstorm 'Meh'" in out
    assert "Restored brainstorm 'Meh'" in again
    restored = ci.get_item(tmp_path, item["id"])
    assert restored is not None and restored["status"] == "active"
    assert "not discarded" in not_discarded
    actions = [a["action"] for a in read_actions(tmp_path)]
    assert "cortex_discard" in actions and "cortex_restore" in actions


# ───────────────────────── deep actions (promote / explore) ──────────────────


async def test_cortex_promote_creates_project(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Big Idea", summary="s", detail="# Big Idea")
    outcome = WorkerOutcome(status="completed", summary="scaffolded")
    with (
        _ctx(tmp_path),
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)),
    ):
        # The user approved it as a project outright: `target="project"` jumps
        # the funnel, which is what the dashboard's Create project button does.
        out = await cortex_tools.cortex_promote.ainvoke(
            {"item_id": item["id"], "target": "project"}
        )
        repeat = await cortex_tools.cortex_promote.ainvoke({"item_id": item["id"]})
    assert "Promoted 'Big Idea' to project" in out
    source = ci.get_item(tmp_path, item["id"])
    assert source is not None and source["status"] == "promoted"
    assert source["links"]["promoted_to_kind"] == "project"
    projects = ci.list_items(tmp_path, kind="project")
    assert projects and projects[0]["title"] == "Big Idea"
    assert projects[0]["links"]["from_item"] == item["id"]
    assert "already promoted" in repeat
    assert "cortex_promote" in [a["action"] for a in read_actions(tmp_path)]


async def test_cortex_promote_default_is_the_next_funnel_stage(tmp_path: Path) -> None:
    """idea → prototype by default: the worker scaffolds a folder, the tool
    registers it as the prototype and links the two."""
    item = ci.add_item(tmp_path, kind="idea", title="Solar Timer", summary="s", detail="# x")

    async def _scaffold(**kwargs):
        folder = tmp_path / "prototypes" / "solar-timer"
        folder.mkdir(parents=True)
        (folder / "README.md").write_text("# Solar Timer\n\nA tiny timer.\n", encoding="utf-8")
        return WorkerOutcome(status="completed", summary="built")

    with (
        _ctx(tmp_path),
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(side_effect=_scaffold)),
    ):
        out = await cortex_tools.cortex_promote.ainvoke({"item_id": item["id"]})
    assert "to prototype 'Solar Timer'" in out
    protos = ci.list_items(tmp_path, kind="prototype")
    assert protos and protos[0]["links"]["from_item"] == item["id"]
    source = ci.get_item(tmp_path, item["id"])
    assert source is not None and source["status"] == "promoted"
    assert source["links"]["promoted_to"] == protos[0]["path"]


async def test_cortex_promote_brainstorm_becomes_an_idea(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="brainstorm", title="Energy", summary="s", detail="# E")
    outcome = WorkerOutcome(
        status="completed",
        summary='{"title": "Sunrise Scheduler", "summary": "schedule by sunrise", "detail": "## Why"}',
    )
    with (
        _ctx(tmp_path),
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)),
    ):
        out = await cortex_tools.cortex_promote.ainvoke({"item_id": item["id"]})
    assert "to idea 'Sunrise Scheduler'" in out
    ideas = ci.list_items(tmp_path, kind="idea")
    assert ideas and ideas[0]["links"]["from_item"] == item["id"]
    assert (tmp_path / ideas[0]["detail_file"]).is_file()


async def test_cortex_promote_refuses_a_skipped_stage(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="brainstorm", title="Jump", detail="x")
    with _ctx(tmp_path):
        out = await cortex_tools.cortex_promote.ainvoke(
            {"item_id": item["id"], "target": "prototype"}
        )
    assert "[tool-error]" in out and "Cannot promote a brainstorm to a prototype" in out
    source = ci.get_item(tmp_path, item["id"])
    assert source is not None and source["status"] == "active"


async def test_cortex_promote_guards(tmp_path: Path) -> None:
    project = ci.add_item(tmp_path, kind="project", title="Already", detail="x")
    with _ctx(tmp_path):
        already = await cortex_tools.cortex_promote.ainvoke({"item_id": project["id"]})
        missing = await cortex_tools.cortex_promote.ainvoke({"item_id": "nope1234"})
    assert "already a project" in already
    assert "not found" in missing


async def test_cortex_promote_reports_worker_failure(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Doomed", detail="x")
    outcome = WorkerOutcome(status="error", error="boom")
    with (
        _ctx(tmp_path),
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)),
    ):
        out = await cortex_tools.cortex_promote.ainvoke({"item_id": item["id"]})
    assert "[tool-error]" in out and "boom" in out
    source = ci.get_item(tmp_path, item["id"])
    assert source is not None and source["status"] == "active"  # unchanged on failure


async def test_cortex_explore_enriches_and_returns_active(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Curious", detail="x")
    outcome = WorkerOutcome(
        status="completed", summary="found things", artifacts=[f"{item['path']}/research.md"]
    )
    with (
        _ctx(tmp_path),
        patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)),
    ):
        out = await cortex_tools.cortex_explore.ainvoke({"item_id": item["id"]})
    assert "Explored 'Curious'" in out and "found things" in out
    after = ci.get_item(tmp_path, item["id"])
    assert after is not None and after["status"] == "active"
    assert "cortex_explore" in [a["action"] for a in read_actions(tmp_path)]


async def test_cortex_explore_restores_status_on_error(tmp_path: Path) -> None:
    item = ci.add_item(tmp_path, kind="idea", title="Fragile", detail="x")
    with (
        _ctx(tmp_path),
        patch(
            "olano.cognition.ideation.run_worker",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ),
    ):
        out = await cortex_tools.cortex_explore.ainvoke({"item_id": item["id"]})
    assert "[tool-error]" in out
    after = ci.get_item(tmp_path, item["id"])
    assert after is not None and after["status"] == "active"


# ───────────────────────────── wiring / gating ───────────────────────────────


def test_cortex_toolkit_registered() -> None:
    from olano.tools.registry import resolve_tools

    tools = resolve_tools(list(CORTEX_TOOL_NAMES))
    assert {t.name for t in tools} == set(CORTEX_TOOL_NAMES)


def test_cortex_tools_absent_while_the_engine_is_off(base_config: AgentConfig) -> None:
    # Cortex defaults OFF (global master switch; per-agent cognition.enabled
    # inherits), so a fresh agent carries none of the cortex_* tools.
    assert not any(name in base_config.tools for name in CORTEX_TOOL_NAMES)


def test_cortex_tools_injected_when_engine_enabled(tmp_workspace: Path) -> None:
    from olano.config.agent_config import CognitionConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.config.system_settings import set_cortex_master

    # One agent opting in through the fleet-wide off...
    opted_in = AgentConfig(
        name="pilot",
        workspace=str(tmp_workspace),
        cognition=CognitionConfig(enabled=True),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
    )
    for name in CORTEX_TOOL_NAMES:
        assert name in opted_in.tools

    # ...and every inheriting agent once the engine is on for the fleet.
    set_cortex_master(True)
    inheriting = AgentConfig(
        name="fleet",
        workspace=str(tmp_workspace),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
    )
    for name in CORTEX_TOOL_NAMES:
        assert name in inheriting.tools


def test_cortex_tools_not_injected_when_agent_disables_cognition(tmp_workspace: Path) -> None:
    from olano.config.agent_config import CognitionConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="no-cortex",
        workspace=str(tmp_workspace),
        cognition=CognitionConfig(enabled=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
    )
    assert not any(name in config.tools for name in CORTEX_TOOL_NAMES)


def test_cortex_tools_not_injected_when_master_switch_off(tmp_workspace: Path) -> None:
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.config.system_settings import set_autonomy

    set_autonomy(False, master=False)  # global kill-switch
    config = AgentConfig(
        name="killed",
        workspace=str(tmp_workspace),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
    )
    assert not any(name in config.tools for name in CORTEX_TOOL_NAMES)


def test_cortex_tools_respect_disabled_tools(tmp_workspace: Path) -> None:
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.config.system_settings import set_cortex_master

    set_cortex_master(True)
    config = AgentConfig(
        name="partial",
        workspace=str(tmp_workspace),
        disabled_tools=["cortex_promote", "cortex_explore"],
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace)),
    )
    assert "cortex_list" in config.tools
    assert "cortex_promote" not in config.tools
    assert "cortex_explore" not in config.tools
