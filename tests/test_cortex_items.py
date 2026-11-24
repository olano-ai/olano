"""Tests for the Cortex item store + ideation runners (folders + index + promote)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition import cortex_items as ci
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig

# ───────────────────────────── index module ─────────────────────────────────


def test_add_item_creates_folder_index_and_mirror(tmp_path: Path) -> None:
    item = ci.add_item(
        tmp_path,
        kind="idea",
        title="Solar Cron Scheduler",
        summary="schedule by sunrise/sunset",
        detail="## Problem\nCron is rigid.",
        tags=["scheduling"],
    )
    assert item["status"] == "active"
    assert item["path"] == "ideas/solar-cron-scheduler"
    assert (tmp_path / item["detail_file"]).is_file()
    assert (tmp_path / "IDEAS.md").is_file()  # auto-generated mirror
    assert "Solar Cron Scheduler" in (tmp_path / "IDEAS.md").read_text(encoding="utf-8")


def test_unique_slug_avoids_collision(tmp_path: Path) -> None:
    a = ci.add_item(tmp_path, kind="idea", title="Same Title", detail="x")
    b = ci.add_item(tmp_path, kind="idea", title="Same Title", detail="y")
    assert a["slug"] != b["slug"]


def test_set_status_and_counts(tmp_path: Path) -> None:
    a = ci.add_item(tmp_path, kind="idea", title="Keep", detail="x")
    b = ci.add_item(tmp_path, kind="idea", title="Toss", detail="y")
    ci.set_status(tmp_path, b["id"], "discarded")
    assert ci.counts_by_kind(tmp_path)["idea"] == 1  # discarded excluded
    active = ci.list_items(tmp_path, kind="idea", include_discarded=False)
    assert [i["id"] for i in active] == [a["id"]]


def test_unknown_kind_and_status_raise(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ci.add_item(tmp_path, kind="nope", title="x")
    a = ci.add_item(tmp_path, kind="idea", title="x", detail="y")
    with pytest.raises(ValueError):
        ci.set_status(tmp_path, a["id"], "bogus")


# ───────────────────────────── ideation runners ─────────────────────────────


@pytest.fixture
def cfg(base_config: AgentConfig) -> AgentConfig:
    return base_config


async def test_run_idea_generation_materializes_items(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_idea_generation

    payload = (
        '[{"title": "Solar kiln", "summary": "dry lumber with the sun", "detail": "## A", "tags": ["x"]},'
        ' {"title": "Bike stall", "summary": "repair bikes at the market", "detail": "## B"}]'
    )
    with patch(
        "olano.cognition.ideation.run_worker",
        new=AsyncMock(return_value=WorkerOutcome(status="completed", summary=payload)),
    ):
        outcome, count, _ = await run_idea_generation(cfg, tmp_workspace, budget_check=False)
    assert count == 2
    titles = [i["title"] for i in ci.list_items(tmp_workspace, kind="idea")]
    assert "Solar kiln" in titles and "Bike stall" in titles


async def test_run_brainstorm_materializes_one_session(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_brainstorm

    payload = '{"title": "Cutting token spend", "summary": "angles", "detail": "- caching"}'
    with patch(
        "olano.cognition.ideation.run_worker",
        new=AsyncMock(return_value=WorkerOutcome(status="completed", summary=payload)),
    ):
        outcome, count, _ = await run_brainstorm(cfg, tmp_workspace, budget_check=False)
    assert count == 1
    assert ci.counts_by_kind(tmp_workspace)["brainstorm"] == 1


async def test_promote_to_project_links_and_marks_promoted(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_promote_to_project

    idea = ci.add_item(tmp_workspace, kind="idea", title="Solar Scheduler", detail="rigid cron")
    with patch(
        "olano.cognition.ideation.run_worker",
        new=AsyncMock(return_value=WorkerOutcome(status="completed", summary="planned")),
    ):
        outcome, project = await run_promote_to_project(cfg, tmp_workspace, idea)
    assert project is not None
    assert project["kind"] == "project"
    assert project["links"].get("from_item") == idea["id"]
    assert ci.get_item(tmp_workspace, idea["id"])["status"] == "promoted"


async def test_advance_noop_when_no_active_items(cfg: AgentConfig, tmp_workspace: Path) -> None:
    from olano.cognition.ideation import run_advance_items

    outcome, count, _ = await run_advance_items(cfg, tmp_workspace, budget_check=False)
    assert outcome.status == "empty" and count == 0  # nothing to advance, no worker run


async def test_advance_runs_when_items_exist(cfg: AgentConfig, tmp_workspace: Path) -> None:
    from olano.cognition.ideation import run_advance_items

    ci.add_item(tmp_workspace, kind="idea", title="Advanceable", detail="x")
    outcome = WorkerOutcome(
        status="completed", summary="advanced", artifacts=["ideas/advanceable/research.md"]
    )
    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)) as rw:
        res, count, _ = await run_advance_items(cfg, tmp_workspace, budget_check=False)
    rw.assert_awaited_once()
    assert res.status == "completed" and count == 1


async def test_promote_rolls_back_on_worker_failure(cfg: AgentConfig, tmp_workspace: Path) -> None:
    from olano.cognition.ideation import run_promote_to_project

    idea = ci.add_item(tmp_workspace, kind="idea", title="Doomed", detail="x")
    with patch(
        "olano.cognition.ideation.run_worker",
        new=AsyncMock(return_value=WorkerOutcome(status="error", error="model down")),
    ):
        outcome, project = await run_promote_to_project(cfg, tmp_workspace, idea)
    assert project is None
    assert ci.get_item(tmp_workspace, idea["id"])["status"] == "active"  # source untouched
    # The placeholder project shell was discarded, not left dangling.
    assert ci.counts_by_kind(tmp_workspace)["project"] == 0


# ───────────────────────── preview_for (card description) ────────────────────


def test_preview_for_prefers_summary(tmp_workspace: Path) -> None:
    item = ci.add_item(
        tmp_workspace, kind="idea", title="With Summary", summary="the summary", detail="## H\nbody"
    )
    assert ci.preview_for(tmp_workspace, item) == "the summary"


def test_preview_for_falls_back_to_first_detail_paragraph(tmp_workspace: Path) -> None:
    item = ci.add_item(
        tmp_workspace,
        kind="idea",
        title="No Summary",
        summary="",
        detail="## Heading\n\n- a bullet\n\nThe **first** real paragraph.",
    )
    out = ci.preview_for(tmp_workspace, item)
    assert out == "The first real paragraph."  # heading + bullet skipped, markdown stripped


def test_preview_for_empty_when_nothing_readable(tmp_workspace: Path) -> None:
    item = ci.add_item(
        tmp_workspace, kind="idea", title="Heading Only", summary="", detail="# Title"
    )
    assert ci.preview_for(tmp_workspace, item) == ""


# ───────────────────────── de-duplication helpers ───────────────────────────


def test_find_similar_matches_normalized_and_fuzzy(tmp_workspace: Path) -> None:
    ci.add_item(tmp_workspace, kind="idea", title="Weekly Missed-Trade Review", detail="x")
    # punctuation/spacing differences collapse to the same item
    hit = ci.find_similar(tmp_workspace, "idea", "Weekly Missed Trade Review")
    assert hit is not None and hit["title"] == "Weekly Missed-Trade Review"
    # a genuinely different idea does not match
    assert ci.find_similar(tmp_workspace, "idea", "Solar Cron Scheduler") is None
    # kind is respected — same title, different kind = no match
    assert ci.find_similar(tmp_workspace, "prototype", "Weekly Missed-Trade Review") is None


def test_upsert_item_improves_instead_of_duplicating(tmp_workspace: Path) -> None:
    first, created1 = ci.upsert_item(
        tmp_workspace, kind="idea", title="Watchlist Score", summary="short", tags=["a"]
    )
    assert created1 is True
    second, created2 = ci.upsert_item(
        tmp_workspace,
        kind="idea",
        title="Watchlist  Score",  # near-identical
        summary="a much longer and more useful summary",
        tags=["b"],
    )
    assert created2 is False
    assert second["id"] == first["id"]  # improved the existing one
    # only ONE active idea exists
    assert len(ci.list_items(tmp_workspace, kind="idea", include_discarded=False)) == 1
    # the longer summary won; tags merged
    refreshed = ci.get_item(tmp_workspace, first["id"])
    assert refreshed["summary"] == "a much longer and more useful summary"
    assert set(refreshed["tags"]) == {"a", "b"}


def test_merge_duplicates_keeps_most_developed(tmp_workspace: Path) -> None:
    a = ci.add_item(tmp_workspace, kind="idea", title="Dup Idea", summary="", detail="x")
    b = ci.add_item(
        tmp_workspace, kind="idea", title="Dup Idea", summary="longer summary", detail="y"
    )
    # b has a longer summary → it should be the keeper.
    discarded = ci.merge_duplicates(tmp_workspace, kind="idea")
    assert discarded == 1
    assert ci.get_item(tmp_workspace, b["id"])["status"] == "active"
    dup = ci.get_item(tmp_workspace, a["id"])
    assert dup["status"] == "discarded"
    assert dup["links"].get("duplicate_of") == b["id"]


# ───────────────────────── resolve_detail fallbacks ─────────────────────────


def test_resolve_detail_uses_detail_file(tmp_workspace: Path) -> None:
    item = ci.add_item(
        tmp_workspace, kind="idea", title="Has Detail", detail="## Body\nreal content"
    )
    detail, files = ci.resolve_detail(tmp_workspace, item)
    assert "real content" in detail
    assert item["detail_file"] in files


def test_resolve_detail_synthesizes_from_summary_when_file_missing(tmp_workspace: Path) -> None:
    # Register a prototype against an EMPTY folder (worker never wrote the README).
    (tmp_workspace / "prototypes" / "empty-proto").mkdir(parents=True)
    item = ci.add_item(
        tmp_workspace,
        kind="prototype",
        title="Empty Proto",
        summary="what this prototype does",
        slug="empty-proto",
        write_detail=False,
    )
    detail, _files = ci.resolve_detail(tmp_workspace, item)
    # Never blank — falls back to a synthetic title + summary.
    assert "what this prototype does" in detail
    assert "Empty Proto" in detail


def test_resolve_detail_falls_back_to_promoted_project(tmp_workspace: Path) -> None:
    proj_dir = tmp_workspace / "projects" / "promoted-proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "README.md").write_text("# Promoted\n\nthe real project readme\n", encoding="utf-8")
    (tmp_workspace / "prototypes" / "src-proto").mkdir(parents=True)
    item = ci.add_item(
        tmp_workspace,
        kind="prototype",
        title="Src Proto",
        slug="src-proto",
        write_detail=False,
        status="promoted",
        links={"promoted_to": "projects/promoted-proj"},
    )
    detail, files = ci.resolve_detail(tmp_workspace, item)
    assert "the real project readme" in detail
    assert "projects/promoted-proj/README.md" in files


def test_find_similar_threshold_one_is_exact_only(tmp_workspace: Path) -> None:
    ci.add_item(tmp_workspace, kind="prototype", title="Bounty Generator", detail="x")
    # default threshold would fuzzy-match a close title; threshold=1.0 must not.
    assert ci.find_similar(tmp_workspace, "prototype", "Bounty Generador", threshold=1.0) is None
    # exact normalized match still hits at threshold 1.0
    assert (
        ci.find_similar(tmp_workspace, "prototype", "bounty  generator", threshold=1.0) is not None
    )


def test_upsert_item_writes_richer_detail(tmp_workspace: Path) -> None:
    first, _ = ci.upsert_item(
        tmp_workspace, kind="idea", title="Detail Grows", detail="short", summary="s"
    )
    detail_path = tmp_workspace / first["detail_file"]
    ci.upsert_item(
        tmp_workspace,
        kind="idea",
        title="Detail Grows",
        detail="## Much\nlonger and richer regenerated detail body",
        summary="s",
    )
    assert "richer regenerated detail body" in detail_path.read_text(encoding="utf-8")


def test_merge_duplicates_keeps_promoted_over_active(tmp_workspace: Path) -> None:
    active = ci.add_item(tmp_workspace, kind="idea", title="Dup", summary="x", detail="y")
    promoted = ci.add_item(tmp_workspace, kind="idea", title="Dup", summary="x", detail="y")
    ci.set_status(tmp_workspace, promoted["id"], "promoted")
    ci.merge_duplicates(tmp_workspace, kind="idea")
    # the promoted item survives even though the active one is just as developed
    assert ci.get_item(tmp_workspace, promoted["id"])["status"] == "promoted"
    assert ci.get_item(tmp_workspace, active["id"])["status"] == "discarded"


def test_resolve_detail_handles_empty_path(tmp_workspace: Path) -> None:
    # A path-less item must not walk the whole workspace.
    (tmp_workspace / "stray.md").write_text("unrelated workspace file", encoding="utf-8")
    item = {"id": "x", "kind": "idea", "title": "Pathless", "path": "", "summary": "the summary"}
    detail, files = ci.resolve_detail(tmp_workspace, item)
    assert files == []
    assert "the summary" in detail and "unrelated workspace file" not in detail


async def test_run_prototype_registers_the_folder_the_worker_wrote(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    """The worker scaffolds prototypes/<slug>/ itself; the item must point at
    that folder, never at a suffixed sibling the index minted beside it."""
    from olano.cognition.ideation import run_prototype

    ci.add_item(tmp_workspace, kind="idea", title="Solar kiln", detail="x")

    async def _scaffold(**kwargs):
        folder = tmp_workspace / "prototypes" / "solar-kiln"
        folder.mkdir(parents=True)
        (folder / "README.md").write_text("# Solar kiln\n\nA tiny calculator.\n", encoding="utf-8")
        return WorkerOutcome(status="completed", summary="built")

    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(side_effect=_scaffold)):
        _outcome, count, _ = await run_prototype(cfg, tmp_workspace, budget_check=False)
    assert count == 1
    (proto,) = ci.list_items(tmp_workspace, kind="prototype")
    assert proto["path"] == "prototypes/solar-kiln"
    assert (tmp_workspace / proto["detail_file"]).is_file()
    assert not (tmp_workspace / "prototypes" / "solar-kiln-2").exists()
