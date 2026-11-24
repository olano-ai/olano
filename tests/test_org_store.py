"""Central organization store: migration, reciprocity, cycle detection.

These use the ``lionclaw_home`` fixture (conftest) which isolates LIONCLAW_HOME
to a tmp dir, so each test gets a clean ``organization.yaml`` + agents dir.
"""

import pytest

from olano.config import org_store
from olano.config.agent_config import AgentConfig, OrgConfig
from olano.config.home import get_olano_paths
from olano.config.loader import save_agent_config


def _mk(*names: str) -> None:
    for n in names:
        save_agent_config(AgentConfig(name=n))


def test_set_reports_sets_counterpart_manager(lionclaw_home):
    _mk("boss", "worker")
    org_store.update_org("boss", OrgConfig(reports=["worker"]))
    assert org_store.get_org("boss").reports == ["worker"]
    assert org_store.get_org("worker").manager == "boss"


def test_remove_report_clears_counterpart_manager(lionclaw_home):
    _mk("boss", "worker")
    org_store.update_org("boss", OrgConfig(reports=["worker"]))
    org_store.update_org("boss", OrgConfig(reports=[]))
    assert org_store.get_org("worker").manager is None


def test_set_manager_adds_to_managers_reports(lionclaw_home):
    _mk("boss", "worker")
    _, touched = org_store.update_org("worker", OrgConfig(manager="boss"))
    assert "boss" in touched
    assert "worker" in org_store.get_org("boss").reports


def test_reparenting_removes_from_old_manager(lionclaw_home):
    _mk("a", "b", "w")
    org_store.update_org("w", OrgConfig(manager="a"))
    org_store.update_org("w", OrgConfig(manager="b"))
    assert org_store.get_org("a").reports == []
    assert "w" in org_store.get_org("b").reports


def test_coworkers_are_mutual(lionclaw_home):
    _mk("a", "b")
    org_store.update_org("a", OrgConfig(coworkers=["b"]))
    assert "a" in org_store.get_org("b").coworkers
    org_store.update_org("a", OrgConfig(coworkers=[]))
    assert "a" not in org_store.get_org("b").coworkers


def test_cycle_is_rejected(lionclaw_home):
    _mk("a", "b")
    org_store.update_org("a", OrgConfig(manager="b"))  # b manages a
    with pytest.raises(ValueError):
        org_store.update_org("b", OrgConfig(manager="a"))  # would close a loop


def test_names_are_canonicalised(lionclaw_home):
    _mk("Boss", "Worker")
    org_store.update_org("boss", OrgConfig(reports=["worker"]))
    assert org_store.get_org("Worker").manager == "Boss"


def test_unknown_counterpart_is_one_sided(lionclaw_home):
    _mk("a")
    # 'ghost' is not a real agent → recorded on a's side only, no crash.
    org_store.update_org("a", OrgConfig(reports=["ghost"]))
    assert "ghost" in org_store.get_org("a").reports
    assert org_store.get_org_if_present("ghost") is None


def test_idempotent_re_add(lionclaw_home):
    _mk("boss", "worker")
    org_store.update_org("boss", OrgConfig(reports=["worker"]))
    org_store.update_org("boss", OrgConfig(reports=["worker"]))  # again
    assert org_store.get_org("worker").manager == "boss"
    assert org_store.get_org("boss").reports.count("worker") == 1


def test_first_write_migrates_existing_org_blocks(lionclaw_home):
    save_agent_config(AgentConfig(name="m", org={"reports": ["r"]}))
    save_agent_config(AgentConfig(name="r"))
    save_agent_config(AgentConfig(name="x"))
    # No central file yet; this first write seeds it from the agent.json5 blocks
    # so the other agents' org data isn't dropped.
    org_store.update_org("x", OrgConfig(title="X"))
    assert get_olano_paths().org_path.exists()
    migrated = org_store.get_org_if_present("m")
    assert migrated is not None
    assert migrated.reports == ["r"]


def test_get_org_falls_back_to_agent_json5_before_migration(lionclaw_home):
    save_agent_config(AgentConfig(name="solo", org={"title": "Solo", "output_agents": ["x"]}))
    # No central file written yet → get_org reads the agent.json5 org block.
    org = org_store.get_org("solo")
    assert org.title == "Solo"
    assert org.allows_output("x")


def test_tri_state_roundtrips_through_store(lionclaw_home):
    _mk("a", "b")
    org_store.update_org("a", OrgConfig(input_mode="none", output_mode="list", output_agents=["b"]))
    a = org_store.get_org("a")
    assert a.effective_mode("input") == "none"
    assert a.effective_mode("output") == "list"
    assert a.output_agents == ["b"]


def test_mutating_from_manager_only_roundtrips_through_store(lionclaw_home):
    _mk("a", "b")
    org_store.update_org("a", OrgConfig(manager="b", mutating_from_manager_only=True))
    a = org_store.get_org("a")
    assert a.mutating_from_manager_only is True
    assert a.is_manager("B")  # case-insensitive
    assert not a.is_manager("c")
    assert org_store.entry_for("a")["mutating_from_manager_only"] is True


def test_counterpart_entry_is_seeded_from_its_own_org_block(lionclaw_home):
    """Naming an agent as a report creates its central entry — which shadows
    its agent.json5 block from then on. Seeding that entry blank used to erase
    the counterpart's title (every archetype install writes one) and its
    messaging modes the first time anyone pointed at it."""
    _mk("boss")
    save_agent_config(AgentConfig(name="worker", org=OrgConfig(title="Analyst", input_mode="none")))
    org_store.update_org("boss", OrgConfig(reports=["worker"]))
    worker = org_store.get_org("worker")
    assert worker.manager == "boss"
    assert worker.title == "Analyst"
    assert worker.input_mode == "none"


def test_resolve_title_reads_the_config_block_until_a_central_entry_exists(lionclaw_home):
    save_agent_config(AgentConfig(name="hillary", org=OrgConfig(title="Recruiter")))
    assert org_store.resolve_title("hillary", OrgConfig(title="Recruiter")) == "Recruiter"
    assert org_store.resolve_title("hillary", {"title": " Recruiter "}) == "Recruiter"
    assert org_store.resolve_title("hillary", None) == ""
    org_store.update_org("hillary", OrgConfig(title="Head of Talent"))
    # The central entry wins — even when the block still says otherwise.
    assert org_store.resolve_title("hillary", OrgConfig(title="Recruiter")) == "Head of Talent"
    # …and an emptied central title is an answer, not a fallback.
    org_store.update_org("hillary", OrgConfig(title=None))
    assert org_store.resolve_title("hillary", OrgConfig(title="Recruiter")) == ""
