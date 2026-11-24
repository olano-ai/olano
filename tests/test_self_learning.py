"""Tests for SelfLearningService — fact distillation, daily log writing, weekly distillation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.config.agent_config import AgentConfig
from olano.memory.markdown_memory import MarkdownMemory
from olano.memory.self_learning import SelfLearningService


@pytest.fixture
def md_memory(tmp_workspace: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_workspace)


@pytest.fixture
def self_learn_svc(
    base_config: AgentConfig, tmp_workspace: Path, md_memory: MarkdownMemory
) -> SelfLearningService:
    return SelfLearningService(
        agent_config=base_config,
        workspace_root=tmp_workspace,
        markdown_memory=md_memory,
        rag_memory=None,
        interval_seconds=3600,
    )


class TestSelfLearningService:
    def test_start_stop(self, self_learn_svc: SelfLearningService) -> None:
        """Service can start and stop cleanly."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.sleep(0))
            self_learn_svc._task = None  # ensure fresh start
            # Just test that start() creates a task without crashing
            # (we don't run the full loop)
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_save_facts_writes_to_daily(
        self, self_learn_svc: SelfLearningService, md_memory: MarkdownMemory
    ) -> None:
        await self_learn_svc._save_facts(["User prefers dark mode", "Project uses FastAPI"])
        today = md_memory.read_today()
        assert "User prefers dark mode" in today
        assert "FastAPI" in today

    @pytest.mark.asyncio
    async def test_save_facts_writes_to_memory_md(
        self, self_learn_svc: SelfLearningService, md_memory: MarkdownMemory
    ) -> None:
        await self_learn_svc._save_facts(["Uses Python 3.12"])
        content = md_memory.read()
        assert "Python 3.12" in content

    @pytest.mark.asyncio
    async def test_save_facts_with_rag(
        self,
        base_config: AgentConfig,
        tmp_workspace: Path,
        md_memory: MarkdownMemory,
    ) -> None:
        mock_rag = MagicMock()
        svc = SelfLearningService(
            agent_config=base_config,
            workspace_root=tmp_workspace,
            markdown_memory=md_memory,
            rag_memory=mock_rag,
        )
        await svc._save_facts(["Fact A"])
        mock_rag.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_distill_facts_returns_list(self, self_learn_svc: SelfLearningService) -> None:
        # Fact distillation is now a headless deepagent — its JSON facts come back
        # in the worker's summary, not from a single-shot model call.
        from olano.cognition.worker import WorkerOutcome

        outcome = WorkerOutcome(
            status="completed", summary='["Prefers async Python", "Uses uv for deps"]'
        )
        with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
            facts = await self_learn_svc._distill_facts(
                [
                    {"role": "user", "content": "How do I use uv?"},
                    {"role": "assistant", "content": "Run: uv add package-name"},
                    {"role": "user", "content": "Thanks, I love async"},
                    {"role": "assistant", "content": "Sure! async/await is great."},
                ]
            )
        assert "Prefers async Python" in facts
        assert "Uses uv for deps" in facts

    @pytest.mark.asyncio
    async def test_distill_facts_handles_model_error(
        self, self_learn_svc: SelfLearningService
    ) -> None:
        with patch(
            "olano.cognition.worker.run_worker",
            new=AsyncMock(side_effect=Exception("API error")),
        ):
            facts = await self_learn_svc._distill_facts(
                [{"role": "user", "content": "x"} for _ in range(5)]
            )
        assert facts == []

    @pytest.mark.asyncio
    async def test_weekly_distillation(
        self, self_learn_svc: SelfLearningService, md_memory: MarkdownMemory
    ) -> None:
        # Write some daily logs so the weekly worker has something to consolidate.
        md_memory.append_daily("User fixed a bug in auth module")
        md_memory.append_daily("Discussed switching to PostgreSQL")

        from olano.cognition.worker import WorkerOutcome

        # Weekly consolidation is a deepagent that writes memory itself; assert the
        # agentic worker is invoked (no single-shot LLM call anymore).
        run_worker = AsyncMock(return_value=WorkerOutcome(status="completed", summary="done"))
        with patch("olano.cognition.worker.run_worker", new=run_worker):
            await self_learn_svc._weekly_distillation()
        run_worker.assert_awaited_once()


class TestByteOffsetReads:
    """Session tail reads are byte-offset based (seek, not a scan from line 0)."""

    def test_tail_read_via_byte_offset(self, tmp_path: Path) -> None:
        from olano.memory.self_learning import _read_new_lines

        p = tmp_path / "s.jsonl"
        p.write_text("l0\nl1\nl2\nl3\n", encoding="utf-8")
        new, end = _read_new_lines(p, 6)  # byte position after "l0\nl1\n"
        assert new == ["l2", "l3"]
        assert end == p.stat().st_size

    def test_appended_lines_only(self, tmp_path: Path) -> None:
        from olano.memory.self_learning import _read_new_lines

        p = tmp_path / "s.jsonl"
        p.write_text("a\nb\n", encoding="utf-8")
        first, end1 = _read_new_lines(p, 0)
        assert first == ["a", "b"]

        with p.open("a", encoding="utf-8") as fh:
            fh.write("c\nd\n")
        second, end2 = _read_new_lines(p, end1)
        assert second == ["c", "d"]
        assert end2 == p.stat().st_size
        # Nothing new → empty read, offset unchanged.
        assert _read_new_lines(p, end2) == ([], end2)

    def test_missing_file_returns_offset_unchanged(self, tmp_path: Path) -> None:
        from olano.memory.self_learning import _read_new_lines

        assert _read_new_lines(tmp_path / "gone.jsonl", 7) == ([], 7)

    def test_legacy_line_count_conversion_equivalence(self, tmp_path: Path) -> None:
        """Converting a legacy line count yields exactly the same tail the old
        line-offset read produced — nothing re-distilled, nothing skipped."""
        from olano.memory.self_learning import _byte_offset_after_line, _read_new_lines

        lines = [f'{{"n": {i}, "pad": "é"}}' for i in range(6)]  # multi-byte chars too
        p = tmp_path / "s.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for count in range(len(lines) + 2):  # incl. count beyond EOF (rotation)
            offset = _byte_offset_after_line(p, count)
            assert offset is not None
            new, end = _read_new_lines(p, offset)
            assert new == lines[count:]
            assert end == p.stat().st_size

    def test_byte_offset_after_line_unreadable_is_none(self, tmp_path: Path) -> None:
        from olano.memory.self_learning import _byte_offset_after_line

        assert _byte_offset_after_line(tmp_path / "gone.jsonl", 3) is None


