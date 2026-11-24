"""AgentFather reviews any agent's Cortex: list, view, discard, restore, promote.

``olano_cortex_*`` act on ANOTHER agent's Cortex index, through the same store
(``cognition/cortex_items``) and the same promotion job (``cognition/jobs`` +
``ideation.run_promote``) the dashboard uses. These tests pin:

* the target is resolved by name, never the caller's workspace;
* a bulk discard needs a selector (an empty one is refused, never "everything"),
  skips what must not be discarded and says why, and is undone by restore;
* a promotion runs as a Cortex job under the TARGET's config, joins a running
  one, and refuses what the funnel does not allow;
* the gates: owner-only, reads read-only, writes in HITL's write_external,
  nothing grants them by default, AgentFather ships with them, and the
  migration hands them to existing AgentFathers.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from olano import admin
from olano.cognition import cortex_items as ci
from olano.cognition import jobs as cj

READS = ("olano_cortex_list", "olano_cortex_view")
WRITES = ("olano_cortex_discard", "olano_cortex_restore", "olano_cortex_promote")
ALL_FIVE = READS + WRITES


@pytest.fixture(autouse=True)
def _fresh_registry():
    cj.REGISTRY.reset()
    yield
    cj.REGISTRY.reset()


def _agent_root(name: str) -> Path:
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace

    root = get_workspace(load_agent_config(name).workspace).root
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def scout() -> Path:
    """An agent named scout with a small funnel in its own workspace."""
    admin.create_agent("scout", model="fake:instant")
    return _agent_root("scout")


def _seed(root: Path) -> dict[str, dict]:
    items = {
        "b1": ci.add_item(root, kind="brainstorm", title="Weekly market scan"),
        "b2": ci.add_item(root, kind="brainstorm", title="Partner referral loop"),
        "b3": ci.add_item(root, kind="brainstorm", title="Old pricing riff"),
        "i1": ci.add_item(root, kind="idea", title="Usage-based pricing page"),
    }
    ci.set_rating(root, items["b1"]["id"], 3, recommendation="discard")
    ci.set_rating(root, items["b2"]["id"], 8, recommendation="promote")
    ci.set_status(root, items["b3"]["id"], "promoted")
    return items


# ── store helper ──────────────────────────────────────────────────────────────


def test_set_statuses_writes_once_and_skips_unknown_ids(tmp_path: Path) -> None:
    a = ci.add_item(tmp_path, kind="idea", title="A")
    b = ci.add_item(tmp_path, kind="brainstorm", title="B")
    changed = ci.set_statuses(tmp_path, [a["id"], b["id"], "nope"], "discarded")
    assert sorted(it["id"] for it in changed) == sorted([a["id"], b["id"]])
    assert {it["status"] for it in ci.list_items(tmp_path)} == {"discarded"}
    with pytest.raises(ValueError):
        ci.set_statuses(tmp_path, [a["id"]], "gone")


# ── reads ─────────────────────────────────────────────────────────────────────


def test_list_reads_the_target_agents_index(scout: Path) -> None:
    items = _seed(scout)
    admin.create_agent("other", model="fake:instant")
    ci.add_item(_agent_root("other"), kind="idea", title="Someone else's idea")

    out = admin.cortex.list_items("scout", kind="brainstorm", status="all")
    titles = {r["title"] for r in out["items"]}
    assert titles == {"Weekly market scan", "Partner referral loop", "Old pricing riff"}
    assert "Someone else's idea" not in titles
    assert out["counts"]["brainstorm"] >= 2
    rated = next(r for r in out["items"] if r["id"] == items["b2"]["id"])
    assert rated["score"] == 8 and rated["recommendation"] == "promote"

    # Score filters keep rated items only.
    low = admin.cortex.list_items("scout", max_score=4)
    assert [r["id"] for r in low["items"]] == [items["b1"]["id"]]


def test_the_default_lists_only_items_still_in_play(scout: Path) -> None:
    """Promoted and discarded items are history; the default view is the work
    still open, and ``counts_by_status`` says what it leaves out."""
    items = _seed(scout)
    ci.set_status(scout, items["i1"]["id"], "discarded")
    exploring = ci.add_item(scout, kind="idea", title="Being researched")
    ci.set_status(scout, exploring["id"], "exploring")

    out = admin.cortex.list_items("scout")
    assert {r["id"] for r in out["items"]} == {
        items["b1"]["id"],
        items["b2"]["id"],
        exploring["id"],  # an item under a research run is still in play
    }
    assert out["status_filter"] == ["active", "exploring"]
    assert out["counts_by_status"] == {
        "active": 2,
        "exploring": 1,
        "promoted": 1,
        "discarded": 1,
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("discarded", {"i1"}),
        ("promoted", {"b3"}),
        ("promoted, discarded", {"b3", "i1"}),
        ("all", {"b1", "b2", "b3", "i1"}),
        ("ALL", {"b1", "b2", "b3", "i1"}),
    ],
)
def test_status_selects_other_items(scout: Path, status: str, expected: set[str]) -> None:
    items = _seed(scout)
    ci.set_status(scout, items["i1"]["id"], "discarded")
    out = admin.cortex.list_items("scout", status=status)
    assert {r["id"] for r in out["items"]} == {items[k]["id"] for k in expected}


def test_an_unknown_status_is_refused(scout: Path) -> None:
    with pytest.raises(ValueError, match="Valid statuses: active, exploring"):
        admin.cortex.list_items("scout", status="archived")


def test_fleet_mode_stamps_each_row_with_its_agent(scout: Path) -> None:
    _seed(scout)
    admin.create_agent("other", model="fake:instant")
    ci.add_item(_agent_root("other"), kind="idea", title="Someone else's idea")

    out = admin.cortex.list_items("", kind="idea")
    by_title = {r["title"]: r["agent"] for r in out["items"]}
    assert by_title["Usage-based pricing page"] == "scout"
    assert by_title["Someone else's idea"] == "other"
    assert {"scout", "other"} <= set(out["counts_by_agent"])
    assert out["counts_by_status"]["active"] == 2  # one idea each, both in play


def test_view_returns_detail_and_refuses_an_unknown_item(scout: Path) -> None:
    items = _seed(scout)
    out = admin.cortex.get_item("scout", items["b2"]["id"])
    assert out["item"]["title"] == "Partner referral loop"
    assert out["item"]["rating"]["score"] == 8
    with pytest.raises(ValueError, match="not found"):
        admin.cortex.get_item("scout", "missing")
    with pytest.raises(ValueError, match="does not exist"):
        admin.cortex.list_items("ghost")


# ── discard / restore ─────────────────────────────────────────────────────────


def test_an_empty_selector_is_refused_not_read_as_everything(scout: Path) -> None:
    _seed(scout)
    with pytest.raises(ValueError, match="Nothing was discarded"):
        admin.cortex.discard_items("scout")
    assert all(it["status"] != "discarded" for it in ci.list_items(scout))


def test_discard_by_kind_skips_promoted_and_keeps_other_kinds(scout: Path) -> None:
    items = _seed(scout)
    out = admin.cortex.discard_items("scout", kind="brainstorm", reason="pruning", actor="af")

    assert sorted(d["id"] for d in out["discarded"]) == sorted(
        [items["b1"]["id"], items["b2"]["id"]]
    )
    status = {it["id"]: it["status"] for it in ci.list_items(scout)}
    assert status[items["b3"]["id"]] == "promoted"  # history is not discarded
    assert status[items["i1"]["id"]] == "active"  # another kind is untouched

    from olano.cognition.audit import read_actions

    entry = read_actions(scout, limit=5)[0]
    assert entry["action"] == "cortex_discard"
    assert entry["detail"]["by"] == "af" and entry["detail"]["reason"] == "pruning"


def test_discard_with_max_score_keeps_unrated_and_higher_rated(scout: Path) -> None:
    items = _seed(scout)
    extra = ci.add_item(scout, kind="brainstorm", title="Unrated thought")
    out = admin.cortex.discard_items("scout", kind="brainstorm", max_score=4)
    assert [d["id"] for d in out["discarded"]] == [items["b1"]["id"]]
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert reasons[extra["id"]] == "not rated yet"
    assert "above 4" in reasons[items["b2"]["id"]]


def test_named_items_report_what_was_not_discarded(scout: Path) -> None:
    items = _seed(scout)
    out = admin.cortex.discard_items(
        "scout", item_ids=[items["i1"]["id"], items["b3"]["id"], "nope"]
    )
    assert [d["id"] for d in out["discarded"]] == [items["i1"]["id"]]
    assert out["not_found"] == ["nope"]
    assert out["skipped"][0]["id"] == items["b3"]["id"]

    # A kind filter alongside names only discards names of that kind.
    out = admin.cortex.discard_items("scout", item_ids=[items["b1"]["id"]], kind="idea")
    assert out["discarded"] == [] and "not a idea" in out["skipped"][0]["reason"]


async def test_an_item_being_promoted_is_not_discarded(scout: Path) -> None:
    items = _seed(scout)
    gate = asyncio.Event()

    async def run(scope) -> None:
        await gate.wait()

    job, _ = cj.REGISTRY.start(
        agent="scout", workspace_root=scout, kind="promote", target=items["b2"]["id"], run=run
    )
    out = admin.cortex.discard_items("scout", item_ids=[items["b2"]["id"]])
    assert out["discarded"] == [] and "working on it" in out["skipped"][0]["reason"]
    gate.set()
    await job.task


def test_restore_brings_discarded_items_back_and_leaves_the_rest(scout: Path) -> None:
    items = _seed(scout)
    admin.cortex.discard_items("scout", item_ids=[items["i1"]["id"]])
    out = admin.cortex.restore_items("scout", [items["i1"]["id"], items["b3"]["id"]])
    assert [r["id"] for r in out["restored"]] == [items["i1"]["id"]]
    assert out["skipped"][0]["id"] == items["b3"]["id"]
    assert ci.get_item(scout, items["i1"]["id"])["status"] == "active"
    assert ci.get_item(scout, items["b3"]["id"])["status"] == "promoted"


# ── promote ───────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_promote(monkeypatch):
    """Replace the deep worker; record which config it ran under."""
    from olano.cognition import ideation
    from olano.cognition.worker import WorkerOutcome

    calls: list[dict] = []
    gate = asyncio.Event()
    gate.set()

    async def fake_run_promote(config, workspace_root, it, *, target=None, source="manual", **kw):
        calls.append({"agent": config.name, "root": workspace_root, "target": target})
        await gate.wait()
        new = ci.add_item(
            workspace_root, kind=target or ci.next_kind(it["kind"]), title=it["title"]
        )
        ci.set_status(workspace_root, it["id"], "promoted")
        return WorkerOutcome(status="completed", summary="Distilled."), new

    monkeypatch.setattr(ideation, "run_promote", fake_run_promote)
    return calls, gate


async def test_promote_runs_under_the_target_agent_as_a_cortex_job(scout, fake_promote) -> None:
    calls, _gate = fake_promote
    items = _seed(scout)
    out = await admin.cortex.promote_item("scout", items["b2"]["id"], actor="agentfather")

    assert out["status"] == "done" and out["to_kind"] == "idea"
    assert out["created"]["kind"] == "idea"
    assert calls == [{"agent": "scout", "root": scout, "target": "idea"}]
    assert ci.get_item(scout, items["b2"]["id"])["status"] == "promoted"
    # The run is recorded where the dashboard looks for it.
    job = cj.REGISTRY.history(scout, "scout", kind="promote")[0]
    assert job["target"] == items["b2"]["id"] and job["detail"]["requested_by"] == "agentfather"


async def test_promote_without_waiting_and_a_second_call_joins(scout, fake_promote) -> None:
    calls, gate = fake_promote
    gate.clear()
    items = _seed(scout)
    first = await admin.cortex.promote_item(
        "scout", items["i1"]["id"], target="project", wait=False
    )
    second = await admin.cortex.promote_item(
        "scout", items["i1"]["id"], target="project", wait=False
    )
    assert first["status"] == "running" and second["joined_running_job"] is True
    assert second["job_id"] == first["job_id"]
    gate.set()
    await cj.REGISTRY.get_memory(first["job_id"]).task
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("key", "target", "message"),
    [
        ("i1", "idea", "only moves forward"),
        ("b3", "", "already promoted"),
        ("i1", "galaxy", "Unknown target"),
    ],
)
async def test_promote_refuses_what_the_funnel_does_not_allow(
    scout, fake_promote, key, target, message
) -> None:
    items = _seed(scout)
    with pytest.raises(ValueError, match=message):
        await admin.cortex.promote_item("scout", items[key]["id"], target=target)
    assert fake_promote[0] == []


async def test_a_discarded_item_must_be_restored_first(scout, fake_promote) -> None:
    items = _seed(scout)
    admin.cortex.discard_items("scout", item_ids=[items["i1"]["id"]])
    with pytest.raises(ValueError, match="Restore it first"):
        await admin.cortex.promote_item("scout", items["i1"]["id"])


# ── the tool layer and its gates ──────────────────────────────────────────────


def test_tools_return_json_errors_instead_of_raising(scout: Path) -> None:
    from olano.tools import olano_admin

    out = json.loads(olano_admin.olano_cortex_discard.invoke({"agent": "scout"}))
    assert "Nothing was discarded" in out["error"]
    out = json.loads(olano_admin.olano_cortex_list.invoke({"agent": "scout"}))
    assert out["agent"] == "scout"


@pytest.mark.parametrize("name", ALL_FIVE)
def test_every_cortex_admin_tool_is_owner_only(name: str) -> None:
    from olano.security.tool_metadata import get_tool_metadata
    from olano.tools import olano_admin

    meta = get_tool_metadata(olano_admin.get_tool(name))
    assert meta.owner_only is True
    if name in READS:
        assert meta.capability == "read_only" and meta.requires_hitl is False
    else:
        assert meta.capability == "mutating" and meta.requires_hitl is True


@pytest.mark.parametrize("name", WRITES)
def test_writes_are_write_external(name: str) -> None:
    from olano.security.hitl_policy import CATEGORY_TOOLS

    assert name in CATEGORY_TOOLS["write_external"]
    assert name not in CATEGORY_TOOLS["cross_agent_exec"]


@pytest.mark.parametrize("name", ALL_FIVE)
def test_pipeline_denies_them_to_a_non_owner(name: str) -> None:
    from olano.config.agent_config import AgentConfig
    from olano.security.pipeline import evaluate_tool_access
    from olano.security.request_context import push_request_context
    from olano.security.tool_metadata import get_tool_metadata
    from olano.tools import olano_admin

    cfg = AgentConfig(name="agentfather", model="fake:instant")
    meta = get_tool_metadata(olano_admin.get_tool(name))
    with push_request_context(source="api", channel="api", is_owner=False, is_admin=False):
        assert evaluate_tool_access(name, meta, cfg).allowed is False
    with push_request_context(source="api", channel="api", is_owner=True, is_admin=True):
        assert evaluate_tool_access(name, meta, cfg).allowed is True


def test_nothing_grants_them_by_default_and_agentfather_ships_with_them() -> None:
    from olano.config.agent_config import AgentConfig
    from olano.config.json5_fast import loads
    from olano.config.ops_defaults import resolve_effective_tool_names

    plain = AgentConfig(name="plain", model="fake:instant")
    effective = set(resolve_effective_tool_names(plain.tools, plain.disabled_tools, plain))
    assert effective.isdisjoint(ALL_FIVE)

    template = Path(__file__).parents[1] / "olano/archetypes/templates/agentfather.json5"
    assert set(ALL_FIVE) <= set(loads(template.read_text())["tools"])


class TestMigration:
    mig = importlib.import_module("olano.migrations.0028_agentfather_cortex_tools")

    def _write(self, home: Path, name: str, body: dict) -> Path:
        path = home / "agents" / name / "agent.json5"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return path

    def test_grants_the_five_after_the_scheduler_tools(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "agentfather",
            {"name": "agentfather", "tools": ["olano_scheduler_run_now", "olano_update"]},
        )
        self.mig.apply(tmp_path)
        tools = json.loads(path.read_text())["tools"]
        assert tools == ["olano_scheduler_run_now", *ALL_FIVE[:2], *WRITES, "olano_update"]
        self.mig.apply(tmp_path)  # idempotent
        assert json.loads(path.read_text())["tools"] == tools

    def test_respects_an_opt_out_and_skips_other_agents(self, tmp_path: Path) -> None:
        opted = self._write(
            tmp_path,
            "agentfather",
            {"name": "agentfather", "tools": ["x"], "disabled_tools": ["olano_cortex_promote"]},
        )
        other = self._write(tmp_path, "scout", {"name": "scout", "tools": ["x"]})
        self.mig.apply(tmp_path)
        tools = json.loads(opted.read_text())["tools"]
        assert "olano_cortex_promote" not in tools and "olano_cortex_discard" in tools
        assert json.loads(other.read_text())["tools"] == ["x"]
