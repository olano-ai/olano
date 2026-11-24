"""Per-agent skill on/off switches (``agent.json5 → disabled_skills``).

Skills reach an agent by *directory* — its own ``skills:`` entries plus the
managed ``<home>/skills`` pack every agent on the box shares — so "off for this
one agent" cannot be expressed by dropping a source dir. The switch is therefore
a denylist of skill NAMES, and these tests pin the four places that must honour
it plus the one place they must not:

* the prompt (via the filter middleware the builder hands the merged set),
* ``delegate_task``'s subagent registry,
* ``iter_agent_skills`` (skill-awareness announcements, self-learning),
* the listings, which keep showing a disabled skill — **marked** — because a
  listing that hid it would leave nothing to switch back on.

The last point is the one worth stating outright: disabling never touches the
skill on disk and never affects another agent, even for a skill from the shared
pack.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano import admin
from olano.config.agent_config import AgentConfig
from olano.config.workspace import Workspace
from olano.skills import browse
from olano.skills.loader import (
    compute_disabled_skills,
    disabled_skill_names,
    is_skill_disabled,
    iter_agent_skills,
    load_subagent_registry,
)


def _skill(directory: Path, name: str, *, tier: str = "light", description: str = "d") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\ntier: {tier}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return md


@pytest.fixture
def ws(tmp_path: Path, lionclaw_home: Path) -> Workspace:
    return Workspace(root=tmp_path / "ws")


@pytest.fixture
def cfg(ws: Workspace) -> AgentConfig:
    return AgentConfig(
        name="switcher",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="p",
        workspace=str(ws.root),
    )


# ── name resolution ──────────────────────────────────────────────────────────


def test_names_are_matched_case_insensitively_and_trimmed(cfg) -> None:
    # The field is typed by a human (or written from a dashboard row) while the
    # name it must match comes from a SKILL.md's frontmatter. A case difference
    # silently leaving the skill ON is the failure nobody would think to check.
    cfg.disabled_skills = ["  Canvas-Design  ", "XLSX"]

    assert disabled_skill_names(cfg) == {"canvas-design", "xlsx"}
    assert is_skill_disabled("canvas-design", cfg)
    assert is_skill_disabled("XLSX", cfg)
    assert not is_skill_disabled("web-research", cfg)


def test_no_config_means_nothing_is_disabled() -> None:
    # The unscoped listing has no agent to ask; it must not read as "all off".
    assert disabled_skill_names(None) == set()
    assert not is_skill_disabled("anything", None)


def test_blank_entries_are_ignored(cfg) -> None:
    cfg.disabled_skills = ["", "   ", "real"]
    assert disabled_skill_names(cfg) == {"real"}


# ── what the switch actually turns off ───────────────────────────────────────


def test_compute_disabled_only_reports_skills_that_exist(cfg, ws) -> None:
    # A stale entry naming an uninstalled skill is harmless (it may be installed
    # later), but it must not be reported as a live "off" skill — that is how a
    # listing tells "switched off" from "typo".
    _skill(ws.skills_dir / "here", "here")
    cfg.disabled_skills = ["here", "never-installed"]

    assert compute_disabled_skills(cfg, ws) == {"here": "turned off for this agent"}


def test_disabled_skill_is_not_available_to_the_agent(cfg, ws) -> None:
    _skill(ws.skills_dir / "kept", "kept")
    _skill(ws.skills_dir / "dropped", "dropped")
    cfg.disabled_skills = ["dropped"]

    names = {s["name"] for s in iter_agent_skills(cfg, ws)}
    assert names == {"kept"}


def test_a_disabled_subagent_skill_stops_being_delegable(cfg, ws) -> None:
    # Pruning it from the prompt but leaving it in the registry would keep it
    # callable by name through delegate_task — i.e. the capability the operator
    # just turned off would still work if the model guessed the name.
    _skill(ws.skills_dir / "researcher", "researcher", tier="subagent")
    _skill(ws.skills_dir / "auditor", "auditor", tier="subagent")
    cfg.disabled_skills = ["auditor"]

    assert set(load_subagent_registry(cfg, ws)) == {"researcher"}


def test_the_builder_prunes_disabled_skills_from_the_prompt(cfg, ws) -> None:
    # The middleware is what actually keeps a disabled skill out of the model's
    # context: deepagents freezes skills_metadata per directory before olano
    # middleware runs, so nothing upstream can drop a single skill.
    from olano.core.skill_audit_middleware import build_skill_audit_filter_middleware

    _skill(ws.skills_dir / "kept", "kept")
    _skill(ws.skills_dir / "dropped", "dropped")
    cfg.disabled_skills = ["dropped"]

    (mw,) = build_skill_audit_filter_middleware(blocked_skills=compute_disabled_skills(cfg, ws))
    state = {"skills_metadata": [{"name": "kept"}, {"name": "dropped"}]}
    assert mw.before_model(state, None) == {"skills_metadata": [{"name": "kept"}]}


def test_disabling_leaves_the_skill_on_disk_and_in_the_listing(cfg, ws) -> None:
    md = _skill(ws.skills_dir / "dropped", "dropped")
    cfg.disabled_skills = ["dropped"]

    assert md.exists(), "the switch must never delete the skill"
    rows = {
        s["name"]: s for s in browse.annotate([{"name": "dropped", "location": str(md)}], cfg, ws)
    }
    assert rows["dropped"]["enabled"] is False


def test_annotate_marks_enabled_skills_and_defaults_to_on(cfg, ws) -> None:
    on = _skill(ws.skills_dir / "on", "on")
    rows = {s["name"]: s for s in browse.annotate([{"name": "on", "location": str(on)}], cfg, ws)}
    assert rows["on"]["enabled"] is True

    # No config at all (the unscoped listing) reads as on, not as off.
    unscoped = browse.annotate([{"name": "on", "location": str(on)}], None, ws)
    assert unscoped[0]["enabled"] is True


def test_turning_off_a_common_skill_affects_only_that_agent(ws, tmp_path, lionclaw_home) -> None:
    # The managed pack is one folder shared by every agent on the box, which is
    # the whole reason this is a per-agent denylist rather than a delete.
    from olano.config.home import get_olano_paths

    managed = get_olano_paths().managed_skills_dir
    _skill(managed / "shared", "shared")

    picky = AgentConfig(
        name="picky",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="p",
        workspace=str(ws.root),
        disabled_skills=["shared"],
    )
    other = AgentConfig(
        name="other",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="p",
        workspace=str(ws.root),
    )

    assert "shared" not in {s["name"] for s in iter_agent_skills(picky, ws)}
    assert "shared" in {s["name"] for s in iter_agent_skills(other, ws)}


# ── the admin writer ─────────────────────────────────────────────────────────


def _seed_agent(name: str, *skills: str) -> Path:
    """An agent in the tmp home with *skills* in its own workspace.

    Deliberately no ``workspace:`` key: ``save_agent_config`` excludes that
    field on every write (it is derived from the agent's name), so an agent
    pinned at an arbitrary path would lose the pin the first time the writer
    under test saved — and the test would be measuring that, not the switch.
    """
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    agent_dir = paths.agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps({"name": name, "model": "anthropic:claude-haiku-4-5-20251001"}),
        encoding="utf-8",
    )
    agent_ws = paths.agent_workspace_dir(name)
    for skill_name in skills:
        _skill(agent_ws / "skills" / skill_name, skill_name)
    return agent_ws


def test_set_skill_enabled_round_trips(lionclaw_home) -> None:
    from olano.config.loader import load_agent_config

    _seed_agent("writer", "toggleable")

    off = admin.set_skill_enabled("writer", "toggleable", False)
    assert off["changed"] is True
    assert load_agent_config("writer", source="home").disabled_skills == ["toggleable"]

    on = admin.set_skill_enabled("writer", "toggleable", True)
    assert on["changed"] is True
    assert load_agent_config("writer", source="home").disabled_skills == []


def test_set_skill_enabled_writes_the_skills_own_spelling(lionclaw_home) -> None:
    # Matching is case-insensitive, but the entry saved is the SKILL.md's name,
    # so the config can never disagree with the skill it names.
    from olano.config.loader import load_agent_config

    _seed_agent("writer", "casing")

    admin.set_skill_enabled("writer", "CASING", False)
    assert load_agent_config("writer", source="home").disabled_skills == ["casing"]


def test_set_skill_enabled_is_idempotent(lionclaw_home) -> None:
    _seed_agent("writer", "once")

    admin.set_skill_enabled("writer", "once", False)
    again = admin.set_skill_enabled("writer", "once", False)
    assert again["changed"] is False
    assert again["disabled_skills"] == ["once"]


def test_an_unknown_skill_name_is_refused(lionclaw_home) -> None:
    # A typo saved as a denylist entry does nothing, and the caller walks away
    # believing the skill is off. Refuse instead, and name what does exist.
    _seed_agent("writer", "real")

    with pytest.raises(ValueError, match="no skill named 'raal'"):
        admin.set_skill_enabled("writer", "raal", False)


def test_list_agent_skills_reports_enabled_state(lionclaw_home) -> None:
    _seed_agent("writer", "alpha", "beta")
    admin.set_skill_enabled("writer", "beta", False)

    rows = {s["name"]: s for s in admin.list_agent_skills("writer")}
    assert rows["alpha"]["enabled"] is True
    # Still listed while off — otherwise there is nothing left to switch back on.
    assert rows["beta"]["enabled"] is False
