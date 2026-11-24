"""The fleet scheduler tools stay AgentFather's, and stay admin-gated.

``olano_scheduler_*`` reads and writes every agent's scheduled jobs across both
stores. Two properties keep that from being a hole:

* **Nothing grants them by default**, and reading one is owner-only — a job's
  prompt says what another agent is being told to do, unattended, on a timer.
* **Writing one needs an admin human on the turn.** Scheduling work for another
  agent is strictly more reach than ``olano_run_agent_command``: that runs one
  command once under supervision, while a scheduled job makes the agent take a
  full model turn on a prompt chosen here, repeatedly, with nobody watching.
  Rewriting an existing job's prompt reaches the same state, and ``run_now``
  makes the agent take that turn immediately — so all three sit in
  ``cross_agent_exec``, which every profile gates and the runner refuses to
  blanket-approve.

``olano_scheduler_delete`` is ``write_external`` instead: it removes autonomy
rather than granting it, so ``cross_agent_exec`` would be a semantic lie.
"""

from __future__ import annotations

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.ops_defaults import resolve_effective_tool_names
from olano.security.hitl_policy import CATEGORY_TOOLS, PROFILE_CATEGORIES
from olano.security.pipeline import evaluate_tool_access
from olano.security.request_context import push_request_context
from olano.security.tool_metadata import get_tool_metadata
from olano.tools import olano_admin

READS = ("olano_scheduler_list", "olano_scheduler_get")
CROSS_AGENT_WRITES = (
    "olano_scheduler_create",
    "olano_scheduler_update",
    "olano_scheduler_run_now",
)
ALL_SIX = READS + CROSS_AGENT_WRITES + ("olano_scheduler_delete",)


# ── nothing hands these out ───────────────────────────────────────────────────


def test_no_fleet_scheduler_tool_reaches_a_default_agent():
    """Verified against the real resolution, not by reading the grant lists —
    a new entry in any ops_defaults / internal_tools path would otherwise put a
    fleet-wide scheduler writer on every agent."""
    cfg = AgentConfig(name="plain", model="fake:instant")
    effective = set(resolve_effective_tool_names(cfg.tools, cfg.disabled_tools, cfg))
    assert effective.isdisjoint(ALL_SIX)


def test_the_ordinary_toolkit_gets_scheduler_get_and_no_fleet_tool():
    """The ops toolkit's own read gap is closed by scheduler_get, not by handing
    an ordinary agent the fleet surface."""
    from olano.config.ops_defaults import OPS_TOOL_NAMES

    assert "scheduler_get" in OPS_TOOL_NAMES
    assert set(OPS_TOOL_NAMES).isdisjoint(ALL_SIX)


# ── every one of them is owner-only ───────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_SIX)
def test_every_fleet_scheduler_tool_is_owner_only(name):
    meta = get_tool_metadata(olano_admin.get_tool(name))
    assert meta.owner_only is True, f"{name} must be owner_only"


@pytest.mark.parametrize("name", READS)
def test_reads_stay_read_only(name):
    """Read-only keeps them out of the approval categories — an admin auditing
    the fleet's schedule should not approve each list."""
    meta = get_tool_metadata(olano_admin.get_tool(name))
    assert meta.capability == "read_only"
    assert meta.requires_hitl is False


@pytest.mark.parametrize("name", CROSS_AGENT_WRITES + ("olano_scheduler_delete",))
def test_writes_are_mutating_and_hitl(name):
    meta = get_tool_metadata(olano_admin.get_tool(name))
    assert meta.capability == "mutating"
    assert meta.requires_hitl is True
    assert meta.risk == "high"


@pytest.mark.parametrize("name", ALL_SIX)
def test_pipeline_denies_every_fleet_scheduler_tool_to_a_non_owner(name):
    """The metadata is only worth what the pipeline does with it."""
    cfg = AgentConfig(name="agentfather", model="fake:instant")
    meta = get_tool_metadata(olano_admin.get_tool(name))

    with push_request_context(source="api", channel="api", is_owner=False, is_admin=False):
        denied = evaluate_tool_access(name, meta, cfg)
    assert denied.allowed is False
    assert denied.layer == 5

    with push_request_context(source="api", channel="api", is_owner=True, is_admin=True):
        assert evaluate_tool_access(name, meta, cfg).allowed is True


# ── HITL wiring ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", CROSS_AGENT_WRITES)
def test_scheduling_for_another_agent_is_cross_agent_exec_in_every_profile(name):
    assert name in CATEGORY_TOOLS["cross_agent_exec"]
    for profile, cats in PROFILE_CATEGORIES.items():
        assert "cross_agent_exec" in cats, f"{profile} must gate cross-agent scheduling"


def test_delete_is_write_external_not_cross_agent_exec():
    assert "olano_scheduler_delete" in CATEGORY_TOOLS["write_external"]
    assert "olano_scheduler_delete" not in CATEGORY_TOOLS["cross_agent_exec"]


def test_gated_even_in_the_developer_profile():
    """The most permissive profile, with every toggle off, still approves these."""
    from types import SimpleNamespace

    from olano.security.hitl_policy import resolve_approval_set

    sec = SimpleNamespace(
        profile="developer",
        hitl_enabled=True,
        hitl_write_tools=False,
        hitl_shell=False,
        tools={},
    )
    approval = resolve_approval_set(SimpleNamespace(security=sec))
    for name in CROSS_AGENT_WRITES:
        assert name in approval, f"{name} must stay gated in the developer profile"


@pytest.mark.parametrize("name", CROSS_AGENT_WRITES)
def test_runner_never_auto_approves_fleet_scheduling(name):
    """Remembered 'always' rules and the low-risk switch must not bypass it —
    an unattended scheduled turn must not be able to schedule more work."""
    from types import SimpleNamespace

    from olano.core import runner

    action = {"name": name, "args": {"agent": "victim", "task_id": "abc"}}
    cfg = SimpleNamespace(
        name="agentfather",
        security=SimpleNamespace(hitl_auto_approve_low_risk=True),
    )
    assert runner._is_action_auto_approved(action, cfg) is False


# ── privilege bookkeeping ─────────────────────────────────────────────────────


def test_the_new_tools_are_privileged_so_they_cannot_be_granted_by_name():
    """privileged_tool_names() inverts the GENERATED name index, so a stale
    index would silently let olano_update_agent(x, {"tools": [...]}) hand a
    fleet scheduler writer to an ordinary agent."""
    from olano.admin.integrations import privileged_tool_names

    assert set(ALL_SIX) <= privileged_tool_names()
