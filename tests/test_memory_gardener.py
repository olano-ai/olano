"""Tests for deterministic memory dedup + daily-log roll-up (Phase 4a).

The DEEP MemoryGardener worker tests live alongside in Phase 4b.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from olano.memory.markdown_memory import (
    MarkdownMemory,
    _normalize_bullet,
    make_memory_tools,
)


def test_normalize_bullet() -> None:
    assert _normalize_bullet("- [2026-01-01] Uses Python") == "uses python"
    assert _normalize_bullet("-   [2026-02-02]  uses   python ") == "uses python"
    assert _normalize_bullet("## Heading") is None
    assert _normalize_bullet("plain text") is None


def test_dedupe_within_section_keeps_newest_date(tmp_path: Path) -> None:
    mem = MarkdownMemory(tmp_path)
    mem.write(
        "# MEMORY.md\n\n"
        "## Tech\n"
        "- [2026-01-01] Uses Python\n"
        "- [2026-03-03] uses python\n"
        "- [2026-02-02] Likes coffee\n"
    )
    removed = mem.dedupe_facts()
    assert removed == 1
    content = mem.read()
    # The newest-dated form is kept; the older duplicate is gone.
    assert "- [2026-03-03] uses python" in content
    assert "- [2026-01-01] Uses Python" not in content
    assert "Likes coffee" in content


def test_dedupe_is_per_section(tmp_path: Path) -> None:
    mem = MarkdownMemory(tmp_path)
    mem.write("# MEMORY.md\n\n## A\n- [2026-01-01] same fact\n\n## B\n- [2026-01-01] same fact\n")
    removed = mem.dedupe_facts()
    # Same text in different sections is preserved (per-section dedup).
    assert removed == 0
    assert mem.read().count("same fact") == 2


def test_dedupe_preserves_headings_and_nonbullets(tmp_path: Path) -> None:
    mem = MarkdownMemory(tmp_path)
    original = (
        "# MEMORY.md\n\n"
        "Intro paragraph.\n\n"
        "## Knowledge Graph\n<!-- auto-generated -->\n"
        "- [2026-01-01] Entity X\n"
        "- [2026-01-01] entity x\n"
    )
    mem.write(original)
    removed = mem.dedupe_facts()
    assert removed == 1
    content = mem.read()
    assert "Intro paragraph." in content
    assert "<!-- auto-generated -->" in content
    assert "## Knowledge Graph" in content


def test_roll_up_daily_logs(tmp_path: Path) -> None:
    mem = MarkdownMemory(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    recent = now - timedelta(days=2)
    mem.append_daily("old entry", date=old)
    mem.append_daily("recent entry", date=recent)

    rolled = mem.roll_up_daily_logs(keep_days=14)
    assert rolled == 1
    # Old log removed from memories/, recent kept.
    assert not (mem.memories_dir / f"{old.strftime('%Y-%m-%d')}.md").exists()
    assert (mem.memories_dir / f"{recent.strftime('%Y-%m-%d')}.md").exists()
    archive = mem.memories_dir / "archive" / f"{old.strftime('%Y-%m')}.md"
    assert archive.exists()
    assert "old entry" in archive.read_text(encoding="utf-8")


def test_memory_compact_tool(tmp_path: Path) -> None:
    mem = MarkdownMemory(tmp_path)
    mem.write("# MEMORY.md\n\n## A\n- [2026-01-01] x\n- [2026-01-01] x\n")
    tools = {t.name: t for t in make_memory_tools(mem)}
    assert "memory_compact" in tools
    result = tools["memory_compact"].invoke({})
    assert "removed 1 duplicate" in result
    assert mem.read().count("- [2026-01-01] x") == 1


# ----------------------------------------------------------- DEEP gardener (4b)


async def test_run_memory_gardener_runs_deterministic_and_worker(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from olano.cognition.memory_gardener import is_garden_locked, run_memory_gardener
    from olano.cognition.worker import WorkerOutcome
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    mem = MarkdownMemory(tmp_path)
    mem.write("# MEMORY.md\n\n## A\n- [2026-01-01] x\n- [2026-01-01] x\n")
    config = AgentConfig(
        name="g",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )

    outcome = WorkerOutcome(status="completed", summary="merged facts")
    with patch(
        "olano.cognition.memory_gardener.run_worker", new=AsyncMock(return_value=outcome)
    ) as rw:
        result = await run_memory_gardener(config, tmp_path, mem, budget_check=False)

    rw.assert_awaited_once()
    # Deterministic dedup actually ran (one dupe removed).
    assert mem.read().count("- [2026-01-01] x") == 1
    assert "deterministic" in result.summary
    assert "MEMORY.md" in result.artifacts
    # A backup was created.
    backups = tmp_path / ".olano" / "state" / "cognition" / "backups"
    assert backups.exists() and any(backups.iterdir())
    # Lock released after run.
    assert not is_garden_locked(tmp_path)


async def test_run_memory_gardener_respects_lock(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from olano.cognition.memory_gardener import _garden_lock_path, run_memory_gardener
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    mem = MarkdownMemory(tmp_path)
    config = AgentConfig(
        name="g",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )
    lock = _garden_lock_path(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("locked")

    with patch("olano.cognition.memory_gardener.run_worker", new=AsyncMock()) as rw:
        result = await run_memory_gardener(config, tmp_path, mem, budget_check=False)
    rw.assert_not_awaited()
    assert result.status == "rejected"
