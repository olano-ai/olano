"""Tests for copy-on-write skill forks + the preservation gate/quarantine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.skills.fork import (
    PROVENANCE_FILE,
    build_file_manifest,
    fork_base_dir,
    fork_skill,
    fork_violations,
    is_fork,
    is_machine_written,
    read_provenance,
    shadow_violations_vs_source,
    skill_md_headings,
)

RICH_SKILL = (
    "---\n"
    "name: sicop-search-report\n"
    "description: Full SICOP tender search + report pipeline\n"
    "---\n\n"
    "# SICOP Search Report\n\n"
    "## Fase 0 - Perfil\n\n" + ("Company profile, keywords, recipients, defaults.\n" * 8) + "\n"
    "## Fase 1 - Busqueda\n\n"
    "Run `python skills/sicop-search-report/scripts/sicop_avanzada.py --dias 7`.\n"
    + ("Dedup rules and scoring details here.\n" * 8)
    + "\n"
    "## Fase 2 - Detalle\n\n"
    + ("Use scripts/sicop_detalle.py per tender; handle errors.\n" * 8)
    + "\n"
    "## Fase 3 - Plantilla\n\n" + ("Nine mandatory sections per tender.\n" * 8) + "\n"
    "### Checklist final\n\n" + ("Verify all nine items.\n" * 4)
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "skills").mkdir()
    return tmp_path


def _make_original(workspace: Path, slug: str = "sicop-search-report") -> Path:
    d = workspace / "skills" / slug
    (d / "scripts").mkdir(parents=True)
    (d / "references").mkdir()
    (d / "SKILL.md").write_text(RICH_SKILL, encoding="utf-8")
    (d / "scripts" / "sicop_avanzada.py").write_text("print('search')\n", encoding="utf-8")
    (d / "references" / "template.md").write_text("# Report template\n" * 40, encoding="utf-8")
    # Both generations of the pack marker: neither may travel into a fork.
    (d / ".olano-skillpack").touch()
    (d / ".lionclaw-skillpack").touch()
    return d / "SKILL.md"


def _make_stub_shadow(workspace: Path, slug: str = "sicop-search-report") -> Path:
    d = workspace / "skills" / "learned" / slug
    d.mkdir(parents=True)
    stub = (
        "---\n"
        f"name: {slug}\n"
        "description: Search SICOP and report\n"
        'trigger: "sicop"\n'
        "tier: light\n"
        "auto_learned: true\n"
        "category: learned\n"
        "---\n\n"
        f"## {slug}\n\nSearch SICOP and send a report.\n\n**Steps:**\n"
        "1. Search\n2. Filter\n3. Report\n4. Email\n"
    )
    (d / "SKILL.md").write_text(stub, encoding="utf-8")
    return d / "SKILL.md"


# ------------------------------------------------------------------ fork_skill


def test_fork_copies_whole_folder_and_records_provenance(workspace):
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )

    # Aux files travel with the fork; ownership sentinels do not.
    assert (fork_dir / "scripts" / "sicop_avanzada.py").is_file()
    assert (fork_dir / "references" / "template.md").is_file()
    assert not (fork_dir / ".olano-skillpack").exists()
    assert not (fork_dir / ".lionclaw-skillpack").exists()

    # Workspace-relative self-references now point at the fork.
    text = (fork_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "skills/learned/sicop-search-report/scripts/sicop_avanzada.py" in text
    assert "skills/sicop-search-report/scripts" not in text

    # Provenance: frontmatter stamps + sidecar manifests + pristine base.
    assert "lc_improves: sicop-search-report" in text
    assert "lc_source_path: skills/sicop-search-report/SKILL.md" in text
    prov = read_provenance(fork_dir)
    assert prov is not None and is_fork(fork_dir)
    assert "SKILL.md" in prov["source_files"]
    assert "scripts/sicop_avanzada.py" in prov["base_files"]
    assert any("Fase 1" in h for h in prov["skill_md_headings"])
    base = fork_base_dir(workspace, "sicop-search-report")
    assert (base / "SKILL.md").is_file()
    assert (base / "scripts" / "sicop_avanzada.py").is_file()

    # The fork passes its own gate pristine.
    assert fork_violations(fork_dir) == []


def test_fork_refuses_existing_destination(workspace):
    src_md = _make_original(workspace)
    fork_skill(src_md, workspace, "sicop-search-report", source_name="sicop-search-report")
    with pytest.raises(FileExistsError):
        fork_skill(src_md, workspace, "sicop-search-report", source_name="sicop-search-report")


# ------------------------------------------------------------------ gate


def test_gate_passes_additive_edits(workspace):
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    md = fork_dir / "SKILL.md"
    md.write_text(
        md.read_text(encoding="utf-8") + "\n## Learned update\n\nNew edge case.\n", encoding="utf-8"
    )
    (fork_dir / "references" / "extra.md").write_text("more\n", encoding="utf-8")
    assert fork_violations(fork_dir) == []


def test_gate_catches_file_deletion(workspace):
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    (fork_dir / "scripts" / "sicop_avanzada.py").unlink()
    assert any("file removed" in v for v in fork_violations(fork_dir))


def test_gate_catches_hollowed_out_skill_md(workspace):
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    (fork_dir / "SKILL.md").write_text(
        "---\nname: sicop-search-report\n---\n\n## Summary\n\nSearch and report.\n",
        encoding="utf-8",
    )
    violations = fork_violations(fork_dir)
    assert any("shrank" in v for v in violations)
    assert any("headings" in v for v in violations)


def test_gate_ignores_non_fork_dirs(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "SKILL.md").write_text("# plain\n", encoding="utf-8")
    assert fork_violations(d) == []


# ------------------------------------------------------------------ legacy shadows


def test_stub_shadow_flagged_against_rich_original(workspace):
    src_md = _make_original(workspace)
    stub_md = _make_stub_shadow(workspace)
    assert is_machine_written(stub_md)
    violations = shadow_violations_vs_source(stub_md, src_md)
    assert any("shadow body" in v for v in violations)
    assert any("headings" in v for v in violations)
    assert any("missing" in v for v in violations)  # scripts/references absent


def test_full_shadow_passes_against_original(workspace):
    src_md = _make_original(workspace)
    shadow_dir = workspace / "skills" / "learned" / "sicop-search-report"
    (shadow_dir / "scripts").mkdir(parents=True)
    (shadow_dir / "references").mkdir()
    (shadow_dir / "SKILL.md").write_text(
        src_md.read_text(encoding="utf-8") + "\n## Learned update\n\nExtra detail.\n",
        encoding="utf-8",
    )
    (shadow_dir / "scripts" / "sicop_avanzada.py").write_text("print('search')\n", encoding="utf-8")
    (shadow_dir / "references" / "template.md").write_text(
        "# Report template\n" * 40, encoding="utf-8"
    )
    assert shadow_violations_vs_source(shadow_dir / "SKILL.md", src_md) == []


def test_hand_written_shadow_is_not_machine_marked(workspace):
    shadow_dir = workspace / "skills" / "learned" / "my-override"
    shadow_dir.mkdir(parents=True)
    md = shadow_dir / "SKILL.md"
    md.write_text("---\nname: my-override\ndescription: mine\n---\n\n# Mine\n", encoding="utf-8")
    assert not is_machine_written(md)


def test_manifest_and_headings_helpers(workspace):
    src_md = _make_original(workspace)
    manifest = build_file_manifest(src_md.parent)
    assert set(manifest) == {"SKILL.md", "scripts/sicop_avanzada.py", "references/template.md"}
    assert all("sha256" in m and m["size"] > 0 for m in manifest.values())
    headings = skill_md_headings(RICH_SKILL)
    assert "Fase 0 - Perfil" in headings
    assert "Checklist final" in headings


# ------------------------------------------------------------------ loader quarantine


@pytest.fixture
def agent_setup(workspace):
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.config.workspace import Workspace

    cfg = AgentConfig(
        name="test",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir=str(workspace)),
    )
    return cfg, Workspace(root=workspace)


def test_degraded_stub_is_quarantined_and_original_serves(workspace, agent_setup):
    from olano.skills.loader import compute_quarantined_shadows, iter_agent_skills

    cfg, ws = agent_setup
    _make_original(workspace)
    stub_md = _make_stub_shadow(workspace)

    quarantined = compute_quarantined_shadows(cfg, ws)
    assert str(stub_md) in quarantined
    assert quarantined[str(stub_md)]["name"] == "sicop-search-report"
    assert quarantined[str(stub_md)]["original_location"].endswith(
        "skills/sicop-search-report/SKILL.md"
    )

    served = {s["name"]: s for s in iter_agent_skills(cfg, ws)}
    assert "sicop-search-report" in served
    assert "learned" not in served["sicop-search-report"]["location"]


def test_healthy_fork_serves_and_is_not_quarantined(workspace, agent_setup):
    from olano.skills.loader import compute_quarantined_shadows, iter_agent_skills

    cfg, ws = agent_setup
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )

    assert compute_quarantined_shadows(cfg, ws) == {}
    served = {s["name"]: s for s in iter_agent_skills(cfg, ws)}
    assert served["sicop-search-report"]["location"] == str(fork_dir / "SKILL.md")


def test_mangled_fork_is_quarantined_and_original_serves(workspace, agent_setup):
    from olano.skills.loader import compute_quarantined_shadows, iter_agent_skills

    cfg, ws = agent_setup
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    (fork_dir / "SKILL.md").write_text(
        "---\nname: sicop-search-report\n---\n\n## Summary\n\nTiny.\n", encoding="utf-8"
    )

    quarantined = compute_quarantined_shadows(cfg, ws)
    assert str(fork_dir / "SKILL.md") in quarantined
    served = {s["name"]: s for s in iter_agent_skills(cfg, ws)}
    assert "learned" not in served["sicop-search-report"]["location"]


def test_hand_written_shadow_is_never_quarantined(workspace, agent_setup):
    from olano.skills.loader import compute_quarantined_shadows, iter_agent_skills

    cfg, ws = agent_setup
    _make_original(workspace)
    shadow_dir = workspace / "skills" / "learned" / "sicop-search-report"
    shadow_dir.mkdir(parents=True)
    (shadow_dir / "SKILL.md").write_text(
        "---\nname: sicop-search-report\ndescription: my deliberate short override\n---\n\n"
        "# Mine\n\nUse my flow.\n",
        encoding="utf-8",
    )
    assert compute_quarantined_shadows(cfg, ws) == {}
    served = {s["name"]: s for s in iter_agent_skills(cfg, ws)}
    assert "learned" in served["sicop-search-report"]["location"]


# ------------------------------------------------------------------ prompt middleware


def test_quarantine_middleware_swaps_shadow_for_original():
    from olano.core.skill_audit_middleware import SkillQuarantineMiddleware

    mw = SkillQuarantineMiddleware(
        quarantined={
            "/ws/skills/learned/x/SKILL.md": {
                "name": "x",
                "original_location": "/ws/skills/x/SKILL.md",
                "original_description": "the real one",
            }
        }
    )
    state = {
        "skills_metadata": [
            {
                "name": "x",
                "path": "/ws/skills/learned/x/SKILL.md",
                "description": "stub",
                "allowed_tools": [],
            },
            {
                "name": "y",
                "path": "/ws/skills/y/SKILL.md",
                "description": "other",
                "allowed_tools": [],
            },
        ]
    }
    out = mw.before_model(state, None)
    assert out is not None
    swapped = {s["name"]: s for s in out["skills_metadata"]}
    assert swapped["x"]["path"] == "/ws/skills/x/SKILL.md"
    assert swapped["x"]["description"] == "the real one"
    assert swapped["y"]["path"] == "/ws/skills/y/SKILL.md"


def test_quarantine_middleware_drops_shadow_without_original():
    from olano.core.skill_audit_middleware import SkillQuarantineMiddleware

    mw = SkillQuarantineMiddleware(
        quarantined={"/ws/skills/learned/x/SKILL.md": {"name": "x", "original_location": ""}}
    )
    state = {"skills_metadata": [{"name": "x", "path": "/ws/skills/learned/x/SKILL.md"}]}
    out = mw.before_model(state, None)
    assert out == {"skills_metadata": []}


def test_quarantine_middleware_noop_when_original_already_won():
    from olano.core.skill_audit_middleware import SkillQuarantineMiddleware

    mw = SkillQuarantineMiddleware(
        quarantined={"/ws/skills/learned/x/SKILL.md": {"name": "x", "original_location": "/o"}}
    )
    state = {"skills_metadata": [{"name": "x", "path": "/ws/skills/x/SKILL.md"}]}
    assert mw.before_model(state, None) is None


# ------------------------------------------------------------------ heartbeat dedupe


def test_skill_heartbeats_prefer_learned_fork(tmp_path):
    from olano.scheduler.cron import OlanoScheduler

    orig = tmp_path / "skills" / "daily-brief"
    orig.mkdir(parents=True)
    (orig / "SKILL.md").write_text(
        "---\nname: daily-brief\n---\n\noriginal body\n", encoding="utf-8"
    )
    (orig / "HEARTBEAT.md").write_text("- task: brief\n  when: '0 9 * * *'\n", encoding="utf-8")
    fork = tmp_path / "skills" / "learned" / "daily-brief"
    fork.mkdir(parents=True)
    (fork / "SKILL.md").write_text(
        "---\nname: daily-brief\n---\n\nimproved body\n", encoding="utf-8"
    )
    (fork / "HEARTBEAT.md").write_text("- task: brief\n  when: '0 9 * * *'\n", encoding="utf-8")

    results = OlanoScheduler._collect_skill_heartbeats(tmp_path)
    assert len(results) == 1
    slug, hb_path, body = results[0]
    assert slug == "daily-brief"
    assert "learned" in str(hb_path)
    assert "improved body" in body


def test_skill_heartbeats_without_fork_unchanged(tmp_path):
    from olano.scheduler.cron import OlanoScheduler

    orig = tmp_path / "skills" / "daily-brief"
    orig.mkdir(parents=True)
    (orig / "SKILL.md").write_text(
        "---\nname: daily-brief\n---\n\noriginal body\n", encoding="utf-8"
    )
    (orig / "HEARTBEAT.md").write_text("- task: brief\n  when: '0 9 * * *'\n", encoding="utf-8")

    results = OlanoScheduler._collect_skill_heartbeats(tmp_path)
    assert len(results) == 1
    assert "original body" in results[0][2]


# ------------------------------------------------------------------ librarian interplay


def test_stale_shadows_skips_manifest_forks(workspace):
    from olano.cognition.skill_librarian import _stale_shadows

    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    # Drift the source — a legacy shadow would be flagged, a fork must not be
    # (its re-merge is folder-aware and handled separately).
    src_md.write_text(RICH_SKILL + "\n## Fase 7\n\nNew upstream phase.\n", encoding="utf-8")
    assert _stale_shadows(workspace) == []
    assert (fork_dir / PROVENANCE_FILE).is_file()


def test_stale_shadows_still_flags_legacy_shadow(workspace):
    from olano.cognition.skill_librarian import _stale_shadows

    src_md = _make_original(workspace)
    stub_md = _make_stub_shadow(workspace)
    text = stub_md.read_text(encoding="utf-8").replace(
        "auto_learned: true",
        "auto_learned: true\nlc_source_path: skills/sicop-search-report/SKILL.md\n"
        "lc_source_sha256: deadbeef",
    )
    stub_md.write_text(text, encoding="utf-8")
    stale = _stale_shadows(workspace)
    assert len(stale) == 1
    assert stale[0]["shadow"].endswith("learned/sicop-search-report/SKILL.md")
    assert src_md.exists()


# ------------------------------------------------------------------ /skills command surface


async def test_skills_command_shows_quarantined_section(workspace, agent_setup):
    from unittest.mock import MagicMock

    from olano.cli.commands import CommandContext, CommandDispatcher
    from olano.core.runner import AgentRunner

    cfg, _ws = agent_setup
    _make_original(workspace)
    _make_stub_shadow(workspace)

    cfg = cfg.model_copy(update={"workspace": str(workspace)})
    ctx = CommandContext(
        config=cfg,
        runner=AgentRunner(MagicMock(), cfg),
        thread_id="t",
        verbosity="normal",
        think_level="off",
    )
    result = await CommandDispatcher().dispatch("/skills", ctx)
    assert "Quarantined learned copies" in result.output
    assert "sicop-search-report" in result.output
    # The loaded (serving) entry is the original, not the learned stub.
    loaded_block = result.output.split("Quarantined")[0]
    assert "learned" not in loaded_block


# ------------------------------------------------------------------ provenance JSON shape


def test_provenance_sidecar_is_valid_json(workspace):
    src_md = _make_original(workspace)
    fork_dir = fork_skill(
        src_md, workspace, "sicop-search-report", source_name="sicop-search-report"
    )
    data = json.loads((fork_dir / PROVENANCE_FILE).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["source_name"] == "sicop-search-report"
    assert data["source_dir"] == "skills/sicop-search-report"
    assert data["forked_at"]
