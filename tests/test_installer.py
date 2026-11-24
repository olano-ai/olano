from __future__ import annotations

import json

from olano.archetypes.installer import (
    emit_system_update_pulse,
    scaffold_agent_workspace,
    sync_system_zone,
)


def test_scaffolded_prompts_limit_follow_up_questions(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    template = {
        "description": "Test agent",
        "emoji": "🦁",
        "system_prompt": "You are a test agent.",
        "subagents": [],
        "tools": [],
    }

    scaffold_agent_workspace("merli", template, workspace_root)

    system_agents_text = (workspace_root / "system" / "prompts" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    soul_text = (workspace_root / "SOUL.md").read_text(encoding="utf-8")

    # The conversation guidance lives in SOUL.md (injected) and the system-default
    # AGENTS.md fallback — NOT in the root AGENTS.md, which is seeded as a clean
    # additive placeholder (no system_prompt copy) so it never shadows the config
    # prompt under the additive merge.
    assert "Ask at most one follow-up question per response" in system_agents_text
    assert "Ask at most one simple follow-up question per reply" in soul_text


def test_scaffolded_agents_md_is_empty_placeholder_not_prompt_copy(tmp_path) -> None:
    """scaffold_agent_workspace must NOT copy system_prompt into AGENTS.md.

    Regression: AGENTS.md used to be seeded with system_prompt[:800], which then
    silently shadowed the full config prompt (and would duplicate it under the
    additive merge). It must now be the pristine scaffold placeholder.
    """
    from olano.config.workspace import is_scaffold_default

    workspace_root = tmp_path / "workspace"
    long_prompt = "You are Zeta, a meticulous specialist. " + ("detail " * 300)
    template = {
        "description": "Zeta agent",
        "emoji": "🦁",
        "system_prompt": long_prompt,
        "subagents": [],
        "tools": [],
    }

    scaffold_agent_workspace("zeta", template, workspace_root)

    agents_text = (workspace_root / "AGENTS.md").read_text(encoding="utf-8")
    assert is_scaffold_default("AGENTS.md", agents_text)
    assert "You are Zeta" not in agents_text  # the system_prompt was not copied in


def test_emit_system_update_pulse_records_noteworthy_changes(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = workspace_root / ".olano" / "state"
    state_dir.mkdir(parents=True)

    item_id = emit_system_update_pulse(
        workspace_root,
        agent_name="phase1-local",
        changed=[
            "prompts/CORE_VIBES.md",
            "policies/DEFAULT_SAFE.yaml",
            "knowledge/USER_GUIDE.md",
        ],
    )

    queue_path = state_dir / "pulse" / "inbox.json"
    assert item_id is not None
    assert queue_path.exists()
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(queue) == 1
    assert "system/CHANGELOG.md" in queue[0]["content"]
    assert "prompts/CORE_VIBES.md" in queue[0]["content"]
    assert "policies/DEFAULT_SAFE.yaml" in queue[0]["content"]
    assert "Potentially breaking system update" in queue[0]["content"]
    assert queue[0]["priority"] == "urgent"
    assert queue[0]["priority_score"] == 90


def test_emit_system_update_pulse_skips_non_noteworthy_changes(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = workspace_root / ".olano" / "state"
    state_dir.mkdir(parents=True)

    item_id = emit_system_update_pulse(
        workspace_root,
        agent_name="phase1-local",
        changed=["knowledge/USER_GUIDE.md"],
    )

    assert item_id is None
    assert not (state_dir / "pulse" / "inbox.json").exists()


def test_emit_system_update_pulse_uses_normal_priority_for_prompt_only_changes(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = workspace_root / ".olano" / "state"
    state_dir.mkdir(parents=True)

    item_id = emit_system_update_pulse(
        workspace_root,
        agent_name="phase1-local",
        changed=["prompts/CORE_VIBES.md"],
    )

    queue = json.loads((state_dir / "pulse" / "inbox.json").read_text(encoding="utf-8"))
    assert item_id is not None
    assert len(queue) == 1
    assert "System zone updated" in queue[0]["content"]
    assert queue[0]["priority"] == "normal"
    assert queue[0]["priority_score"] == 70


def test_scaffold_seeds_only_the_user_guide_into_system_knowledge(tmp_path) -> None:
    """The knowledge zone ships the user guide and nothing else.

    ARCHITECTURE.md / COMMANDS.md / TROUBLESHOOTING.md were engineering docs —
    module paths, trust-model analysis, the project's own defect history — that
    used to be copied verbatim into every agent workspace, where the agent could
    read (and quote) them. They now live outside this repository entirely.
    Seeding one of them again is a regression, not a feature.
    """
    workspace_root = tmp_path / "workspace"
    template = {
        "description": "Test agent",
        "emoji": "🦁",
        "system_prompt": "You are a test agent.",
        "subagents": [],
        "tools": [],
    }

    scaffold_agent_workspace("merli", template, workspace_root)

    knowledge_dir = workspace_root / "system" / "knowledge"
    assert sorted(p.name for p in knowledge_dir.glob("*.md")) == ["USER_GUIDE.md"]
    assert (knowledge_dir / "USER_GUIDE.md").read_text(encoding="utf-8").strip()

    # The refresh path (`olano update` / `olano agents refresh`) re-runs the same
    # system zone with overwrite=True, so pin it here too: a deployment upgrade
    # must not reintroduce what provisioning stopped writing.
    sync_system_zone(workspace_root, agent_name="merli", template=template, overwrite=True)
    assert sorted(p.name for p in knowledge_dir.glob("*.md")) == ["USER_GUIDE.md"]


def test_sync_system_zone_purges_legacy_knowledge_files_and_nothing_else(tmp_path) -> None:
    """An upgraded box loses the three internal docs an older release seeded.

    Provisioning stopped writing ARCHITECTURE.md / COMMANDS.md / TROUBLESHOOTING.md,
    but a box upgraded in place kept the copies it already had — and the builder
    indexes every `system/knowledge/*.md`, so they stayed quotable. The purge is an
    explicit list: a document the operator dropped there themselves survives.
    """
    workspace_root = tmp_path / "workspace"
    template = {
        "description": "Test agent",
        "emoji": "🦁",
        "system_prompt": "You are a test agent.",
        "subagents": [],
        "tools": [],
    }
    scaffold_agent_workspace("merli", template, workspace_root)

    knowledge_dir = workspace_root / "system" / "knowledge"
    for legacy in ("ARCHITECTURE.md", "COMMANDS.md", "TROUBLESHOOTING.md"):
        (knowledge_dir / legacy).write_text("# internal\n", encoding="utf-8")
    (knowledge_dir / "MY_NOTES.md").write_text("# mine\n", encoding="utf-8")

    changed = sync_system_zone(
        workspace_root, agent_name="merli", template=template, overwrite=True
    )

    assert sorted(p.name for p in knowledge_dir.glob("*.md")) == ["MY_NOTES.md", "USER_GUIDE.md"]
    assert {
        "knowledge/ARCHITECTURE.md",
        "knowledge/COMMANDS.md",
        "knowledge/TROUBLESHOOTING.md",
    } <= set(changed)
    changelog = (workspace_root / "system" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- removed `knowledge/ARCHITECTURE.md`" in changelog
    assert "MY_NOTES.md" not in changelog

    # Idempotent: a second sync has nothing left to remove.
    changed_again = sync_system_zone(
        workspace_root, agent_name="merli", template=template, overwrite=True
    )
    assert not [c for c in changed_again if c.startswith("knowledge/")]


def test_scaffold_creates_reports_and_uploads_with_readmes(tmp_path) -> None:
    """Provisioning matches Workspace.init(): both scaffold paths seed the same
    output folders, so an agent's prompt never names a folder that isn't there."""
    from olano.archetypes.installer import scaffold_agent_workspace

    ws = tmp_path / "ws"
    scaffold_agent_workspace("merli", {}, ws, overwrite=False)
    assert "One canonical file per deliverable" in (ws / "reports" / "README.md").read_text()
    assert "Inbound only" in (ws / "uploads" / "README.md").read_text()
    assert (ws / "scratch" / "README.md").is_file()
