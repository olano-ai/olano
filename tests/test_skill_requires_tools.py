"""The ``requires_tools`` skill gate.

A skill written around a specific toolkit — the AgentFather playbooks assume
the ``olano_*`` admin tools — declares ``requires_tools`` in its frontmatter and
is then advertised only to agents whose effective tool set holds every listed
tool. Everything else about it mirrors ``disabled_skills`` (the same four
enforcement points, the same "stays listed, marked" rule), with one difference
these tests pin: a gated skill has no switch. The denylist is not what keeps it
out, so ``set_skill_enabled`` refuses to "turn it on"; granting the tools is what
makes it appear.
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
    compute_gated_skills,
    discover_skills,
    gated_skill_reason,
    iter_agent_skills,
    load_subagent_registry,
)

ADMIN_TOOL = "olano_list_agents"


def _skill(
    directory: Path,
    name: str,
    *,
    tier: str = "light",
    requires_tools: list[str] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / "SKILL.md"
    lines = [f"name: {name}", "description: d", f"tier: {tier}"]
    if requires_tools:
        lines.append("requires_tools: [" + ", ".join(requires_tools) + "]")
    md.write_text("---\n" + "\n".join(lines) + "\n---\n\nBody.\n", encoding="utf-8")
    return md


@pytest.fixture
def ws(tmp_path: Path, lionclaw_home: Path) -> Workspace:
    return Workspace(root=tmp_path / "ws")


@pytest.fixture
def cfg(ws: Workspace) -> AgentConfig:
    return AgentConfig(
        name="plain",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="p",
        workspace=str(ws.root),
    )


# ── the gate itself ──────────────────────────────────────────────────────────


def test_compute_gated_reports_missing_tools_with_reason(cfg, ws) -> None:
    _skill(ws.skills_dir / "admin-ish", "admin-ish", requires_tools=[ADMIN_TOOL])
    _skill(ws.skills_dir / "plain-one", "plain-one")

    assert compute_gated_skills(cfg, ws) == {"admin-ish": f"needs tools: {ADMIN_TOOL}"}

    # Granting the tool clears the gate; disabling it re-gates.
    cfg.tools = [ADMIN_TOOL]
    assert compute_gated_skills(cfg, ws) == {}
    cfg.disabled_tools = [ADMIN_TOOL]
    assert compute_gated_skills(cfg, ws) == {"admin-ish": f"needs tools: {ADMIN_TOOL}"}


def test_the_gate_is_also_read_from_metadata(cfg, ws) -> None:
    """The Agent Skills spec allows no custom top-level frontmatter key, and
    the olano-ai/skills catalog runs the reference validator, so a catalog
    skill (charts, social-image-composer) declares the gate as
    ``metadata: {requires_tools: [...]}``. Same gate, same reason string."""
    d = ws.skills_dir / "catalog-ish"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: catalog-ish\ndescription: d\nmetadata:\n"
        f"  requires_tools: [{ADMIN_TOOL}]\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (found,) = [s for s in discover_skills(ws.skills_dir) if s["name"] == "catalog-ish"]
    assert found["requires_tools"] == [ADMIN_TOOL]
    assert compute_gated_skills(cfg, ws) == {"catalog-ish": f"needs tools: {ADMIN_TOOL}"}
    cfg.tools = [ADMIN_TOOL]
    assert compute_gated_skills(cfg, ws) == {}


def test_every_listed_tool_must_be_present(cfg, ws) -> None:
    _skill(ws.skills_dir / "two", "two", requires_tools=[ADMIN_TOOL, "olano_create_agent"])
    cfg.tools = [ADMIN_TOOL]
    assert compute_gated_skills(cfg, ws) == {"two": "needs tools: olano_create_agent"}


def test_a_universal_default_satisfies_requires_tools_until_the_knob_is_off(cfg, ws) -> None:
    """The gate reads the agent's EFFECTIVE tools, not `tools:` alone — a
    builder-granted default counts, and `default_internal_tools: false`
    takes it away again."""
    _skill(ws.skills_dir / "needs-whoami", "needs-whoami", requires_tools=["whoami"])
    assert compute_gated_skills(cfg, ws) == {}
    # The universal defaults are merged into `tools` at validation time, so the
    # knob has to be set on construction to keep them out.
    locked = AgentConfig(
        name="plain",
        model=cfg.model,
        system_prompt="p",
        workspace=str(ws.root),
        default_internal_tools=False,
    )
    assert compute_gated_skills(locked, ws) == {"needs-whoami": "needs tools: whoami"}


def test_reason_is_none_without_an_agent_or_without_requirements() -> None:
    assert gated_skill_reason({"name": "x", "requires_tools": ["a"]}, None) is None
    assert gated_skill_reason({"name": "x", "requires_tools": []}, set()) is None
    assert gated_skill_reason({"name": "x"}, set()) is None


# ── the four enforcement points ──────────────────────────────────────────────


def test_gated_skill_is_not_available_to_the_agent(cfg, ws) -> None:
    _skill(ws.skills_dir / "kept", "kept")
    _skill(ws.skills_dir / "gated", "gated", requires_tools=[ADMIN_TOOL])

    assert {s["name"] for s in iter_agent_skills(cfg, ws)} == {"kept"}


def test_a_gated_subagent_skill_is_not_delegable(cfg, ws) -> None:
    _skill(ws.skills_dir / "researcher", "researcher", tier="subagent")
    _skill(ws.skills_dir / "fleet-op", "fleet-op", tier="subagent", requires_tools=[ADMIN_TOOL])

    assert set(load_subagent_registry(cfg, ws)) == {"researcher"}


def test_the_builder_prunes_gated_skills_from_the_prompt(cfg, ws) -> None:
    from olano.core.skill_audit_middleware import build_skill_audit_filter_middleware

    _skill(ws.skills_dir / "kept", "kept")
    _skill(ws.skills_dir / "gated", "gated", requires_tools=[ADMIN_TOOL])

    (mw,) = build_skill_audit_filter_middleware(blocked_skills=compute_gated_skills(cfg, ws))
    state = {"skills_metadata": [{"name": "kept"}, {"name": "gated"}]}
    assert mw.before_model(state, None) == {"skills_metadata": [{"name": "kept"}]}


def test_the_capability_banner_drops_gated_skills(cfg, ws) -> None:
    from olano.cli.capability_banner import get_skills_from_dirs

    _skill(ws.skills_dir / "kept", "kept")
    _skill(ws.skills_dir / "gated", "gated", requires_tools=[ADMIN_TOOL])

    names = {s["name"] for s in get_skills_from_dirs([str(ws.skills_dir)], cfg)}
    assert names == {"kept"}
    # No config at all: nothing to gate against.
    assert {s["name"] for s in get_skills_from_dirs([str(ws.skills_dir)])} == {"kept", "gated"}


# ── listings keep the facts apart ────────────────────────────────────────────


def test_gating_keeps_enabled_true_and_stamps_a_reason(cfg, ws) -> None:
    md = _skill(ws.skills_dir / "gated", "gated", requires_tools=[ADMIN_TOOL])
    (row,) = browse.annotate(discover_skills(md.parent), cfg, ws)
    assert row["enabled"] is True  # not switched off — a different fact
    assert row["gated"] == f"needs tools: {ADMIN_TOOL}"
    assert row["requires_tools"] == [ADMIN_TOOL]

    # The unscoped listing has no agent to ask.
    (unscoped,) = browse.annotate(discover_skills(md.parent), None, ws)
    assert unscoped["gated"] is None


def _seed_agent(name: str, tools: list[str] | None = None) -> Path:
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    agent_dir = paths.agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "model": "anthropic:claude-haiku-4-5-20251001"}
    if tools:
        body["tools"] = tools
    (agent_dir / "agent.json5").write_text(json.dumps(body), encoding="utf-8")
    agent_ws = paths.agent_workspace_dir(name)
    _skill(agent_ws / "skills" / "gated", "gated", requires_tools=[ADMIN_TOOL])
    _skill(agent_ws / "skills" / "open", "open")
    return agent_ws


def test_list_agent_skills_reports_gated(lionclaw_home) -> None:
    _seed_agent("writer")
    rows = {s["name"]: s for s in admin.list_agent_skills("writer")}
    assert rows["gated"]["gated"] == f"needs tools: {ADMIN_TOOL}"
    assert rows["open"]["gated"] is None


def test_enabling_a_gated_skill_is_refused_but_disabling_is_allowed(lionclaw_home) -> None:
    _seed_agent("writer")
    with pytest.raises(ValueError, match="needs tools"):
        admin.set_skill_enabled("writer", "gated", True)
    off = admin.set_skill_enabled("writer", "gated", False)
    assert off["disabled_skills"] == ["gated"]


def test_granting_the_tools_lifts_the_gate(lionclaw_home) -> None:
    _seed_agent("holder", tools=[ADMIN_TOOL])
    rows = {s["name"]: s for s in admin.list_agent_skills("holder")}
    assert rows["gated"]["gated"] is None


# ── the built-in playbooks ───────────────────────────────────────────────────


def test_agentfather_is_not_gated_for_the_admin_playbooks_but_a_plain_agent_is() -> None:
    from olano.archetypes.installer import load_archetype_template
    from olano.skills.built_in_sync import _builtin_source_dir
    from olano.skills.loader import effective_tool_names

    built_in = {s["name"]: s for s in discover_skills(_builtin_source_dir())}
    gated = {name for name, s in built_in.items() if s.get("requires_tools")}
    playbooks = {"olano-admin", "olano-agent-builder"}
    assert playbooks <= gated

    father = AgentConfig(**load_archetype_template("agentfather"))
    plain = AgentConfig(name="plain", model="fake:instant")
    for name in playbooks:
        assert gated_skill_reason(built_in[name], effective_tool_names(father)) is None, name
        assert gated_skill_reason(built_in[name], effective_tool_names(plain)), name
    # The everyday platform skills are for everybody.
    for name in ("olano-agent", "olano-platform", "olano-cloud"):
        assert name in built_in
        assert gated_skill_reason(built_in[name], effective_tool_names(plain)) is None
    # `charts` is gated too, but on a universal default (`render_html_image`),
    # so a plain agent sees it — until the defaults knob takes the renderer away.
    assert "charts" in gated
    assert gated_skill_reason(built_in["charts"], effective_tool_names(plain)) is None
    locked = AgentConfig(name="plain", model="fake:instant", default_internal_tools=False)
    assert (
        gated_skill_reason(built_in["charts"], effective_tool_names(locked))
        == "needs tools: render_html_image"
    )
