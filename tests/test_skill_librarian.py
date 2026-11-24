"""Tests for skill librarian helpers — folder-form scan, stale shadows, read-only refs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from olano.cognition.skill_librarian import (
    _is_inside,
    _readonly_skill_refs,
    _restamp_shadows,
    _skill_files,
    _stale_shadows,
)
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import FilesystemBackendConfig


def _write(skill_md: Path, body: str) -> None:
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(body, encoding="utf-8")


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    # A workspace that is NOT a parent of the olano home (tmp_path/olano-home),
    # so managed skills resolve as a sibling — mirroring a real deployment.
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def cfg(ws_root: Path) -> AgentConfig:
    return AgentConfig(
        name="librarian-agent",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(ws_root),
        backend=FilesystemBackendConfig(root_dir=str(ws_root), virtual_mode=False),
    )


def test_skill_files_folder_form_only(tmp_path: Path) -> None:
    _write(tmp_path / "skills" / "alpha" / "SKILL.md", "# alpha")
    _write(tmp_path / "skills" / "learned" / "beta" / "SKILL.md", "# beta")
    # A stray flat *.md (old form) must NOT be picked up.
    (tmp_path / "skills" / "learned" / "stray.md").write_text("# stray", encoding="utf-8")

    files = _skill_files(tmp_path)
    assert "skills/alpha/SKILL.md" in files
    assert "skills/learned/beta/SKILL.md" in files
    assert not any(f.endswith("stray.md") for f in files)


def test_stale_shadows_detects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "skills" / "research" / "SKILL.md"
    _write(source, "---\nname: research\n---\n\n# research v1\n")
    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    shadow = tmp_path / "skills" / "learned" / "research" / "SKILL.md"
    _write(
        shadow,
        "---\nname: research\nlc_improves: research\n"
        f"lc_source_path: skills/research/SKILL.md\nlc_source_sha256: {old_hash}\n---\n\n# better\n",
    )
    # Source unchanged → not stale.
    assert _stale_shadows(tmp_path) == []

    # Third party refreshes the original → shadow becomes stale.
    source.write_text("---\nname: research\n---\n\n# research v2 (refreshed)\n", encoding="utf-8")
    stale = _stale_shadows(tmp_path)
    assert len(stale) == 1
    assert stale[0]["shadow"] == "skills/learned/research/SKILL.md"
    assert stale[0]["source"] == "skills/research/SKILL.md"
    assert stale[0]["recorded_sha256"] != stale[0]["current_sha256"]


def test_restamp_converges(tmp_path: Path) -> None:
    source = tmp_path / "skills" / "research" / "SKILL.md"
    _write(source, "---\nname: research\n---\n\n# research v2\n")
    new_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    shadow = tmp_path / "skills" / "learned" / "research" / "SKILL.md"
    _write(
        shadow,
        "---\nname: research\nlc_improves: research\n"
        "lc_source_path: skills/research/SKILL.md\nlc_source_sha256: OLDHASH\n---\n\n# improved\n",
    )

    stale = _stale_shadows(tmp_path)
    assert len(stale) == 1
    # Deterministic restamp bumps the hash even when the body is unchanged, so an
    # irrelevant source change can't keep the shadow flagged forever.
    assert _restamp_shadows(tmp_path, stale) == 1
    assert f"lc_source_sha256: {new_hash}" in shadow.read_text()
    assert _stale_shadows(tmp_path) == []  # converged


def test_stale_flags_and_restamp_repairs_dropped_hash_line(tmp_path: Path) -> None:
    source = tmp_path / "skills" / "research" / "SKILL.md"
    _write(source, "---\nname: research\n---\n\n# v1\n")
    cur = hashlib.sha256(source.read_bytes()).hexdigest()

    # A shadow whose worker re-merge dropped the lc_source_sha256 line (kept the path).
    shadow = tmp_path / "skills" / "learned" / "research" / "SKILL.md"
    _write(
        shadow,
        "---\nname: research\nlc_improves: research\n"
        "lc_source_path: skills/research/SKILL.md\n---\n\n# improved\n",
    )

    stale = _stale_shadows(tmp_path)
    assert len(stale) == 1  # missing hash → flagged, not silently dropped
    assert _restamp_shadows(tmp_path, stale) == 1
    text = shadow.read_text()
    assert f"lc_source_sha256: {cur}" in text  # line re-inserted
    assert "lc_source_path: skills/research/SKILL.md" in text
    assert _stale_shadows(tmp_path) == []  # converged


def test_is_inside_no_prefix_collision(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    assert _is_inside(str(root / "skills" / "a" / "SKILL.md"), root)
    # A sibling sharing the textual prefix must NOT count as inside.
    sibling = tmp_path / "ws-extra"
    sibling.mkdir()
    assert not _is_inside(str(sibling / "skills" / "b" / "SKILL.md"), root)


def test_readonly_refs_only_lists_out_of_workspace(cfg: AgentConfig, ws_root: Path) -> None:
    from olano.config.workspace import Workspace

    ws = Workspace(root=ws_root)
    # In-workspace skill → editable, NOT a read-only ref.
    _write(ws.skills_dir / "local" / "SKILL.md", "---\nname: local\ndescription: d\n---\n# local")
    # Managed skill (outside the workspace) → read-only ref.
    _write(
        ws.managed_skills_dir / "global-skill" / "SKILL.md",
        "---\nname: global-skill\ndescription: a managed one\n---\n# g",
    )

    refs = {r["name"]: r for r in _readonly_skill_refs(cfg, ws_root)}
    assert "global-skill" in refs
    assert "local" not in refs
    assert refs["global-skill"]["description"] == "a managed one"


def test_run_budget_scales_with_library_and_floors_at_agent_budget(cfg: AgentConfig) -> None:
    from olano.cognition.skill_librarian import run_budget
    from olano.config.system_settings import effective_worker_budget
    from olano.security.delegate_guards import MAX_MAX_ITERATIONS, MAX_TIMEOUT_SECONDS

    agent_steps, agent_seconds = effective_worker_budget(cfg)
    # A tiny library never gets LESS than the agent's own budget (the old fixed
    # tiers handed an 11-skill library fewer steps than a 5-skill one).
    small_steps, small_timeout = run_budget(cfg, 1)
    assert small_steps >= agent_steps
    assert small_timeout >= agent_seconds
    # A larger library gets more, monotonically.
    mid_steps, mid_timeout = run_budget(cfg, 30)
    assert mid_steps > small_steps
    assert mid_timeout > small_timeout
    # And the ceiling still holds for an absurd one.
    huge_steps, huge_timeout = run_budget(cfg, 10_000)
    assert huge_steps == MAX_MAX_ITERATIONS
    assert huge_timeout == float(MAX_TIMEOUT_SECONDS)


def test_run_budget_honours_agent_override(cfg: AgentConfig) -> None:
    from olano.cognition.skill_librarian import run_budget

    cfg.cognition.worker_max_iterations = 300
    cfg.cognition.worker_timeout_seconds = 5000
    steps, timeout = run_budget(cfg, 3)
    assert steps == 300
    assert timeout == 5000.0


async def test_unchanged_library_is_skipped_until_a_skill_changes(
    cfg: AgentConfig, ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.cognition import skill_librarian as sl
    from olano.cognition.worker import WorkerOutcome

    _write(ws_root / "skills" / "learned" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n# a\n")
    _write(ws_root / "skills" / "learned" / "beta" / "SKILL.md", "---\nname: beta\n---\n# b\n")

    calls: list[dict] = []

    async def fake_run_worker(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(status="completed", summary="merged nothing")

    monkeypatch.setattr(sl, "run_worker", fake_run_worker)

    outcome, _ = await sl.run_skill_librarian(cfg, ws_root, budget_check=False)
    assert outcome.status == "completed"
    assert len(calls) == 1
    # The run's budget came from the size-scaled resolver, not a fixed tier.
    assert calls[0]["max_iterations"] == sl.run_budget(cfg, 2)[0]

    # Same bytes on disk: no model run, no backup, no streak change.
    outcome, backup = await sl.run_skill_librarian(cfg, ws_root, budget_check=False)
    assert outcome.status == "skipped"
    assert backup == ""
    assert len(calls) == 1

    # A skill changed: the next run spends again.
    _write(ws_root / "skills" / "learned" / "beta" / "SKILL.md", "---\nname: beta\n---\n# b2\n")
    outcome, _ = await sl.run_skill_librarian(cfg, ws_root, budget_check=False)
    assert outcome.status == "completed"
    assert len(calls) == 2


async def test_failed_run_does_not_record_fingerprint(
    cfg: AgentConfig, ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.cognition import skill_librarian as sl
    from olano.cognition.worker import WorkerOutcome

    _write(ws_root / "skills" / "learned" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n# a\n")
    calls = 0

    async def fake_run_worker(**kwargs):
        nonlocal calls
        calls += 1
        return WorkerOutcome(status="error", error="Recursion limit of 60 reached")

    monkeypatch.setattr(sl, "run_worker", fake_run_worker)
    await sl.run_skill_librarian(cfg, ws_root, budget_check=False)
    await sl.run_skill_librarian(cfg, ws_root, budget_check=False)
    # A run that hit its step limit left the library unreviewed, so the next
    # one must try again rather than read the unchanged bytes as "done".
    assert calls == 2