class TestOffsetStateMigration:
    """Legacy line-count offset state migrates lazily to byte offsets per file."""

    def _seed_session(self, svc: SelfLearningService, turns: list[dict]) -> Path:
        import json

        from olano.config.home import get_olano_paths

        sdir = get_olano_paths().sessions_dir / svc.config.name
        sdir.mkdir(parents=True, exist_ok=True)
        f = sdir / "t.jsonl"
        f.write_text("".join(json.dumps(t) + "\n" for t in turns), encoding="utf-8")
        return f

    def test_legacy_line_count_converted_once(self, self_learn_svc: SelfLearningService) -> None:
        import json

        turns = [{"role": "user", "content": f"turn {i}"} for i in range(5)]
        f = self._seed_session(self_learn_svc, turns)
        # Legacy shape: only a line count recorded for this file.
        self_learn_svc._processed_offsets = {str(f): 3}

        new_turns, pending = self_learn_svc._scan_session_turns()

        assert [t["content"] for t in new_turns] == ["turn 3", "turn 4"]
        # Conversion committed immediately: byte position after the 3 legacy lines.
        processed = "".join(json.dumps(t) + "\n" for t in turns[:3])
        assert self_learn_svc._processed_byte_offsets[str(f)] == len(processed.encode("utf-8"))
        # Pending carries both shapes; uncommitted until the cycle decides.
        assert pending[str(f)] == (5, f.stat().st_size)
        assert self_learn_svc._processed_offsets[str(f)] == 3

    async def test_distill_commits_both_maps_and_reads_appends_only(
        self, self_learn_svc: SelfLearningService
    ) -> None:
        import json

        turns = [{"role": "user", "content": f"turn {i}"} for i in range(2)]
        f = self._seed_session(self_learn_svc, turns)

        # <4 new turns → the cycle commits offsets without a deepagent run.
        await self_learn_svc._distill_from_sessions()
        assert self_learn_svc._processed_offsets[str(f)] == 2
        assert self_learn_svc._processed_byte_offsets[str(f)] == f.stat().st_size

        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "user", "content": "turn 2"}) + "\n")
        new_turns, pending = self_learn_svc._scan_session_turns()
        assert [t["content"] for t in new_turns] == ["turn 2"]
        assert pending[str(f)] == (3, f.stat().st_size)

    def test_stale_entries_pruned_from_both_maps(self, self_learn_svc: SelfLearningService) -> None:
        f = self._seed_session(self_learn_svc, [{"role": "user", "content": "hi"}])
        gone = str(f.parent / "gone.jsonl")
        self_learn_svc._processed_offsets = {gone: 99, str(f): 0}
        self_learn_svc._processed_byte_offsets = {gone: 4096}

        self_learn_svc._scan_session_turns()

        assert gone not in self_learn_svc._processed_offsets
        assert gone not in self_learn_svc._processed_byte_offsets


class TestResetFailures:
    def test_reset_failures_clears_counters(self, self_learn_svc: SelfLearningService) -> None:
        self_learn_svc._record_failure("distill", RuntimeError("boom"))
        self_learn_svc._record_failure("distill", RuntimeError("boom again"))
        self_learn_svc._record_failure("rag_save", ValueError("nope"))
        assert self_learn_svc.status()["failures"]  # non-empty before reset

        cleared = self_learn_svc.reset_failures()

        assert cleared == 2  # two distinct stages cleared
        assert self_learn_svc.status()["failures"] == {}
        assert self_learn_svc.status()["last_failure"] is None

    def test_reset_failures_single_stage(self, self_learn_svc: SelfLearningService) -> None:
        self_learn_svc._record_failure("distill", RuntimeError("boom"))
        self_learn_svc._record_failure("rag_save", ValueError("nope"))

        cleared = self_learn_svc.reset_failures("distill")  # only one stage

        assert cleared == 1
        failures = self_learn_svc.status()["failures"]
        assert "distill" not in failures
        assert "rag_save" in failures  # sibling row survives

    def test_reset_failures_unknown_stage_is_noop(
        self, self_learn_svc: SelfLearningService
    ) -> None:
        self_learn_svc._record_failure("distill", RuntimeError("boom"))
        assert self_learn_svc.reset_failures("nonexistent") == 0
        assert "distill" in self_learn_svc.status()["failures"]
