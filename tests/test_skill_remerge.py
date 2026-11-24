"""Tests for three-way re-merge of skill forks after their source refreshes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from olano.skills.fork import (
    PROVENANCE_FILE,
    fork_base_dir,
    fork_skill,
    fork_violations,
    read_provenance,
)
from olano.skills.remerge import (
    _transform_theirs,
    detect_stale_forks,
    post_remerge_pulse,
    remerge_fork,
    remerge_stale_forks,
)

SKILL_V1 = (
    "---\n"
    "name: sicop-search-report\n"
    "description: SICOP tender pipeline\n"
    "---\n\n"
    "# SICOP\n\n"
    "## Fase 0 - Perfil\n\n"
    "Collect company profile and keywords before searching.\n"
    + ("Details of the profile phase.\n" * 8)
    + "\n## Fase 1 - Busqueda\n\n"
    "Run `python skills/sicop-search-report/scripts/search.py --dias 7`.\n"
    + ("Dedup and scoring rules.\n" * 8)
    + "\n## Fase 2 - Reporte\n\n"
    + ("Nine mandatory report sections.\n" * 8)
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "skills").mkdir()
    return tmp_path


def _make_source(workspace: Path, slug: str = "sicop-search-report") -> Path:
    d = workspace / "skills" / slug
    (d / "scripts").mkdir(parents=True)
    (d / "references").mkdir()
    (d / "SKILL.md").write_text(SKILL_V1, encoding="utf-8")
    (d / "scripts" / "search.py").write_text("print('v1')\nTIMEOUT = 30\n", encoding="utf-8")
    (d / "references" / "template.md").write_text("# Template v1\n" * 30, encoding="utf-8")
    return d


def _fork(workspace: Path, slug: str = "sicop-search-report") -> Path:
    return fork_skill(workspace / "skills" / slug / "SKILL.md", workspace, slug, source_name=slug)


def _append_learned(fork_dir: Path) -> None:
    md = fork_dir / "SKILL.md"
    md.write_text(
        md.read_text(encoding="utf-8") + "\n## Learned update (2026-07-22)\n\nRetry on 429.\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------ detection


def test_detects_script_only_drift(workspace):
    src = _make_source(workspace)
    _fork(workspace)
    assert detect_stale_forks(workspace) == []  # nothing drifted yet

    (src / "scripts" / "search.py").write_text("print('v2')\nTIMEOUT = 60\n", encoding="utf-8")
    stale = detect_stale_forks(workspace)
    assert [s["slug"] for s in stale] == ["sicop-search-report"]


def test_orphaned_source_not_stale(workspace):
    src = _make_source(workspace)
    _fork(workspace)
    shutil.rmtree(src)
    assert detect_stale_forks(workspace) == []


def test_non_fork_learned_dirs_ignored(workspace):
    d = workspace / "skills" / "learned" / "hand-made"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: hand-made\n---\n# mine\n", encoding="utf-8")
    assert detect_stale_forks(workspace) == []


# ------------------------------------------------------------------ deterministic matrix


def test_upstream_modified_fork_untouched_taken(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (src / "references" / "template.md").write_text("# Template v2\n" * 30, encoding="utf-8")

    summary = remerge_fork(workspace, fork_dir)
    assert summary["taken_upstream"] == ["references/template.md"]
    assert summary["conflicts"] == []
    assert "Template v2" in (fork_dir / "references" / "template.md").read_text()
    # Manifests + base converge: same drift is never re-flagged, gate still passes.
    assert remerge_fork(workspace, fork_dir)["skipped"] == "source unchanged"
    assert detect_stale_forks(workspace) == []
    assert fork_violations(fork_dir) == []
    assert (
        "Template v2"
        in (
            fork_base_dir(workspace, "sicop-search-report") / "references" / "template.md"
        ).read_text()
    )


def test_skill_md_taken_upstream_is_transformed(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    v2 = SKILL_V1.replace("Nine mandatory report sections.", "TEN mandatory report sections.")
    (src / "SKILL.md").write_text(v2, encoding="utf-8")
    new_hash = hashlib.sha256((src / "SKILL.md").read_bytes()).hexdigest()

    summary = remerge_fork(workspace, fork_dir)
    assert "SKILL.md" in summary["taken_upstream"]
    text = (fork_dir / "SKILL.md").read_text()
    assert "TEN mandatory report sections." in text
    # Fork-lineage transform re-applied: provenance restamped with the NEW source
    # hash, and self-references rewritten to the fork's own path.
    assert f"lc_source_sha256: {new_hash}" in text
    assert "lc_improves: sicop-search-report" in text
    assert "skills/learned/sicop-search-report/scripts/search.py" in text
    assert text.count("lc_source_sha256:") == 1


def test_upstream_added_file_taken(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (src / "references" / "faq.md").write_text("# FAQ\n", encoding="utf-8")

    summary = remerge_fork(workspace, fork_dir)
    assert summary["taken_upstream"] == ["references/faq.md"]
    assert (fork_dir / "references" / "faq.md").is_file()


def test_upstream_deleted_untouched_removed(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (src / "references" / "template.md").unlink()

    summary = remerge_fork(workspace, fork_dir)
    assert summary["deleted"] == ["references/template.md"]
    assert not (fork_dir / "references" / "template.md").exists()
    assert fork_violations(fork_dir) == []  # base_files rebased — no false "removed"


def test_upstream_deleted_fork_modified_kept_as_conflict(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (fork_dir / "references" / "template.md").write_text(
        "# Template v1 + learned notes\n" * 30, encoding="utf-8"
    )
    (src / "references" / "template.md").unlink()

    summary = remerge_fork(workspace, fork_dir)
    assert (fork_dir / "references" / "template.md").is_file()
    assert [c["kind"] for c in summary["conflicts"]] == ["deleted-upstream"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_both_changed_non_overlapping_clean_merge(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    _append_learned(fork_dir)  # fork appends at the END
    v2 = SKILL_V1.replace(
        "Collect company profile and keywords before searching.",
        "Collect company profile, keywords, AND recipients before searching.",
    )  # upstream edits the TOP
    (src / "SKILL.md").write_text(v2, encoding="utf-8")

    summary = remerge_fork(workspace, fork_dir)
    assert summary["merged"] == ["SKILL.md"]
    text = (fork_dir / "SKILL.md").read_text()
    assert "AND recipients" in text  # upstream change absorbed
    assert "Retry on 429." in text  # learned content preserved
    assert fork_violations(fork_dir) == []


def test_both_changed_same_lines_conflict_keeps_ours(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    ours = (
        (fork_dir / "SKILL.md")
        .read_text()
        .replace("Nine mandatory report sections.", "Nine sections tuned by learning.")
    )
    (fork_dir / "SKILL.md").write_text(ours, encoding="utf-8")
    (src / "SKILL.md").write_text(
        SKILL_V1.replace("Nine mandatory report sections.", "Eleven mandatory sections."),
        encoding="utf-8",
    )

    summary = remerge_fork(workspace, fork_dir)
    kinds = {c["rel"]: c["kind"] for c in summary["conflicts"]}
    assert kinds == {"SKILL.md": "prose"}
    assert "Nine sections tuned by learning." in (fork_dir / "SKILL.md").read_text()
    conflict = summary["conflicts"][0]
    assert conflict["fork_path"].endswith("skills/learned/sicop-search-report/SKILL.md")
    assert conflict["source_path"].endswith("skills/sicop-search-report/SKILL.md")


def test_both_changed_script_conflict_keeps_ours(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (fork_dir / "scripts" / "search.py").write_text(
        "print('v1')\nTIMEOUT = 90  # learned\n", encoding="utf-8"
    )
    (src / "scripts" / "search.py").write_text("print('v1')\nTIMEOUT = 15\n", encoding="utf-8")

    summary = remerge_fork(workspace, fork_dir)
    assert [c["kind"] for c in summary["conflicts"]] == ["script"]
    assert "TIMEOUT = 90" in (fork_dir / "scripts" / "search.py").read_text()


def test_missing_git_degrades_to_conflict(workspace, monkeypatch):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    _append_learned(fork_dir)
    (src / "SKILL.md").write_text(SKILL_V1 + "\n## Fase 3\n\nNew upstream phase.\n", "utf-8")

    monkeypatch.setattr("olano.skills.remerge.shutil.which", lambda _: None)
    summary = remerge_fork(workspace, fork_dir)
    assert [c["kind"] for c in summary["conflicts"]] == ["prose"]
    assert "Retry on 429." in (fork_dir / "SKILL.md").read_text()  # ours kept


def test_gate_violating_fork_is_skipped(workspace):
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    (fork_dir / "SKILL.md").write_text("---\nname: sicop-search-report\n---\n\ntiny\n", "utf-8")
    (src / "SKILL.md").write_text(SKILL_V1 + "\nmore\n", encoding="utf-8")

    summary = remerge_fork(workspace, fork_dir)
    assert "preservation gate" in summary["skipped"]
    assert (fork_dir / "SKILL.md").read_text().endswith("tiny\n")  # untouched


def test_transform_strips_stale_upstream_lc_lines(workspace):
    _make_source(workspace)
    fork_dir = _fork(workspace)
    prov = read_provenance(fork_dir)
    upstream = b"---\nname: sicop-search-report\nlc_source_sha256: stalejunk\n---\n\n# body\n"
    out = _transform_theirs(
        upstream,
        "SKILL.md",
        provenance=prov,
        slug="sicop-search-report",
        new_source_md_hash="newhash",
    ).decode()
    assert "stalejunk" not in out
    assert out.count("lc_source_sha256:") == 1
    assert "lc_source_sha256: newhash" in out


# ------------------------------------------------------------------ orchestration + undo


def test_remerge_stale_forks_snapshot_allows_full_undo(workspace):
    from olano.cognition.backups import restore

    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    before = (fork_dir / "references" / "template.md").read_text()
    (src / "references" / "template.md").write_text("# Template v2\n" * 30, encoding="utf-8")

    summaries, backup_job = remerge_stale_forks(workspace)
    assert len(summaries) == 1 and backup_job
    assert "Template v2" in (fork_dir / "references" / "template.md").read_text()

    ok, _msg = restore(workspace, backup_job)
    assert ok
    assert (fork_dir / "references" / "template.md").read_text() == before
    sidecar = json.loads((fork_dir / PROVENANCE_FILE).read_text())
    assert "last_remerge" not in sidecar  # sidecar reverted too


def test_remerge_stale_forks_noop_when_fresh(workspace):
    _make_source(workspace)
    _fork(workspace)
    assert remerge_stale_forks(workspace) == ([], "")


def test_pulse_posted_with_conflicts(workspace):
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.pulse.engine import PulseEngine

    cfg = AgentConfig(
        name="test",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir=str(workspace)),
    )
    summaries = [
        {
            "slug": "sicop-search-report",
            "taken_upstream": ["references/template.md"],
            "merged": [],
            "deleted": [],
            "kept_ours": ["SKILL.md"],
            "conflicts": [
                {
                    "rel": "SKILL.md",
                    "kind": "prose",
                    "fork_path": "f",
                    "source_path": "s",
                    "base_path": "b",
                }
            ],
        }
    ]
    post_remerge_pulse(cfg, workspace, summaries, "job-123")
    items = PulseEngine(cfg, workspace / ".olano").list_pending(max_items=20)
    match = [i for i in items if "re-merged" in (i.title or "")]
    assert match
    assert match[0].action == {"type": "restore_backup", "job_id": "job-123"}
    assert "review" in (match[0].suggested_next_step or "").lower()


# ------------------------------------------------------------------ librarian integration


async def test_librarian_runs_remerge_and_hands_conflicts_to_worker(workspace):
    from olano.cognition.skill_librarian import run_skill_librarian
    from olano.cognition.worker import WorkerOutcome
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    cfg = AgentConfig(
        name="test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(workspace),
        backend=FilesystemBackendConfig(root_dir=str(workspace)),
    )
    src = _make_source(workspace)
    fork_dir = _fork(workspace)
    # Conflict on SKILL.md (both changed the same line) + clean upstream-only
    # change on template.md.
    ours = (
        (fork_dir / "SKILL.md")
        .read_text()
        .replace("Nine mandatory report sections.", "Nine sections tuned by learning.")
    )
    (fork_dir / "SKILL.md").write_text(ours, encoding="utf-8")
    (src / "SKILL.md").write_text(
        SKILL_V1.replace("Nine mandatory report sections.", "Eleven mandatory sections."),
        encoding="utf-8",
    )
    (src / "references" / "template.md").write_text("# Template v2\n" * 30, encoding="utf-8")

    captured: dict = {}

    async def _fake_worker(**kwargs):
        captured.update(kwargs)
        return WorkerOutcome(status="completed", summary="ARTIFACTS: none")

    with patch(
        "olano.cognition.skill_librarian.run_worker", new=AsyncMock(side_effect=_fake_worker)
    ):
        outcome, backup = await run_skill_librarian(cfg, workspace, budget_check=False)

    assert outcome.status == "completed" and backup
    # Deterministic pass already absorbed the clean upstream change…
    assert "Template v2" in (fork_dir / "references" / "template.md").read_text()
    # …and the semantic conflict was handed to the worker with all three paths.
    ctx = captured["context"]
    assert "FORK CONFLICTS" in ctx
    assert str(fork_dir / "SKILL.md") in ctx
    assert str(src / "SKILL.md") in ctx
    assert ".olano/skill_bases/sicop-search-report" in ctx
    # The fork kept ours for the conflicted file.
    assert "Nine sections tuned by learning." in (fork_dir / "SKILL.md").read_text()


def test_conflict_context_line_inlines_outside_workspace(workspace, tmp_path_factory):
    from olano.cognition.skill_librarian import _conflict_context_line

    inside = {
        "rel": "SKILL.md",
        "kind": "prose",
        "fork_path": str(workspace / "skills/learned/x/SKILL.md"),
        "source_path": str(workspace / "skills/x/SKILL.md"),
        "base_path": str(workspace / ".olano/skill_bases/x/SKILL.md"),
    }
    assert "upstream content" not in _conflict_context_line(inside, workspace)

    # A managed skill genuinely outside the workspace tree.
    managed = tmp_path_factory.mktemp("managed-home") / "skills" / "x" / "SKILL.md"
    managed.parent.mkdir(parents=True)
    managed.write_text("---\nname: x\n---\n# managed body\n", encoding="utf-8")
    outside = {**inside, "source_path": str(managed)}
    line = _conflict_context_line(outside, workspace)
    assert "upstream content" in line and "# managed body" in line
